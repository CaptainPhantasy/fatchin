"""Recover saved work without repeating model work or remote writes.

These controls extend the existing web control plane. They preserve evidence:
archiving only changes presentation, exporting copies existing results into a
fresh destination, and resolving uncertainty never silently repeats a post.
"""
from __future__ import annotations

import copy
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .control_store import ControlStore
from .github_client import redact_text
from .interfaces import Job, JobState
from .queue import TERMINAL_STATES


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _failure(message, status=409):
    from .web_control import ApiError
    return ApiError(message, status)


def _public(job):
    return json.loads(job.to_json())


class RecoveryControls:
    """Methods mixed into WebControl; use its validated folders and stores."""

    def job_recovery(self, job_id, data):
        from .fleet_locks import FleetBusy, repo_lock
        action = data.get('action')
        if action not in {'archive', 'restore', 'export', 'recheck-post', 'retire-post'}:
            raise _failure('Choose Archive, Restore, Save results again, Check GitHub, or Retire this decision.', 400)
        with self._draft_action_lock(job_id):
            job = self.queue().get(job_id)
            if job is None:
                raise _failure('This task was not found. Refresh Tasks to choose a saved task.', 404)
            if action in {'archive', 'restore'}:
                return self._recover_job(job, action, data)
            try:
                with repo_lock(self.config().state_dir, job.repo_full_name,
                               exclusive=True, blocking=False):
                    return self._recover_job(job, action, data)
            except FleetBusy:
                raise _failure('An agent is still using this project. Your results are kept. Wait for that task to finish, then try this recovery again.') from None

    def _recover_job(self, job, action, data):
        if action in {'archive', 'restore'}:
            if job.state not in TERMINAL_STATES:
                raise _failure('This task still needs a decision or is working. Open its details and finish or stop it before archiving.')
            if action == 'archive':
                if not job.notes.get('archived_at'):
                    job.notes['archived_at'] = _now()
                    job.notes['archive_reason'] = str(data.get('reason') or 'Archived by a person from task history.')[:1000]
                message = 'Moved this task to History. Its results, errors, and GitHub changes are kept.'
            else:
                if job.notes.get('archived_at'):
                    job.notes.setdefault('archive_history', []).append({
                        'archived_at': job.notes.pop('archived_at'),
                        'reason': job.notes.pop('archive_reason', ''), 'restored_at': _now()})
                message = 'Returned this task to the working list. It has not been run again.'
            self.queue().update(job)
            return {'job': _public(job), 'message': message, 'hint': message,
                    'actions_available': ['open-task', 'history']}
        if job.state not in TERMINAL_STATES | {JobState.WAITING_ON_YOU}:
            raise _failure('This task is still working. Stop it or wait for it to finish before recovering its saved results.')
        if action == 'export':
            return self._recover_export(job, data)
        if (not job.notes.get('posting') and job.notes.get('phase') != 'posting-unconfirmed'
                and job.notes.get('publish_outcome') != 'uncertain'):
            raise _failure('This task has no unconfirmed post. Read its saved results or review its remaining drafts.')
        if action == 'recheck-post':
            return self._recheck_post(job)
        if data.get('confirmed') is not True:
            raise _failure('Open GitHub and inspect the intended change first. Then confirm that you want to retire this decision without posting it again.')
        outcome = data.get('outcome', 'unknown')
        if outcome not in {'posted', 'not-posted', 'unknown'}:
            raise _failure('Choose what you found on GitHub: the change is there, it is not there, or you still do not know.', 400)
        job.notes.setdefault('recovery_history', []).append({
            'action': 'retire-post', 'at': _now(), 'outcome': outcome,
            'reported_by': 'user', 'observed': False,
            'note': str(data.get('note') or '')[:2000],
            'posting_marker': job.notes.get('posting')})
        job.notes['phase'] = 'posting-retired'
        job.notes['post_recovery'] = {'outcome': outcome, 'reported_by': 'user',
                                      'observed': False, 'at': _now()}
        # Keep the posting claim as durable evidence. A stopped job cannot be
        # posted again through the draft endpoint, even after being restored.
        job.state = JobState.STOPPED
        job.error = ('This decision was retired without repeating the post. '
                     'The recorded GitHub outcome was reported by a person; Fatchin has not proved it.')
        self.queue().update(job)
        return {'job': _public(job), 'message': job.error, 'hint':
                'You can archive this task or download its record. Existing GitHub changes are kept.',
                'actions_available': ['archive', 'download-report']}

    def _recover_export(self, job, data):
        from .results import export_worktree, grant_member_access, write_report, EXPORT_LIMIT_BYTES
        owner = job.notes.get('member') or job.notes.get('account')
        account = data.get('account') or owner
        if owner and account != owner:
            raise _failure('Keep this task’s results in its original member account. Choose a folder belonging to ' + str(owner) + '.', 403)
        destination = self.destination({**data, 'account': account})
        home = self.member_home(account)
        worktree = job.notes.get('worktree_path')
        retained = None
        if worktree:
            try:
                retained = Path(worktree).resolve(strict=True)
                if not retained.is_relative_to(Path(self.config().worktree_root).resolve()):
                    raise _failure('The retained working copy is outside this worker’s scratch folder. Download the task record and ask the maintainer to recover that location.', 403)
            except FileNotFoundError:
                retained = None
        old_root = None
        original_output = job.notes.get('output_dir')
        if original_output:
            try:
                old_parent, _ = self.account_path(owner or account, original_output)
                candidate = old_parent / ('gha-worker-' + job.job_id)
                if not candidate.is_symlink() and candidate.is_dir():
                    old_root = candidate
            except Exception:
                # A missing old destination is exactly the case this route
                # repairs, but it must never be mistaken for available files.
                old_root = None
        if old_root is None and job.notes.get('artifact_dir'):
            try:
                candidate = Path(job.notes['artifact_dir'])
                resolved = candidate.resolve(strict=True)
                member_root = Path(home).resolve(strict=True)
                if (candidate.name == 'gha-worker-' + job.job_id and not candidate.is_symlink()
                        and resolved.is_relative_to(member_root) and resolved.is_dir()
                        and not any(part.startswith('.') for part in resolved.relative_to(member_root).parts)):
                    old_root = resolved
            except (OSError, ValueError):
                old_root = None
        expects_files = bool(job.notes.get('changed_files') or job.notes.get('exported_files') or job.notes.get('result_export_error'))
        if retained is None and old_root is None and expects_files:
            raise _failure('The saved task record is available, but its working files and old export could not be found. Download the report now. Ask the maintainer to recover the recorded working-copy location; rerunning would be new work.')
        recovery_parent = self._new_recovery_folder(destination, home)
        grant_member_access(recovery_parent, home)
        recovered = Job.from_json(job.to_json())
        recovered.notes.setdefault('export_history', []).append({
            'at': _now(), 'previous_output_dir': original_output,
            'previous_artifact_dir': job.notes.get('artifact_dir'),
            'selected_output_dir': destination, 'output_dir': str(recovery_parent),
            'action': 'export-only', 'model_calls': 0, 'remote_writes': 0})
        previous_skipped = recovered.notes.pop('recovery_copy_skipped', None)
        if previous_skipped is not None:
            recovered.notes['export_history'][-1]['previous_recovery_copy_skipped'] = previous_skipped
            if recovered.notes.get('result_export_error'):
                recovered.notes['export_history'][-1]['previous_result_export_error'] = recovered.notes['result_export_error']
        recovered.notes.update(output_dir=str(recovery_parent), member=account,
                               member_home=home, selected_output_dir=destination)
        try:
            if retained is not None:
                export_worktree(recovered, str(retained))
            elif old_root is not None:
                target = recovery_parent / ('gha-worker-' + job.job_id)
                target.mkdir(mode=0o2775)
                grant_member_access(target, home)
                total = 0
                skipped = []
                root_fd = os.open(old_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    for source in old_root.rglob('*'):
                        relative = source.relative_to(old_root)
                        if source.is_symlink() or any(part.startswith('.') for part in relative.parts):
                            skipped.append(str(relative))
                            continue
                        if not source.is_file():
                            continue
                        # Walk from an already-open directory so a concurrent
                        # symlink replacement cannot turn a saved-file read
                        # into a read outside this member's result tree.
                        directory_fd = os.dup(root_fd)
                        try:
                            for part in relative.parts[:-1]:
                                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                                os.close(directory_fd)
                                directory_fd = next_fd
                            source_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                            with os.fdopen(source_fd, 'rb') as handle:
                                metadata = os.fstat(handle.fileno())
                                if not stat.S_ISREG(metadata.st_mode):
                                    raise ValueError('A saved result changed into a non-file during recovery. The original record is kept.')
                                total += metadata.st_size
                                if total > EXPORT_LIMIT_BYTES:
                                    raise ValueError('The saved files exceed the 50 MB recovery limit. The original files are still kept.')
                                content = handle.read(EXPORT_LIMIT_BYTES + 1)
                                total += len(content) - metadata.st_size
                                if len(content) > EXPORT_LIMIT_BYTES or total > EXPORT_LIMIT_BYTES:
                                    raise ValueError('A result file grew beyond the recovery limit. The original files are still kept.')
                            from .results import write_file
                            write_file(target / relative, content)
                        finally:
                            os.close(directory_fd)
                finally:
                    os.close(root_fd)
                if skipped:
                    recovered.notes['recovery_copy_skipped'] = skipped
                recovered.notes['artifact_dir'] = str(target)
            recovered.notes.pop('result_export_error', None)
            if recovered.notes.get('recovery_copy_skipped'):
                recovered.notes['result_export_error'] = 'Some saved files were skipped because they were hidden or symbolic links. Read recovery_copy_skipped in the report; the earlier files remain in their original folder.'
            write_report(recovered)
        except Exception as exc:
            job.notes.setdefault('export_attempts', []).append({'at': _now(),
                'destination': str(recovery_parent), 'error': redact_text(str(exc))[:1000]})
            self.queue().update(job)
            raise _failure('These results could not be saved: ' + redact_text(str(exc))[:700] + ' The original task, working files, and previous exports are kept. Choose another folder or download the task report.') from None
        self.queue().update(recovered)
        return {'job': _public(recovered), 'message':
                'Saved the existing results in a new recovery folder. No model work or GitHub action was repeated.',
                'hint': 'Browse task files to inspect the saved copy. Earlier results remain in their original folders.',
                'actions_available': ['browse-task-files', 'download-report']}

    def _new_recovery_folder(self, destination, home):
        """Create a fresh child without following a swapped parent link."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open('/', flags)
        try:
            for component in Path(destination).parts[1:]:
                child = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            actual = Path(os.readlink(f'/proc/self/fd/{fd}'))
            member_root = Path(home).resolve(strict=True)
            if not actual.is_relative_to(member_root) or actual == member_root:
                raise _failure('The results folder moved outside the selected account. Choose another folder before saving these results.', 403)
            name = 'recovery-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
            os.mkdir(name, mode=0o2775, dir_fd=fd)
            child_fd = os.open(name, flags, dir_fd=fd)
            try:
                created = Path(os.readlink(f'/proc/self/fd/{child_fd}'))
                if not created.is_relative_to(member_root):
                    raise _failure('The results folder moved while saving. The task and original files are kept. Choose a stable folder and retry.', 403)
                return created
            finally:
                os.close(child_fd)
        finally:
            os.close(fd)

    def _recheck_post(self, job):
        marker = job.notes.get('posting')
        draft = next((d for d in job.notes.get('drafts', []) if d.get('name') == marker), {})
        repo = self.validate_repo(job.repo_full_name)
        evidence = []
        try:
            if job.kind.value == 'triage-issues':
                rows = self.gh.rest('GET', f'repos/{repo}/issues',
                                    body={'state': 'all', 'per_page': 100}, paginate=True) or []
                evidence = [{'url': r.get('html_url'), 'title': r.get('title'), 'number': r.get('number')}
                    for r in rows if not r.get('pull_request') and r.get('title') == draft.get('title')
                    and r.get('body') == draft.get('body')]
            elif job.kind.value == 'review-pr' and job.number:
                rows = self.gh.rest('GET', f'repos/{repo}/pulls/{job.number}/reviews', paginate=True) or []
                evidence = [{'url': r.get('html_url'), 'title': 'Matching review text', 'id': r.get('id')}
                            for r in rows if r.get('body') == draft.get('body')]
            else:
                # Other tasks may change branches, labels, or several remote
                # objects. A title match cannot prove those operations complete.
                evidence = [{'url': f'https://github.com/{repo}' + (f'/pull/{job.number}' if job.number else ''),
                             'title': 'Inspect this project on GitHub'}]
            message = ('Matching text exists on GitHub. Open the evidence to check whether it is this intended change.'
                       if evidence and job.kind.value in {'triage-issues', 'review-pr'} else
                       'Fatchin cannot prove whether this post completed. Open GitHub and inspect the intended change.')
            if not evidence:
                message = 'No exact matching text was found in this GitHub read. This does not prove that nothing was posted. Inspect GitHub before retiring the decision.'
        except Exception as exc:
            message = 'GitHub could not be checked: ' + redact_text(str(exc))[:500] + '. Your draft and posting record are kept. Check access or inspect GitHub directly.'
        record = {'at': _now(), 'message': message, 'evidence': evidence,
                  'outcome': 'unknown', 'observed': bool(evidence), 'remote_writes': 0}
        job.notes.setdefault('post_checks', []).append(record)
        self.queue().update(job)
        return {'job': _public(job), 'message': message, 'hint':
                'After inspecting GitHub, you can retire this decision without sending it again.',
                'evidence': evidence, 'outcome': 'unknown', 'actions_available': ['open-github', 'retire-post']}

    def watch_destinations(self, repo, account=None):
        """Offer only existing, writable member destinations; never choose one."""
        repo = self.validate_repo(repo)
        if account and account not in {a['account'] for a in self.accounts()}:
            from .web_control import ApiError
            raise ApiError('Choose your member account to see its saved folders.')
        items, seen = [], set()

        def offer(member, path, source):
            if not member or not path or (account and member != account):
                return
            from .web_control import ApiError
            try:
                destination = self.destination({'account': member, 'output_dir': path})
            except (ApiError, OSError):
                return  # A removed or inaccessible old folder is not a usable offer.
            key = (member, destination)
            if key not in seen:
                seen.add(key)
                items.append({'account': member, 'output_dir': destination, 'source': source})

        watched = self.watches().read()
        for meta in (watched.get(repo) or {}).values():
            offer(meta.get('member') or meta.get('account'), meta.get('output_dir'), 'Saved for this project’s watch')
            for previous in reversed(meta.get('watch_history') or []):
                old = previous.get('previous') or {}
                offer(old.get('member') or old.get('account'), old.get('output_dir'), 'Earlier setup for this project’s watch')
        for job in reversed(self.queue().all()):
            if job.repo_full_name == repo:
                notes = job.notes
                request = notes.get('request') or {}
                offer(notes.get('member') or notes.get('account') or request.get('account'),
                      notes.get('selected_output_dir') or notes.get('output_dir') or request.get('output_dir'),
                      'Saved for an earlier task in this project')
        for fleet in self.fleet_store().snapshot().get('fleets', []):
            if fleet.get('scope') == 'all' or repo in (fleet.get('repos') or []):
                offer(fleet.get('account'), fleet.get('output_dir'), 'Saved for this project’s fleet')
        for name, entries in watched.items():
            if name != repo:
                for meta in entries.values():
                    offer(meta.get('member') or meta.get('account'), meta.get('output_dir'), 'Used by the watch for ' + name)
        return {'repo': repo, 'items': items}

    def watch_health(self, rows):
        cfg = self.config()
        service = self.service()
        paused = not cfg.poll_repos or self.controls().read().get('polling_paused', False)
        configured = {r.full_name for r in cfg.repos}
        result = []
        for original in rows:
            row = copy.deepcopy(original)
            repo = row.get('repo') or row.get('repo_full_name')
            folder_error = ''
            if all(row.get(k) for k in ('member', 'member_home', 'output_dir')):
                try:
                    self.destination({'account': row['member'], 'output_dir': row['output_dir']})
                except Exception as exc:
                    folder_error = redact_text(str(exc))[:700]
            status = row.get('watch_status') or 'not-checked'
            if row.get('setup_required'):
                status, reason, step, actions = 'needs-folder', 'This project’s review watch is waiting for your setup. Automatic follow-up is off.', 'Finish watch setup: choose a saved results folder or browse for one, then decide whether to enable automatic feedback checks.', ['edit-watch']
            elif row.get('enabled') is False:
                status, reason, step, actions = 'disabled', 'This earlier watch is retained as history.', 'Edit this watch to enable it again, or keep its record in history.', ['edit-watch']
            elif not all(row.get(k) for k in ('member', 'member_home', 'output_dir')):
                status, reason, step, actions = 'needs-folder', 'This watch has no selected member results folder. No automatic work can start from it.', 'Choose your account and results folder to complete this watch.', ['edit-watch']
            elif folder_error:
                status, reason, step, actions = 'needs-folder', 'The chosen results folder cannot be used: ' + folder_error, 'Edit this watch and choose an existing writable folder in its member account.', ['edit-watch']
            elif repo not in configured:
                status, reason, step, actions = 'repository-removed', 'This project is no longer in the worker’s saved projects.', 'Add the project again, or remove this watch if you no longer need it.', ['add-repository', 'remove-watch']
            elif not service.get('active'):
                status, reason, step, actions = 'worker-stopped', 'The worker is stopped, so this watch is not checking GitHub.', 'Start the worker to continue checking feedback.', ['start-worker']
            elif paused:
                status, reason, step, actions = 'paused', 'Automatic feedback checks are paused.', 'Resume polling when you want this watch to check for feedback.', ['resume-polling']
            elif row.get('closed'):
                status, reason, step, actions = 'closed', 'This pull request is closed or merged. This watch is retained as history.', 'Open GitHub to read the result, or remove this watch.', ['open-github', 'remove-watch']
            elif row.get('last_error'):
                status, reason, step, actions = 'lookup-failed', str(row['last_error']), 'Run setup checks or open GitHub to check this project’s access. The next polling check will try again.', ['diagnostics', 'open-github']
            else:
                reason = row.get('status_reason') or row.get('hold_reason') or 'No successful feedback check has been recorded for this watch yet.'
                step = {'author-not-allowed': 'Only feedback from allowed GitHub users starts automatic work. Add that person in GitHub commands, or create an individual task.',
                        'daily-limit': 'Feedback is saved. Automatic work resumes after the daily reset shown here; you can choose a separate task if needed.',
                        'task-in-progress': 'A task for this pull request is already working. Open Tasks to follow it.',
                        'queued': 'A task was queued for this feedback. Open Tasks to follow it.'}.get(status, 'New eligible inline review feedback can start work automatically. Existing feedback is the starting point; choose New task to act on it now.')
                actions = ['new-task', 'command-settings'] if status == 'author-not-allowed' else ['open-tasks'] if status in {'task-in-progress', 'queued'} else ['edit-watch', 'new-task']
                if not row.get('last_success_at') and status == 'watching':
                    status = 'not-checked'
            row.update(health=status, status_reason=reason, next_step=step, actions_available=actions)
            row['feedback_policy'] = 'New inline review comments from allowed GitHub users can queue up to six automatic tasks per pull request per UTC day. Existing feedback is not replayed automatically.'
            result.append(row)
        return result

    def edit_watch(self, data):
        from .web_control import target_number
        if 'enabled' in data and type(data['enabled']) is not bool:
            raise _failure('Choose whether this watch should check new feedback. Its saved state has not changed.', 400)
        repo = self.validate_repo(data.get('repo'), configured=True)
        mode = data.get('mode', 'selected')
        if mode not in {'all', 'selected'}:
            raise _failure('Choose the saved watch to edit.', 400)
        key = '*' if mode == 'all' else str(target_number(data.get('number')))
        output = self.destination(data)
        new_mode = data.get('new_mode', mode)
        if new_mode not in {'all', 'selected'}:
            raise _failure('Choose one pull request or all open pull requests.', 400)
        new_key = '*' if new_mode == 'all' else str(target_number(data.get('new_number', data.get('number'))))
        changing = new_key != key
        if changing and data.get('confirmed_scope_change') is not True:
            raise _failure('Changing what this watch follows keeps its saved feedback positions. Confirm that new pull requests should start from their existing comments; use New task for earlier feedback.')
        new_starting = {}
        if new_mode == 'selected':
            target = self.target(repo, 'fix-pr', int(new_key))
            if str(target.get('state', '')).upper() != 'OPEN':
                raise _failure('Choose an open pull request, or keep the previous scope. Closed work remains in history.')
            if new_key not in self.watches().read().get(repo, {}):
                comments = self.gh.rest('GET', f'repos/{repo}/pulls/{new_key}/comments', paginate=True) or []
                new_starting = {'last_review_comment_id': max((int(c.get('id') or 0) for c in comments), default=0), 'authorized_at': _now()}
        member = data['account']
        metadata = {'output_dir': output, 'member': member,
                    'member_home': self.member_home(member), 'scope': new_mode,
                    'edited_at': _now(),
                    'watch_status': 'not-checked',
                    'status_reason': 'Watch settings were saved. The next polling check will inspect new feedback.'}
        if new_mode == 'selected':
            metadata['closed'] = False  # target() above freshly established OPEN
        store = self.watches()
        def update(value):
            entries = value.get(repo)
            if not isinstance(entries, dict) or key not in entries:
                raise _failure('This watch changed or was removed. Refresh Review watches before editing it.')
            previous = copy.deepcopy(entries[key])
            metadata['enabled'] = data.get('enabled', previous.get('enabled', True))
            if metadata['enabled'] is False:
                metadata.update(watch_status='disabled', status_reason='Watch settings were saved. This watch remains stopped until you choose to enable it.')
            target_meta = copy.deepcopy(entries.get(new_key, new_starting))
            if changing:
                target_meta.setdefault('watch_history', []).append({'at': _now(), 'previous_key': key, 'previous': previous})
                # Keep the old row as a disabled history record, never discard
                # its watermarks. The worker ignores enabled:false rows.
                entries[key]['enabled'] = False
                entries[key]['superseded_by'] = new_key
                if key == '*':
                    for number, child in entries.items():
                        if number != '*' and child.get('scope') == 'all':
                            child['enabled'] = False
                            child['superseded_by'] = new_key
            else:
                target_meta = previous
                target_meta.setdefault('watch_history', []).append({'at': _now(), 'previous': {k: copy.deepcopy(v) for k, v in previous.items() if k != 'watch_history'}})
            target_meta.update(metadata)
            target_meta.pop('setup_required', None)
            target_meta.pop('superseded_by', None)
            entries[new_key] = target_meta
            if new_key == '*':
                for number, child in entries.items():
                    if number != '*' and (child.get('scope') == 'all' or number == key):
                        child.update(metadata, scope='all')
                        child.pop('superseded_by', None)
            return True
        store.change(update)
        saved = store.read()[repo][new_key]
        return {'ok': True, 'watch': {**saved, 'repo': repo,
                    'mode': new_mode, 'number': None if new_key == '*' else int(new_key)},
                'message': 'Watch updated. Its earlier feedback positions and history are kept. Already queued tasks are unchanged.',
                'hint': ('This watch remains stopped. Edit it and enable it when you want new feedback to start automatic work.'
                         if saved.get('enabled') is False else 'New eligible feedback is handled while the worker and polling are running.'),
                'actions_available': ['open-watches']}

    def rules_inspections(self):
        return ControlStore(Path(self.config().state_dir) / 'rules-inspections.json').read()

    def inspect_rules(self, repo):
        repo = self.validate_repo(repo)
        checked = _now()
        try:
            rules = self.gh.rest('GET', f'repos/{repo}/rulesets', paginate=True)
            if not isinstance(rules, list):
                raise ValueError('GitHub did not return a readable rules list')
            record = {'repo': repo, 'state': 'verified', 'rulesets_readable': True,
                      'checked_at': checked, 'rules_count': len(rules),
                      'rules': [{'id': r.get('id'), 'name': r.get('name'), 'enforcement': r.get('enforcement'), 'target': r.get('target')} for r in rules],
                      'reason': f'GitHub returned {len(rules)} repository rule sets. Each task still checks the rules for the actual change before publishing.',
                      'next_step': 'Open the rules on GitHub to inspect them. A successful read does not mean a proposed change satisfies them.'}
        except Exception as exc:
            record = {'repo': repo, 'state': 'unavailable', 'rulesets_readable': False,
                      'checked_at': checked, 'rules': [],
                      'reason': 'GitHub could not return this project’s rules: ' + redact_text(str(exc))[:700],
                      'next_step': 'Open GitHub rules or check repository access. A plan limitation may prevent inspection; no publishing exception was changed.'}
        record['github_url'] = f'https://github.com/{repo}/settings/rules'
        record['actions_available'] = ['inspect-rules', 'open-github-rules', 'diagnostics']
        ControlStore(Path(self.config().state_dir) / 'rules-inspections.json').change(lambda value: value.update({repo: record}))
        return record

    def contextual_diagnostics(self):
        checks = []
        fleet = self.fleet_store().snapshot()
        unresolved = [i for i in fleet.get('items', []) if i.get('status') in {'blocked', 'failed'} and not i.get('archived_at')]
        checks.append({'name': 'Saved work needing attention', 'status': 'warn' if unresolved else 'pass',
                       'message': f'{len(unresolved)} fleet items need a decision. Stopped fleets remain in the saved work list.',
                       'action': 'Open Agent fleet, then Needs attention to read the reason and recovery choices.', 'action_id': 'open-fleet'})
        missing = []
        for repo, entries in self.watches().read().items():
            for number, meta in entries.items():
                if meta.get('enabled') is False or (number != '*' and meta.get('scope') == 'all'):
                    continue
                if not all(meta.get(k) for k in ('output_dir', 'member', 'member_home')):
                    missing.append(repo)
        checks.append({'name': 'Review watch destinations', 'status': 'warn' if missing else 'pass',
                       'message': f'{len(missing)} watches need a member results folder.' if missing else 'Every enabled watch has an explicitly selected member folder.',
                       'action': 'Open Review watches and choose Edit watch to select a folder.', 'action_id': 'open-watches'})
        failures = []
        for item in unresolved:
            for check in item.get('result', {}).get('checks') or []:
                if isinstance(check, dict) and (check.get('passed') is False or check.get('exit', check.get('exit_code', 0)) not in (None, 0)):
                    failures.append({'repo': item.get('repo'), 'item_id': item.get('id'),
                                     'command': check.get('command'), 'output': redact_text(str(check.get('output_tail') or check.get('output') or 'No output was recorded.'))[-4000:]})
        if failures:
            checks.append({'name': 'Project verification failures', 'status': 'warn',
                           'message': f'{len(failures)} recorded project commands failed. Read the output before retrying; connection checks do not run project tests.',
                           'action': 'Open the affected fleet item to inspect its command, update the check if needed, or open its project on GitHub.',
                           'action_id': 'open-fleet', 'details': failures[:10]})
        try:
            result = self.runner(['journalctl', '--user', '-u', 'gha-worker.service', '-n', '30', '--no-pager', '--output=cat'],
                                 capture_output=True, text=True, timeout=10)
            logs = redact_text(str(result.stdout or ''))[-6000:] if result.returncode == 0 else ''
            checks.append({'name': 'Recent worker messages', 'status': 'info',
                           'message': 'Recent worker messages are shown below. Messages may describe earlier work; their presence is not a fresh success check.' if logs else 'Recent worker messages could not be read from this service account.',
                           'details': logs, 'action': 'If starting the worker failed, use this message when asking the maintainer to repair the installed worker service.', 'action_id': 'worker-controls'})
        except Exception:
            checks.append({'name': 'Recent worker messages', 'status': 'warn',
                           'message': 'Recent worker messages could not be read. Existing work is kept.',
                           'action': 'Ask the maintainer to inspect the gha-worker user service and provide the recent failure message.', 'action_id': 'worker-controls'})
        return checks
