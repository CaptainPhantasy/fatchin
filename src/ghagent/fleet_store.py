"""Durable fleet queue. A single flock protects admission, never network I/O."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .control_store import atomic_json

MAX_WORKERS = 20
MAX_PER_PROVIDER = 10
MAX_PER_REPOSITORY = 4
MAX_ATTEMPTS = 3
ACTIVE = {'running'}
PENDING = {'queued', 'retrying'}
KINDS = {'review_pr', 'fix_comment', 'review_code', 'open_pr'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]


class FleetStateError(ValueError):
    pass


class FleetStore:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.path = self.root / 'state.json'

    @contextlib.contextmanager
    def _locked(self, write=False):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / 'state.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(self.path.read_text())
                except FileNotFoundError:
                    data = {'version': 1, 'fleets': {}, 'items': {}, 'workers': {},
                            'events': [], 'supervisor': {}}
                except (OSError, ValueError) as exc:
                    raise FleetStateError('Fleet state could not be read; the original file has been preserved.') from exc
                if (not isinstance(data, dict) or data.get('version') != 1
                        or any(not isinstance(data.get(k), dict) for k in ('fleets', 'items', 'workers', 'supervisor'))
                        or not isinstance(data.get('events'), list)):
                    raise FleetStateError('Fleet state is damaged or uses an unsupported version; it has been preserved.')
                yield data
                if write:
                    atomic_json(self.path, data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _event(data, message, item=None, slot=None):
        data['events'].append({'at': now(), 'message': message, 'item_id': item, 'slot': slot})
        data['events'] = data['events'][-500:]

    def internal(self):
        with self._locked() as data:
            return copy.deepcopy(data)

    def overview(self):
        """Bounded shared projection for every workspace screen, without task bodies."""
        with self._locked() as data:
            fleets = [f for f in data['fleets'].values() if f['status'] != 'stopped']
            ids = {f['id'] for f in fleets}
            repositories = {}
            configured, active = {'glm': 0, 'deepseek': 0}, {'glm': 0, 'deepseek': 0}
            for fleet in fleets:
                configured['glm'] += fleet.get('workers', 0)
                configured['deepseek'] += fleet.get('deepseek_workers', 0)
                for repo in fleet.get('repositories', []):
                    name = repo['repo']
                    status = 'watching' if fleet['status'] == 'running' else 'paused'
                    if name not in repositories or status == 'watching':
                        repositories[name] = {'repo': name, 'status': status,
                            'private': repo.get('private'), 'output_dir': fleet['output_dir'],
                            'account': fleet['account'], 'fleet_id': fleet['id']}
            items = [i for i in data['items'].values() if i['fleet_id'] in ids]
            counts, by_repo = {}, {}
            for item in items:
                status = item['status']
                counts[status] = counts.get(status, 0) + 1
                bucket = by_repo.setdefault(item['repo'], {})
                bucket[status] = bucket.get(status, 0) + 1
                if status == 'running':
                    provider = item.get('provider', 'glm')
                    if provider in active:
                        active[provider] += 1
            for name, repo in repositories.items():
                repo['tasks'] = by_repo.get(name, {})
            # Active and held work stays visible ahead of queued background files.
            items.sort(key=lambda i: i.get('updated_at') or i.get('created_at', ''), reverse=True)
            items.sort(key=lambda i: {'running': 0, 'blocked': 1, 'retrying': 2, 'done': 3}.get(i['status'], 4))
            public = [{k: copy.deepcopy(i[k]) for k in ('id', 'repo', 'kind', 'number', 'title',
                'url', 'status', 'stage', 'provider', 'slot', 'error', 'updated_at') if k in i}
                for i in items[:60]]
            return {'fleets': [{k: copy.deepcopy(f[k]) for k in ('id', 'account', 'status', 'scope',
                        'workers', 'deepseek_workers', 'output_dir', 'last_scan_at', 'error') if k in f}
                    for f in fleets],
                'repositories': sorted(repositories.values(), key=lambda r: r['repo'].casefold()),
                'repository_count': len(repositories),
                'configured_agents': {p: min(MAX_PER_PROVIDER, n) for p, n in configured.items()},
                'active_agents': active, 'max_workers_per_repository': MAX_PER_REPOSITORY,
                'task_counts': counts, 'recent_tasks': public,
                'events': copy.deepcopy(data['events'][-30:]),
                'supervisor': {k: copy.deepcopy(data['supervisor'][k]) for k in
                    ('status', 'heartbeat_at', 'last_scan_at', 'discovery_error', 'discovery_failed_repos')
                    if k in data['supervisor']}}

    @staticmethod
    def _in_scope(fleet, item):
        repo = str(item.get('repo', '')).casefold()
        if fleet.get('scope') == 'selected' and repo not in {str(r).casefold() for r in fleet.get('repos', [])}:
            return False
        if 'repositories' in fleet and repo not in {str(r['repo']).casefold() for r in fleet['repositories']}:
            return False
        return True

    def _reconcile_scope(self, data, fleet):
        for item in data['items'].values():
            if item['fleet_id'] != fleet['id']:
                continue
            if not self._in_scope(fleet, item):
                item['scope_excluded'] = True
                if item.get('readiness'):
                    item['readiness'].update(ready=False, reason='This repository is outside the current writable scope.')
                if item['status'] == 'running':
                    item['fenced'] = True
                elif item['status'] in PENDING:
                    item['status'] = 'blocked'
                if item['status'] in {'blocked', 'running'}:
                    item['error'] = ('This repository is outside the current writable scope. '
                                     'Saved write receipts are retained for reconciliation; no new write is allowed.')
            elif item.pop('scope_excluded', False) and item['status'] == 'blocked' and item.get('auto_recheck', True):
                # The same records, including any prepared write, are reconciled by the runner
                # only after the repository is explicitly in scope and writable again.
                item.update(status='queued', eligible_at=now(), error='', updated_at=now())
                self._event(data, 'Repository access restored; saved work will be reconciled before continuing.', item['id'])

    def configure(self, validated_data):
        value = copy.deepcopy(validated_data)
        for field in ('workers', 'deepseek_workers'):
            count = value.get(field, 10 if field == 'workers' else 0)
            if type(count) is not int or not 0 <= count <= MAX_PER_PROVIDER:
                raise ValueError('Choose between 0 and 10 agents for each provider.')
        if value.get('workers', 10) + value.get('deepseek_workers', 0) < 1:
            raise ValueError('Choose at least one agent.')
        enabled = value.get('enabled_providers', ['glm'])
        if value.get('deepseek_workers', 0) and 'deepseek' not in enabled:
            raise ValueError('DeepSeek is not available. Check its existing connection before starting.')
        if not value.get('account') or not value.get('output_dir') or type(value.get('github_id')) is not int:
            raise ValueError('Choose a member, output folder and connected GitHub account.')
        if value.get('scope', 'all') not in ('all', 'selected'):
            raise ValueError('Choose all repositories or a selection.')
        if value.get('scope') == 'selected' and not value.get('repos'):
            raise ValueError('Select at least one repository.')
        budget = value.get('max_calls_total', 0)
        if type(budget) is not int or budget < 0:
            raise ValueError('Choose a total model-call budget of zero or more.')
        dry_run = value.get('dry_run', False)
        if type(dry_run) is not bool:
            raise ValueError('Choose whether this fleet runs as a dry run.')
        with self._locked(True) as data:
            existing = next((f for f in data['fleets'].values()
                             if f['account'] == value['account'] and f['github_id'] == value['github_id']), None)
            if existing and existing['status'] != 'stopped':
                raise ValueError('This member already has a fleet. Pause or stop it before changing its settings.')
            if existing and any(i['fleet_id'] == existing['id'] and i['status'] == 'running' for i in data['items'].values()):
                raise ValueError('The previous agents are still stopping. Try again when they have stopped.')
            fleet_id = existing['id'] if existing else 'fleet_' + secrets.token_hex(12)
            fleet = {**value, 'id': fleet_id, 'workers': value.get('workers', 10), 'scope': value.get('scope', 'all'),
                     'deepseek_workers': value.get('deepseek_workers', 0), 'repos': value.get('repos', []), 'status': 'running', 'created_at': (existing or {}).get('created_at', now()),
                     'updated_at': now(), 'error': '', 'max_calls_total': budget,
                     'dry_run': dry_run, 'calls_used': 0, 'drained_at': '', 'empty_since': ''}
            if existing and 'repositories' in existing:
                fleet['repositories'] = copy.deepcopy(existing['repositories'])
            data['fleets'][fleet_id] = fleet
            if existing:
                selected_providers = [provider for provider, count in
                                      (('glm', fleet['workers']), ('deepseek', fleet['deepseek_workers'])) if count > 0]
                for item in data['items'].values():
                    previous_provider = item.get('provider')
                    if (item['fleet_id'] != fleet_id or item['status'] not in PENDING | {'blocked'}
                            or not previous_provider or previous_provider in selected_providers):
                        continue
                    replacement = selected_providers[0]
                    item.setdefault('provider_history', []).append({'at': now(), 'from': previous_provider,
                        'to': replacement, 'attempts': item.get('attempts', 0),
                        'reason': 'The member explicitly reconfigured the fleet provider pools.'})
                    item['provider'] = replacement
                    if item['status'] in PENDING:
                        item.update(status='queued', eligible_at=now(), error='')
                    item['history'].append({'at': now(), 'event': 'provider-reconfigured',
                                            'from': previous_provider, 'to': replacement})
                    self._event(data, 'Fleet settings moved the next fresh task from ' + previous_provider + ' to ' + replacement + '.', item['id'])
            self._reconcile_scope(data, fleet)
            self._event(data, f"Fleet started for {value['account']}.")
            return copy.deepcopy(fleet)

    def verification_changed(self, repo):
        """After a successful trusted settings save, retry relevant blocked work.

        This hook does not need prior discovery metadata: a fork's verification
        settings belong to its source repository, not the PR's parent repository.
        """
        normalized = str(repo).strip().casefold()
        if not normalized:
            raise ValueError('Choose the repository whose verification command changed.')
        requeued = []
        with self._locked(True) as data:
            for item in data['items'].values():
                source = str(item.get('source_repo') or item.get('repo') or '').casefold()
                correction = item.get('correction_pr') or {}
                exhausted_repairs = (item['kind'] == 'fix_comment' and item.get('phase') != 'resolve_thread'
                                     and int(correction.get('repair_round') or 0) >= 3)
                if (source != normalized or item['status'] != 'blocked' or exhausted_repairs
                        or item.get('auto_recheck') is False or (item.get('result') or {}).get('auto_recheck') is False
                        or not self._in_scope(data['fleets'][item['fleet_id']], item)):
                    continue
                item.update(status='queued', eligible_at=now(), error='', updated_at=now(),
                            retry_allowance=item.get('attempts', 0) + MAX_ATTEMPTS,
                            fault_allowance=item.get('fault_attempts', 0) + MAX_ATTEMPTS)
                item['history'].append({'at': now(), 'event': 'verification-changed', 'repo': str(repo)})
                self._event(data, 'Verification settings saved; this item will continue automatically.', item['id'])
                requeued.append(item['id'])
            data['supervisor']['refresh_requested'] = datetime.now(timezone.utc).isoformat()
        return {'repo': str(repo), 'requeued': requeued, 'count': len(requeued)}

    def control(self, action, fleet_id='', item_id=''):
        with self._locked(True) as data:
            if action == 'retry':
                item = data['items'].get(item_id)
                if not item or item['status'] not in {'blocked', 'dismissed'}:
                    raise ValueError('This item is not waiting for a retry.')
                if not self._in_scope(data['fleets'][item['fleet_id']], item):
                    raise ValueError('This repository is outside the current writable scope. Restore its scope and access before retrying.')
                # Explicit retry preserves total attempts and starts a new bounded allowance.
                item['retry_allowance'] = item.get('attempts', 0) + MAX_ATTEMPTS
                item['fault_allowance'] = item.get('fault_attempts', 0) + MAX_ATTEMPTS
                item.update(status='queued', error='', eligible_at=now(), updated_at=now())
                self._event(data, 'Retry requested; previous attempts and write receipts retained.', item_id)
            elif action == 'refresh':
                data['supervisor']['refresh_requested'] = now()
            else:
                fleet = data['fleets'].get(fleet_id)
                if not fleet:
                    raise ValueError('Fleet not found.')
                if action not in {'pause', 'resume', 'stop'}:
                    raise ValueError('Unknown fleet action.')
                fleet['status'] = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped'}[action]
                fleet['updated_at'] = now()
                if action == 'stop':
                    for item in data['items'].values():
                        if item['fleet_id'] == fleet_id and item['status'] == 'running':
                            item['fenced'] = True
                self._event(data, f"Fleet {fleet['status']}.")
            return {'ok': True, 'action': action}

    @staticmethod
    def _key(candidate):
        if candidate.get('dedup_key'):
            return str(candidate['dedup_key'])
        repo = candidate.get('repo_id') or candidate.get('repo', '').lower()
        if candidate['kind'] == 'fix_comment':
            revision = candidate.get('comment_revision') or candidate.get('updated_at') or candidate.get('body', '')
            return f"finding:{repo}:{candidate.get('comment_id') or candidate.get('fingerprint') or identity(candidate.get('body', ''))}:{identity(revision)}"
        if candidate['kind'] == 'review_pr':
            return f"pr:{repo}:{candidate.get('number')}:{candidate.get('head_sha')}"
        return f"code:{repo}:{candidate.get('path')}:{candidate.get('blob_sha') or candidate.get('head_sha')}"

    def _upsert(self, data, fleet_id, candidate):
        if candidate.get('kind') not in KINDS:
            raise ValueError('Unknown fleet item kind.')
        key = self._key(candidate)
        item_id = 'item_' + identity(key)
        item = data['items'].get(item_id)
        if item:
            # The same blob can move to a newer branch head before its first review.
            # Keep one work item but read it from the newest pinned snapshot.
            if (item['kind'] == 'review_code' and item['status'] not in {'running', 'done'}
                    and candidate.get('head_sha') != item.get('head_sha')):
                for field in ('head_sha', 'base_sha', 'source_ref', 'target_ref', 'source_repo', 'url'):
                    if field in candidate:
                        item[field] = copy.deepcopy(candidate[field])
                item.update(status='queued', eligible_at=now(), error='', retry_allowance=item['attempts'] + MAX_ATTEMPTS, fault_allowance=item.get('fault_attempts', 0) + MAX_ATTEMPTS)
            if (item['kind'] == 'fix_comment' and item['status'] in PENDING | {'blocked'}
                    and not item.get('mutation_intents') and item.get('auto_recheck', True)
                    and candidate.get('head_sha') != item.get('head_sha')):
                item.setdefault('original_head_sha', item.get('head_sha'))
                for field in ('head_sha', 'base_sha', 'source_ref', 'target_ref', 'source_repo',
                              'source_repo_id', 'target_repo', 'target_repo_id', 'correction_pr'):
                    if field in candidate:
                        item[field] = copy.deepcopy(candidate[field])
                item.update(status='queued', eligible_at=now(), error='', retry_allowance=item['attempts'] + MAX_ATTEMPTS, fault_allowance=item.get('fault_attempts', 0) + MAX_ATTEMPTS)
            # A stopped owner can hand unclaimed work to another explicitly started fleet.
            if (item['status'] != 'running' and data['fleets'][item['fleet_id']]['status'] == 'stopped'
                    and data['fleets'][fleet_id]['status'] == 'running'):
                item['fleet_id'] = fleet_id
            for field in ('title', 'url', 'readiness', 'remote_state', 'discovered_at', 'correction_pr', 'author_slot', 'thread_id'):
                if field in candidate:
                    item[field] = copy.deepcopy(candidate[field])
            return item
        item = {**copy.deepcopy(candidate), 'id': item_id, 'dedup_key': key, 'fleet_id': fleet_id,
                'kind': candidate['kind'], 'title': candidate.get('title') or str(candidate.get('body') or 'Review finding').splitlines()[0][:160], 'status': candidate.get('status', 'queued'), 'priority':
                {'fix_comment': 0, 'review_pr': 1, 'review_code': 2, 'open_pr': 3}[candidate['kind']],
                'created_at': candidate.get('created_at') or now(), 'updated_at': now(), 'slot': None,
                'attempts': 0, 'fault_attempts': 0, 'error': candidate.get('error', ''), 'result': {}, 'mutation_intents': [],
                'history': [], 'author_slot': candidate.get('author_slot'), 'number': candidate.get('number', 0)}
        data['items'][item_id] = item
        return item

    def upsert(self, fleet_id, candidates, **metadata):
        with self._locked(True) as data:
            if fleet_id not in data['fleets']:
                raise ValueError('Fleet not found.')
            fleet = data['fleets'][fleet_id]
            for candidate in candidates:
                self._upsert(data, fleet_id, candidate)
            fleet['last_scan_at'] = now()
            fleet['error'] = str(metadata.get('error') or '')[:500]
            if 'policy_skipped' in metadata:
                fleet['policy_skipped'] = list(metadata['policy_skipped'])[:100]
            if 'repositories' in metadata:
                fleet['repositories'] = copy.deepcopy(metadata['repositories'])
            if 'code_scan_heads' in metadata:
                fleet['code_scan_heads'] = copy.deepcopy(metadata['code_scan_heads'])
            # Readiness expires on every scan, including errors; absent evidence never means success.
            readiness = metadata.get('readiness', {})
            affected = metadata.get('scanned_repos')
            failed = set(metadata.get('failed_repos', []))
            for item in data['items'].values():
                if item['fleet_id'] != fleet_id:
                    continue
                if item['kind'] == 'review_pr' and (affected is None or item['repo'] in affected or item['repo'] in failed):
                    key = f"{item['repo']}#{item['number']}"
                    evidence = readiness.get(key)
                    item['readiness'] = copy.deepcopy(evidence or {'ready': False, 'reason': 'Current GitHub evidence unavailable.', 'checked_at': now()})
                    if evidence and evidence.get('head_sha') != item.get('head_sha'):
                        item['readiness']['ready'] = False
                        item['readiness']['reason'] = 'A newer commit needs a fresh review.'
                if (item['kind'] == 'fix_comment' and item['id'] in metadata.get('finalizations', {})
                        and not item.get('resolved_at') and item['status'] not in {'running', 'queued', 'retrying'}
                        and item.get('phase') != 'resolve_thread'):
                    item.update(phase='resolve_thread', merge_proof=copy.deepcopy(metadata['finalizations'][item['id']]),
                                status='queued', eligible_at=now(), error='', retry_allowance=item['attempts'] + MAX_ATTEMPTS, fault_allowance=item.get('fault_attempts', 0) + MAX_ATTEMPTS)
                if item['kind'] == 'fix_comment' and item['id'] in metadata.get('resolved_items', []):
                    item.update(status='done', resolved_at=now(), resolution='GitHub confirmed the correction merged or the original review thread resolved.')
            branch_heads = metadata.get('branch_heads', {})
            open_pr_heads = metadata.get('open_pr_heads', {})
            promoted = set(metadata.get('promoted_branches') or [])
            conditions = metadata.get('repository_conditions', {})
            for item in data['items'].values():
                if item['fleet_id'] != fleet_id:
                    continue
                if (item['kind'] == 'review_code' and item['status'] in PENDING
                        and not item.get('paths')):
                    item.update(status='dismissed',
                                error='Per-file review was replaced by commit-level review.')
                elif (item['kind'] == 'review_code' and item['status'] in PENDING
                        and f"{item.get('source_repo') or item['repo']}:{item.get('source_ref')}" in promoted):
                    item.update(status='dismissed',
                                error='This branch now has a pull request; review happens there.')
                if item['kind'] == 'review_pr' and item['repo'] in open_pr_heads and item['status'] in PENDING:
                    current_pr = open_pr_heads[item['repo']].get(str(item['number']))
                    if current_pr != item.get('head_sha'):
                        item.update(status='dismissed', error='This pull request closed or a newer commit replaced this review.')
                source = item.get('source_repo') or item.get('repo')
                ref = item.get('source_ref')
                current = branch_heads.get(source + ':' + str(ref))
                if (item['kind'] == 'open_pr' and item['status'] in PENDING
                        and not item.get('new_push')):
                    item.update(status='dismissed',
                                error='Branch promotion now requires a new push; existing branches were left alone.')
                if (item['kind'] == 'fix_comment' and current and current != item.get('head_sha')
                        and item['status'] in PENDING | {'blocked'} and not item.get('mutation_intents')
                        and item.get('auto_recheck', True)):
                    item.setdefault('original_head_sha', item.get('head_sha'))
                    item.update(head_sha=current, status='queued', eligible_at=now(), error='',
                                retry_allowance=item['attempts'] + MAX_ATTEMPTS, fault_allowance=item.get('fault_attempts', 0) + MAX_ATTEMPTS)
                if item['kind'] == 'review_code' and current and current != item.get('head_sha') and item['status'] in PENDING:
                    item.update(status='dismissed', error='A newer branch snapshot replaced this queued review.')
                condition = conditions.get(item.get('repo'))
                previous = item.get('condition_key')
                if condition:
                    item['condition_key'] = condition
                    if previous and previous != condition and item['status'] == 'blocked' and item.get('auto_recheck', True):
                        item.update(status='queued', eligible_at=now(), error='', retry_allowance=item['attempts'] + MAX_ATTEMPTS, fault_allowance=item.get('fault_attempts', 0) + MAX_ATTEMPTS)
                        self._event(data, 'Repository conditions changed; checking this item again automatically.', item['id'])
            self._reconcile_scope(data, fleet)
            if metadata.get('error'):
                self._event(data, 'Discovery needs attention: ' + fleet['error'])
            self._prune(data, fleet_id)

    def _valid(self, data, item_id, lease):
        item = data['items'].get(item_id)
        return bool(item and item.get('lease') == lease and item['status'] == 'running'
                    and not item.get('fenced') and data['fleets'][item['fleet_id']]['status'] != 'stopped'
                    and self._in_scope(data['fleets'][item['fleet_id']], item))

    def alive(self, item_id, lease):
        with self._locked() as data:
            return self._valid(data, item_id, lease)

    @staticmethod
    def _target(item):
        if item['kind'] == 'review_code':
            return (str(item.get('repo_id') or item['repo']),
                    'review-code:' + str(item.get('head_sha')) + ':' + str(item.get('batch', item.get('path'))))
        if item['kind'] == 'review_pr':
            return (str(item.get('repo_id') or item['repo']), 'review-pr:' + str(item.get('number')) + ':' + str(item.get('head_sha')))
        if item['kind'] == 'open_pr':
            return (str(item.get('repo_id') or item['repo']), 'open-pr:' + str(item.get('source_ref')))
        return (str(item.get('target_repo_id') or item.get('source_repo_id') or item.get('repo_id') or item.get('repo')),
                str(item.get('target_ref') or item.get('source_ref') or ('pr:' + str(item.get('number', 0)))))

    @staticmethod
    def _repositories(item):
        """Count both the PR project and its actual source fork, once each."""
        keys = {('id', item['repo_id']) if item.get('repo_id') is not None
                else ('name', str(item['repo']).casefold())}
        if item.get('source_repo_id') is not None:
            keys.add(('id', item['source_repo_id']))
        elif item.get('source_repo') and item['source_repo'].casefold() != item['repo'].casefold():
            keys.add(('name', item['source_repo'].casefold()))
        return keys

    def note_calls(self, item_id, lease, total):
        """Count model calls as they happen, not only when the item finishes."""
        try:
            total = int(total)
        except (TypeError, ValueError):
            return
        if total < 0:
            return
        with self._locked(True) as data:
            item = data['items'].get(item_id)
            if not item or item.get('lease') != lease:
                return
            previous = int(item.get('calls_counted') or 0)
            if total <= previous:
                return
            fleet = data['fleets'].get(item['fleet_id'])
            if fleet is not None:
                fleet['calls_used'] = int(fleet.get('calls_used') or 0) + (total - previous)
            item['calls_counted'] = total

    def reserve_publication(self, fleet_id, repo, cap):
        """Reserve one public write for today; a soft cap shared by all slots."""
        with self._locked(True) as data:
            if fleet_id not in data['fleets']:
                raise ValueError('Fleet not found.')
            day = now()[:10]
            counts = data.setdefault('publications', {})
            for older in [d for d in counts if d < day]:
                counts.pop(older, None)
            today = counts.setdefault(day, {})
            if int(today.get(repo, 0)) >= max(0, int(cap)):
                return False
            today[repo] = int(today.get(repo, 0)) + 1
            return True

    def evaluate_drain(self, fleet_id):
        """Record a fleet as caught up after two quiet minutes, once."""
        with self._locked(True) as data:
            fleet = data['fleets'].get(fleet_id)
            if not fleet:
                return None
            busy = any(item['fleet_id'] == fleet_id
                       and item['status'] in {'running', 'queued', 'retrying'}
                       for item in data['items'].values())
            if busy:
                fleet.pop('empty_since', None)
                fleet.pop('drained_at', None)
                return None
            since = fleet.get('empty_since')
            if not since:
                fleet['empty_since'] = now()
                return None
            try:
                elapsed = (datetime.fromisoformat(now())
                           - datetime.fromisoformat(since)).total_seconds()
            except ValueError:
                fleet['empty_since'] = now()
                return None
            if elapsed >= 120 and not fleet.get('drained_at'):
                fleet['drained_at'] = now()
                fleet['drained_counts'] = {
                    status: sum(1 for i in data['items'].values()
                                if i['fleet_id'] == fleet_id and i['status'] == status)
                    for status in ('done', 'blocked', 'dismissed')}
                self._event(data, 'Fleet caught up: no queued or running work remains.')
            return fleet.get('drained_at')

    @staticmethod
    def _prune(data, fleet_id, keep=2000):
        terminal = [i for i in data['items'].values()
                    if i['fleet_id'] == fleet_id and i['status'] in {'done', 'dismissed'}]
        if len(terminal) <= keep:
            return
        terminal.sort(key=lambda i: i.get('updated_at') or '')
        for item in terminal[:len(terminal) - keep]:
            data['items'].pop(item['id'], None)

    def claim(self, slot):
        if type(slot) is not int or not 1 <= slot <= MAX_WORKERS:
            raise ValueError('Invalid agent slot.')
        with self._locked(True) as data:
            provider = 'glm' if slot <= 10 else 'deepseek'
            active = [i for i in data['items'].values() if i['status'] == 'running']
            if len(active) >= MAX_WORKERS or any(i['slot'] == slot for i in active):
                return None
            limits = data.get('rate_limits', {})
            if any(limits.get(key, {}).get('until', '') > now() for key in (provider, 'github')):
                return None
            targets = {self._target(i) for i in active}
            repository_load = {}
            for running in active:
                for repo in self._repositories(running):
                    repository_load[repo] = repository_load.get(repo, 0) + 1
            candidates = []
            for item in data['items'].values():
                fleet = data['fleets'][item['fleet_id']]
                if fleet.get('max_calls_total') and int(fleet.get('calls_used') or 0) >= int(fleet['max_calls_total']):
                    if fleet['status'] == 'running':
                        fleet['status'] = 'paused'
                        fleet['error'] = 'The run budget is exhausted. Resume the fleet to continue.'
                        self._event(data, 'Run budget reached; no new work was assigned.')
                    continue
                if (item['status'] not in PENDING or fleet['status'] != 'running'
                        or not self._in_scope(fleet, item)
                        or item.get('eligible_at', '') > now() or fleet.get('error')
                        or (item.get('provider') and item['provider'] != provider)
                        or self._target(item) in targets
                        or any(repository_load.get(repo, 0) >= MAX_PER_REPOSITORY for repo in self._repositories(item))
                        or sum(i['fleet_id'] == fleet['id'] and i.get('provider', 'glm') == provider for i in active) >= fleet.get('workers' if provider == 'glm' else 'deepseek_workers', 0)):
                    continue
                if item.get('author_slot') == slot:
                    continue
                candidates.append(item)
            # Stable newest-created ordering, with comments always ahead of PRs, then background code.
            candidates.sort(key=lambda i: (i.get('created_at', ''), i['id']), reverse=True)
            candidates.sort(key=lambda i: i['priority'])
            if not candidates:
                return None
            item = candidates[0]
            item.update(status='running', provider=provider, lease=secrets.token_hex(24), fenced=False, slot=slot,
                        started_at=now(), heartbeat_at=now(), stage='Starting', updated_at=now())
            item['attempts'] += 1
            item['history'].append({'at': now(), 'event': 'claimed', 'slot': slot, 'attempt': item['attempts']})
            data['workers'][str(slot)] = {'slot': slot, 'fleet_id': item['fleet_id'], 'item_id': item['id'],
                'status': 'running', 'provider': provider, 'stage': 'Starting', 'started_at': now(), 'heartbeat_at': now(),
                'restarts': max(0, item['attempts'] - 1)}
            self._event(data, f"Agent {slot} started {item['kind'].replace('_', ' ')}.", item['id'], slot)
            return {**copy.deepcopy(item), 'fleet': copy.deepcopy(data['fleets'][item['fleet_id']])}

    def fence(self, item_id, lease, reason='Agent reset requested.'):
        with self._locked(True) as data:
            item = data['items'].get(item_id)
            if item and item.get('lease') == lease and item['status'] == 'running':
                item['fenced'] = True
                item['reset_reason'] = str(reason)[:300]
                return True
            return False

    def heartbeat(self, item_id, lease, stage, **fields):
        with self._locked(True) as data:
            if not self._valid(data, item_id, lease):
                return False
            item = data['items'][item_id]
            item.update(heartbeat_at=now(), stage=str(stage)[:160])
            # Runtime process records are set by supervisor, never public request data.
            for key in ('process', 'prepared_commit_sha', 'model_calls', 'target_ref', 'target_repo_id'):
                if key in fields:
                    item[key] = copy.deepcopy(fields[key])
            data['workers'][str(item['slot'])].update(heartbeat_at=now(), stage=item['stage'])
            return True

    def intent(self, item_id, lease, kind, payload):
        with self._locked(True) as data:
            if not self._valid(data, item_id, lease):
                raise FleetStateError('This agent has stopped; no new write was admitted.')
            item = data['items'][item_id]
            fleet = data['fleets'][item['fleet_id']]
            payload = copy.deepcopy(payload)
            payload.setdefault('github_id', fleet['github_id'])
            payload.setdefault('repo_id', item.get('target_repo_id') or item.get('repo_id'))
            payload.setdefault('host', fleet.get('github_host', 'github.com'))
            if (payload['github_id'] != fleet['github_id']
                    or payload['host'] != fleet.get('github_host', 'github.com')):
                raise FleetStateError('Write identity does not match the fleet.')
            operation_id = 'op_' + identity([item_id, kind, payload])
            existing = next((op for op in item['mutation_intents'] if op['id'] == operation_id), None)
            if existing:
                return copy.deepcopy(existing)
            operation = {'id': operation_id, 'kind': kind, 'payload': payload, 'state': 'prepared', 'created_at': now()}
            item['mutation_intents'].append(operation)
            self._event(data, 'Write recorded before dispatch: ' + str(kind), item_id, item['slot'])
            return copy.deepcopy(operation)

    def receipt(self, item_id, lease, operation_id, result):
        with self._locked(True) as data:
            item = data['items'].get(item_id)
            # A stopped child may record an already admitted write, but no successor lease may do so.
            if not item or item.get('lease') != lease or item['status'] != 'running':
                raise FleetStateError('The agent lease is no longer current.')
            operation = next((op for op in item['mutation_intents'] if op['id'] == operation_id), None)
            if not operation:
                raise FleetStateError('No admitted write exists for this receipt.')
            operation.update(state='confirmed', result=copy.deepcopy(result), confirmed_at=now())

    def _release(self, data, item):
        worker = data['workers'].get(str(item.get('slot')))
        if worker and worker.get('item_id') == item['id']:
            worker.update(status='idle', stage='Watching for the next item', item_id=None)
        item.pop('lease', None)
        item.pop('process', None)
        item['updated_at'] = now()

    def finish(self, item_id, lease, result):
        with self._locked(True) as data:
            item = data['items'].get(item_id)
            if not item or item.get('lease') != lease or item['status'] != 'running':
                raise FleetStateError('The agent lease is no longer current.')
            item['result'] = copy.deepcopy(result)
            fleet = data['fleets'].get(item['fleet_id']) or {}
            try:
                counted = int(item.get('calls_counted') or 0)
                final = int(result.get('model_calls') or 0)
                if final > counted:
                    fleet['calls_used'] = int(fleet.get('calls_used') or 0) + (final - counted)
                item['calls_counted'] = max(counted, final)
            except (TypeError, ValueError):
                pass
            if isinstance(result.get('readiness'), dict):
                item['readiness'] = copy.deepcopy(result['readiness'])
                if item['readiness'].get('head_sha') != item.get('head_sha'):
                    item['readiness']['ready'] = False
            item['auto_recheck'] = result.get('auto_recheck', True)
            if result.get('resolved') is True:
                item['resolved_at'] = now()
                item['resolution'] = 'Correction confirmed merged and its review thread resolved.'
            status = result.get('status', 'done')
            if status not in {'done', 'dismissed', 'blocked', 'retrying', 'ready'}:
                status = 'blocked'
            if status == 'ready':
                status = 'done'  # Discovery alone can project ready with current authoritative evidence.
            if status == 'retrying':
                self._retry(data, item, str(result.get('summary') or 'Temporary failure.'), result.get('retry_after'), result.get('retry_kind'), result.get('retry_scope'))
            else:
                item.update(status=status, error=str(result.get('error') or '')[:500])
            item['history'].append({'at': now(), 'event': item['status'], 'slot': item['slot']})
            if result.get('fix_pr'):
                item['fix_pr'] = copy.deepcopy(result['fix_pr'])
                item['fix_pr'].setdefault('author_slot', item['slot'])
                item['fix_pr'].setdefault('root_finding_id', item.get('root_finding_id') or item['id'])
                correction = item['fix_pr']
                if (result.get('resolved') is not True and correction.get('head_sha')
                        and correction.get('branch') and correction.get('target_ref')):
                    # Publishing a correction is itself the trigger. Do not wait
                    # for a large account scan before its independent review.
                    self._upsert(data, item['fleet_id'], {'kind': 'review_pr',
                        'repo': correction['repo'], 'repo_id': correction.get('repo_id'),
                        'number': correction['number'], 'head_sha': correction['head_sha'],
                        'base_sha': item.get('head_sha'), 'base_ref': correction['target_ref'],
                        'source_repo': correction['repo'], 'source_repo_id': correction.get('repo_id'),
                        'source_ref': correction['branch'], 'target_repo': correction['repo'],
                        'target_repo_id': correction.get('repo_id'), 'target_ref': correction['branch'],
                        'title': 'Review correction: ' + item['title'], 'url': correction.get('url'),
                        'created_at': now(), 'correction_pr': copy.deepcopy(correction),
                        'author_slot': item['slot']})
                for review in data['items'].values():
                    if (review['kind'] == 'review_pr' and review['repo'] == item['fix_pr'].get('repo')
                            and review['number'] == item['fix_pr'].get('number')):
                        review['correction_pr'] = copy.deepcopy(item['fix_pr'])
                        review['author_slot'] = item['slot']
            for index, finding in enumerate(result.get('findings') or []):
                inherited = {k: copy.deepcopy(item[k]) for k in ('repo', 'repo_id', 'number', 'head_sha', 'base_sha',
                    'source_repo', 'source_repo_id', 'source_ref', 'target_repo', 'target_repo_id', 'target_ref', 'correction_pr', 'condition_key') if k in item}
                candidate = {**inherited, **copy.deepcopy(finding), 'kind': 'fix_comment', 'created_at': now(),
                             'author_slot': item['slot'], 'generated_by': item_id, 'trusted_provenance': True}
                candidate['fingerprint'] = finding.get('fingerprint') or identity([item.get('repo_id') or item['repo'], finding.get('body'), finding.get('path'), finding.get('line')])
                candidate['comment_revision'] = finding.get('comment_revision') or item.get('head_sha', '')
                candidate['dedup_key'] = f"generated:{item.get('repo_id') or item['repo']}:{candidate['fingerprint']}"
                self._upsert(data, item['fleet_id'], candidate)
            self._release(data, item)
            self._event(data, str(result.get('summary') or item['status'])[:400], item_id, item['slot'])

    def _retry(self, data, item, reason, retry_after=None, retry_kind=None, retry_scope=None):
        if retry_kind == 'rate_limit':
            seconds = max(5, min(86400, int(retry_after or 900)))
            until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')
            item.update(status='retrying', error=reason[:500], eligible_at=until, retry_kind='rate_limit')
            key = 'github' if retry_scope == 'github' else item.get('provider', 'glm')
            limits = data.setdefault('rate_limits', {})
            if until > limits.get(key, {}).get('until', ''):
                limits[key] = {'until': until, 'reason': 'Waiting for the service rate limit to reset automatically.'}
            return
        item['fault_attempts'] = item.get('fault_attempts', 0) + 1
        item.pop('retry_kind', None)
        if (retry_kind == 'service_unavailable'
                and item['fault_attempts'] >= item.get('fault_allowance', MAX_ATTEMPTS)):
            seconds = max(900, min(86400, int(retry_after or 900)))
            until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')
            item.update(status='retrying', error=reason[:500], eligible_at=until, retry_kind='service_unavailable',
                        fault_allowance=item['fault_attempts'] + MAX_ATTEMPTS)
            key = 'github' if retry_scope == 'github' else item.get('provider', 'glm')
            limits = data.setdefault('rate_limits', {})
            if until > limits.get(key, {}).get('until', ''):
                limits[key] = {'until': until, 'reason': 'Service unavailable. Retrying automatically after a cooldown.'}
        elif item['fault_attempts'] >= item.get('fault_allowance', MAX_ATTEMPTS):
            item.update(status='blocked', error=f'{reason} Automatic attempts exhausted; inspect the item and choose Retry.')
        else:
            seconds = max(5, min(3600, int(retry_after or 15 * (2 ** max(0, item['fault_attempts'] - 1)))))
            item.update(status='retrying', error=reason[:500], eligible_at=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds'))

    def recover(self, item_id, lease, reason):
        """Supervisor-only: call after proving the owned child process group is gone."""
        with self._locked(True) as data:
            item = data['items'].get(item_id)
            if not item or item.get('lease') != lease or item['status'] != 'running':
                return
            if data['fleets'][item['fleet_id']]['status'] == 'stopped':
                item.update(status='blocked', error='Stopped. Any admitted write will be reconciled before retry.')
            else:
                self._retry(data, item, str(reason))
            item['history'].append({'at': now(), 'event': 'recovered', 'reason': str(reason)[:500]})
            self._release(data, item)
            self._event(data, 'Agent reset automatically: ' + str(reason)[:300], item_id, item['slot'])

    def rate_limit(self, scope, seconds):
        seconds = max(5, min(86400, int(seconds or 900)))
        until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')
        with self._locked(True) as data:
            limits = data.setdefault('rate_limits', {})
            if until > limits.get(scope, {}).get('until', ''):
                limits[scope] = {'until': until, 'reason': 'Waiting for the service rate limit to reset automatically.'}
            if scope == 'github':
                data['supervisor']['discovery_retry_at'] = until
        return until

    def supervisor_status(self, **fields):
        with self._locked(True) as data:
            data['supervisor'].update(copy.deepcopy(fields), heartbeat_at=now())

    def snapshot(self):
        data = self.internal()
        items = []
        for item in data['items'].values():
            public = {k: v for k, v in item.items() if k not in {'lease', 'process', 'fenced', 'mutation_intents'}}
            public['operations'] = [{'id': op['id'], 'kind': op['kind'], 'state': op['state']} for op in item.get('mutation_intents', [])]
            items.append(public)
        open_comments = [i for i in items if i['kind'] == 'fix_comment' and not i.get('resolved_at') and i['status'] != 'dismissed']
        ready = []
        for item in items:
            if (item['kind'] == 'review_pr' and item['status'] == 'done' and item.get('readiness', {}).get('ready')
                    and item['readiness'].get('head_sha') == item.get('head_sha')
                    and not any(c['repo'] == item['repo'] and c.get('number') == item['number'] for c in open_comments)):
                ready.append(item)
        return {'version': 1, 'max_workers': MAX_WORKERS, 'max_workers_per_provider': MAX_PER_PROVIDER, 'fleets': list(data['fleets'].values()),
                'workers': list(data['workers'].values()), 'items': items, 'ready': ready,
                'comments': open_comments, 'rate_limits': data.get('rate_limits', {}), 'events': data['events'], 'supervisor': data['supervisor']}
