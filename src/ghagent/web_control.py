"""Graphical controls over the existing worker; no second coding engine."""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from .config_loader import DESTINATION_KEYS, load_config
from .control_store import ControlStore, WatchStore, AuthorityStore, atomic_json, atomic_bytes, restore_optional, backup
from .github_client import GitHubClient, redact_text, find_gh, build_env
from .interfaces import Config, Job, JobKind, JobState, ModelConfig, RepoConfig
from .queue import JobQueue, TERMINAL_STATES
from .task_write_guard import GuardedGitHub, ensure_not_stopped
from .worker import make_dedup_key, new_job_id
from . import tasks, __version__
from .recovery_control import RecoveryControls

_VERSION_FILE = Path(__file__).resolve().parents[2] / 'VERSION'


def _read_version() -> str:
    """Read the single version file; never hardcode a version beside this."""
    try:
        text = _VERSION_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return __version__
    return text or __version__


def _build_stamp() -> str:
    """Short commit plus a dirty marker: proves which build is actually running."""
    root = _VERSION_FILE.parent
    try:
        sha = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short=7', 'HEAD'],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    if not sha:
        return 'unknown'
    return sha + ('-dirty' if dirty else '')


VERSION = _read_version()
BUILD = _build_stamp()
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
DESTINATION_ERROR = 'Error: No destination folder specified. Supply an output path within your user space.'


class ApiError(Exception):
    def __init__(self, message, status=400, *, hint="", actions=None):
        super().__init__(message)
        self.status = status
        self.hint = hint
        self.actions = list(actions or [])


def iso():
    return datetime.now(timezone.utc).isoformat()


def target_number(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,10}', value) and int(value) > 0:
        return int(value)
    raise ApiError('Choose a positive whole issue or pull request number.')


def toml_document(data):
    """Preserve every supported field, including nested fleet policy values."""
    from datetime import date, time as datetime_time

    def quote_key(item):
        if not isinstance(item, str):
            raise ApiError('A configuration field name must be text.')
        return json.dumps(item, ensure_ascii=False)

    def value(item):
        if isinstance(item, bool):
            return 'true' if item else 'false'
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, (int, float)):
            return str(item)
        if isinstance(item, (datetime, date, datetime_time)):
            return item.isoformat()
        if isinstance(item, list):
            return '[' + ', '.join(value(v) for v in item) + ']'
        if isinstance(item, dict):
            return '{' + ', '.join(f'{quote_key(k)} = {value(v)}' for k, v in item.items()) + '}'
        raise ApiError('An unsupported configuration value was supplied.')
    lines = ['# gha-worker configuration. Edited by the graphical interface.']
    for key, item in data.items():
        if key not in {'repos', 'model'} or (key == 'repos' and item == []):
            lines.append(f'{quote_key(key)} = {value(item)}')
    for repo in data.get('repos', []):
        lines.append('\n[[repos]]')
        lines.extend(f'{quote_key(key)} = {value(item)}' for key, item in repo.items())
    if 'model' in data:
        lines.append('\n[model]')
        lines.extend(f'{quote_key(key)} = {value(item)}' for key, item in data['model'].items())
    return '\n'.join(lines) + '\n'


class WebControl(RecoveryControls):
    def __init__(self, config_path, env_path=None, *, gh=None, runner=subprocess.run, accounts=None):
        self.config_path = Path(config_path).expanduser()
        self.env_path = Path(env_path or '~/.config/gha-worker/env').expanduser()
        cfg = load_config(self.config_path)
        def github_run(argv, input=None):
            from .config_loader import load_env_file
            env = build_env(self.config().github_host)
            saved = load_env_file(self.env_path)
            for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
                if saved.get(key):
                    env[key] = saved[key]
            return subprocess.run([find_gh(), *argv], input=input, capture_output=True, text=True,
                                  timeout=20, env=env)
        self.gh = gh or GitHubClient(host=cfg.github_host, api_version=cfg.rest_api_version, runner=github_run)
        self.runner = runner
        self._accounts = accounts
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._connection = {'status': 'idle', 'verification_uri': 'https://github.com/login/device'}
        self._connection_lock = threading.Lock()
        self._learning_engine = None
        self._learning_lock = threading.Lock()

    def fleet_store(self):
        from .fleet_store import FleetStore
        return FleetStore(Path(self.config().state_dir) / 'fleet-v1')

    def fleet_overview(self):
        cfg = self.config()
        overview = self.fleet_store().overview()
        overview['usage'] = {}
        for provider in ('glm', 'deepseek'):
            usage = ControlStore(Path(cfg.state_dir) / 'fleet-v1' / f'{provider}-model-call-counts.json').read()
            overview['usage'][provider] = usage.get('count', 0) if usage.get('date') == datetime.now(timezone.utc).date().isoformat() else 0
        return overview

    def fleet_state(self):
        from .config_loader import load_env_file
        from .fleet_providers import provider_status
        cfg = self.config()
        state = self.fleet_store().snapshot()
        state['github'] = self.github_status()
        state['providers'] = provider_status(cfg, {**os.environ, **load_env_file(self.env_path)})
        state['max_workers'] = 20
        state['max_workers_per_provider'] = 10
        state['accounts'] = self.accounts()
        state['model'] = {'model_id': cfg.model.model_id}
        state['usage'] = {}
        for provider in ('glm', 'deepseek'):
            record = ControlStore(Path(cfg.state_dir) / 'fleet-v1' / f'{provider}-model-call-counts.json').read()
            state['usage'][provider] = {
                'date': record.get('date'),
                'calls_today': record.get('count', 0) if record.get('date') == datetime.now(timezone.utc).date().isoformat() else 0,
            }
        return state

    def fleet_item(self, item_id):
        item = next((row for row in self.fleet_state()['items'] if row['id'] == item_id), None)
        if not item:
            raise ApiError('This saved fleet item was not found. Open fleet history to choose another item.', 404, actions=['fleet'])
        fleet = self.fleet_store().internal()['fleets'].get(item['fleet_id'], {})
        return {'item': item, 'fleet': fleet}

    def repository_access(self, repo):
        repo = self.validate_repo(repo)
        checked = iso()
        try:
            record = self.gh.rest('GET', f'repos/{repo}')
            permissions = record.get('permissions') or {}
            can_write = permissions.get('push') is True or permissions.get('admin') is True
            return {'repo': repo, 'checked_at': checked, 'can_write': can_write,
                    'message': 'GitHub confirms write access for the connected account.' if can_write else 'GitHub did not confirm write access for the connected account.',
                    'next_step': 'Review the fleet’s selected projects before retrying; this check does not add the project to its scope.' if can_write else 'Ask this project’s owner to grant the connected GitHub account write access, or archive the old item if it no longer needs work.',
                    'github_url': f'https://github.com/{repo}'}
        except Exception as exc:
            return {'repo': repo, 'checked_at': checked, 'can_write': False,
                    'message': 'GitHub could not confirm access: ' + redact_text(str(exc))[:700],
                    'next_step': 'Open this project on GitHub and check which account has access. Reconnect that account in Settings, or ask the project owner for access. Your saved work is kept.',
                    'github_url': f'https://github.com/{repo}'}

    def fleet_files(self, item_id):
        saved = self.fleet_item(item_id)
        item, fleet = saved['item'], saved['fleet']
        report = item.get('result', {}).get('report_path')
        if not report:
            return {'items': [], 'message': 'No separate report file was saved for this attempt. The complete saved item is available with Download report.'}
        parent, _ = self.account_path(fleet.get('account'), fleet.get('output_dir'))
        path = Path(report)
        if path.is_symlink() or not path.resolve().is_relative_to(parent.resolve()) or not path.is_file():
            raise ApiError('The old report file is unavailable. Download the saved item record; its checks and history are still available.', 404)
        content = path.read_bytes()[:100001]
        return {'items': [{'name': path.name, 'path': str(path)}], 'content': redact_text(content[:100000].decode('utf-8', errors='replace')), 'truncated': len(content) > 100000}

    def fleet_action(self, data):
        from .config_loader import load_env_file
        from .fleet_providers import provider_status
        action = data.get('action')
        if action == 'configure_checks':
            return self.fleet_configure_checks(data)
        if action not in {'start', 'pause', 'resume', 'stop', 'retry', 'refresh', 'archive', 'restore', 'authorize_budget'}:
            raise ApiError('Choose Start, Pause, Resume, Stop, Retry, or Refresh for the agent fleet.')
        cfg = self.config()
        store = self.fleet_store()
        try:
            if action == 'start':
                # Bind destinations and identity on the server. Neither a
                # browser-supplied login nor a model suggestion grants access.
                output = self.destination(data)
                identity = self.gh.rest('GET', 'user')
                if type(identity.get('id')) is not int or identity['id'] < 1 or not identity.get('login'):
                    raise ApiError('Connect your GitHub account before starting agents.', 409)
                workers, deepseek_workers = data.get('workers', 10), data.get('deepseek_workers', 10)
                if any(type(n) is not int or not 0 <= n <= 10 for n in (workers, deepseek_workers)) or not workers + deepseek_workers:
                    raise ApiError('Choose zero to ten agents for each model, with at least one agent in total.')
                scope = data.get('scope', 'all')
                repos = data.get('repos', [])
                if scope not in {'all', 'selected'} or not isinstance(repos, list) or len(repos) > 500:
                    raise ApiError('Choose all repositories or a list of repositories.')
                repos = list(dict.fromkeys(self.validate_repo(repo) for repo in repos))
                if scope == 'selected' and not repos:
                    raise ApiError('Select at least one repository, or choose All repositories.')
                providers = provider_status(cfg, {**os.environ, **load_env_file(self.env_path)})
                enabled = [p['id'] for p in providers if p['available']]
                for name, count in (('glm', workers), ('deepseek', deepseek_workers)):
                    if count and name not in enabled:
                        raise ApiError(next(p['error'] for p in providers if p['id'] == name), 409)
                fleet = store.configure({
                    'account': data['account'], 'output_dir': output,
                    'member_home': self.member_home(data['account']),
                    'github_id': identity['id'], 'github_login': identity['login'],
                    'github_host': cfg.github_host, 'scope': scope,
                    'repos': repos if scope == 'selected' else [],
                    'workers': workers, 'deepseek_workers': deepseek_workers,
                    'enabled_providers': enabled,
                    'dry_run': data.get('dry_run') is True,
                    'max_calls_total': int(data.get('max_calls_total')
                                           or cfg.max_fleet_calls_total or 0),
                })
                fleet_id = fleet['id']
            else:
                fleet_id = data.get('fleet_id', '')
                item_id = data.get('item_id', '')
                if not isinstance(fleet_id, str) or not isinstance(item_id, str):
                    raise ApiError('Choose a saved fleet or work item.')
                if action in {'resume', 'retry', 'authorize_budget'}:
                    identity = self.gh.rest('GET', 'user')
                    internal = store.internal()
                    fleet = internal.get('fleets', {}).get(fleet_id)
                    if not fleet and item_id:
                        item = internal.get('items', {}).get(item_id, {})
                        fleet = internal.get('fleets', {}).get(item.get('fleet_id'))
                        if fleet:
                            fleet_id = fleet['id']
                    if fleet and identity.get('id') != fleet.get('github_id'):
                        raise ApiError('This fleet belongs to a different connected GitHub account. Reconnect that account before resuming.', 409)
                outcome = store.control(action, fleet_id=fleet_id, item_id=item_id, additional_calls=data.get('additional_calls'), reason=str(data.get('reason') or '')[:1000])
            if action in {'start', 'resume', 'retry', 'authorize_budget'} and not self.service()['active']:
                result = self.runner(['systemctl', '--user', 'start', 'gha-worker.service'],
                                     capture_output=True, text=True, timeout=30)
                if result.returncode:
                    if fleet_id:
                        store.control('pause', fleet_id=fleet_id)
                    raise ApiError('The worker service could not start. Your fleet is saved and paused. Run Connection checks to see the service problem.', 503)
        except ValueError as exc:
            raise ApiError(str(exc), 400) from None
        reply = self.fleet_state()
        if action != 'start' and outcome.get('message'):
            reply['hint'] = outcome['message']
        return reply

    def fleet_configure_checks(self, data):
        """Save a missing project check in the same view, without a reset."""
        repo = self.validate_repo(data.get('repo'))
        checks = data.get('checks')
        if (not isinstance(checks, list) or not 1 <= len(checks) <= 12 or
                any(not isinstance(c, str) or not c.strip() or len(c) > 1000 or '\x00' in c for c in checks)):
            raise ApiError('Enter one or more commands that test this project, one per line.')
        metadata = self.gh.rest('GET', f'repos/{repo}')
        if not (metadata.get('permissions') or {}).get('push'):
            raise ApiError('The connected GitHub account needs write access to configure this project.', 403)
        base = data.get('base_branch') or metadata.get('default_branch')
        if not isinstance(base, str) or not base or base.startswith('-') or any(c.isspace() for c in base):
            raise ApiError('Choose this project’s default branch.')
        # Resolve the supplied ref before saving it; no mutation is sent.
        self.gh.rest('GET', f'repos/{repo}/branches/' + quote(base, safe=''))
        def update(raw):
            repos = raw.setdefault('repos', [])
            existing = next((r for r in repos if f'{r["owner"]}/{r["name"]}'.casefold() == repo.casefold()), None)
            if existing:
                existing.update(checks=[c.strip() for c in checks], base_branch=base)
            else:
                owner, name = repo.split('/')
                repos.append({'owner': owner, 'name': name, 'base_branch': base,
                              'checks': [c.strip() for c in checks]})
        # change_config compares revisions under its own cross-process lock;
        # concurrent edits are preserved and a real conflict is surfaced.
        self.change_config({'revision': self.revision()}, update)
        self.fleet_store().verification_changed(repo)
        return self.fleet_state()

    def explore_catalog(self):
        from .explore import Explorer
        return Explorer(self.gh).catalog()

    def explore(self, params):
        from .explore import Explorer
        self.validate_repo(params.get('repo'))
        try:
            return Explorer(self.gh).inspect(**{
                key: params.get(key, '') for key in
                ('repo', 'view', 'ref', 'path', 'base', 'head', 'commit')
                if params.get(key) is not None and params.get(key) != ''})
        except ValueError as exc:
            raise ApiError(str(exc)) from None

    @contextlib.contextmanager
    def _learning_model(self, scope):
        from dataclasses import replace
        from .config_loader import load_env_file
        from .model_client import ModelClient
        from .wiring import make_bearer
        cfg = self.config()
        bearer = make_bearer(cfg, load_env_file(self.env_path))
        try:
            client = ModelClient(
                replace(cfg.model, max_tokens=min(cfg.model.max_tokens, 1200)),
                bearer, job_scope=scope,
                day_counter_path=str(Path(cfg.state_dir) / 'model-call-counts.json'),
                max_attempts=1)
            yield client
        finally:
            close = getattr(bearer, 'close', None)
            if callable(close):
                close()

    def learning(self):
        from .learning import LearningEngine
        with self._learning_lock:
            if self._learning_engine is None:
                cfg = self.config()
                self._learning_engine = LearningEngine(
                    Path(cfg.state_dir) / 'learning-v1',
                    Path(cfg.worktree_root) / 'learning-scratch-v1',
                    model_factory=self._learning_model,
                    explorer=self.explore,
                    export_callback=self._export_lesson)
            return self._learning_engine

    def learning_get(self, session_id):
        from .learning import LearningError
        try:
            return {'session': self.learning().get(session_id)}
        except LearningError as exc:
            raise ApiError(str(exc), exc.status) from None

    def learning_action(self, data):
        from .learning import LearningError
        try:
            if data.get('action') == 'start':
                data = dict(data)
                data['output_dir'] = self.destination(data)
                return {'session': self.learning().start(data)}
            return {'session': self.learning().action(data)}
        except LearningError as exc:
            raise ApiError(str(exc), exc.status) from None

    def _export_lesson(self, session, transcript):
        """Create a new transcript under the session's bound member folder."""
        from uuid import uuid4
        from .results import grant_member_access
        parent = Path(self.destination(session))
        session_id = session['id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{12,100}', session_id):
            raise ApiError('The learning session identifier is invalid.', 409)
        # Open each path component without following links, then keep all
        # creation relative to those directory handles. A validated pathname
        # alone can be replaced between validation and the first write.
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open('/', directory_flags)
        directory = None
        try:
            for component in parent.parts[1:]:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            folder_name = 'fatchin-learn-' + session_id
            try:
                os.mkdir(folder_name, mode=0o2775, dir_fd=parent_fd)
            except FileExistsError:
                pass
            directory = os.open(folder_name, directory_flags, dir_fd=parent_fd)
            actual_root = Path(os.readlink(f'/proc/{os.getpid()}/fd/{directory}'))
            home = Path(self.member_home(session['account'])).resolve()
            if not actual_root.is_relative_to(home):
                raise ApiError('Your results folder moved outside your account. '
                               'Choose a folder in your account before exporting.', 409)
            grant_member_access(f'/proc/{os.getpid()}/fd/{directory}', home)
            name = 'transcript-' + uuid4().hex + '.md'
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o664, dir_fd=directory)
            with os.fdopen(fd, 'w') as handle:
                handle.write(transcript)
                handle.flush()
                os.fsync(handle.fileno())
            path = Path(os.readlink(f'/proc/{os.getpid()}/fd/{directory}')) / name
        except OSError as exc:
            if exc.errno in {20, 40}:
                raise ApiError('The results folder changed or contains a symbolic link. '
                               'Choose a regular folder inside your account and retry.', 409) from None
            raise
        finally:
            if directory is not None:
                os.close(directory)
            os.close(parent_fd)
        return {'path': str(path), 'message': 'Lesson transcript saved in your selected folder.'}

    def config(self):
        cfg = load_config(self.config_path)
        if isinstance(getattr(self, 'gh', None), GitHubClient):
            self.gh.api_version = cfg.rest_api_version
        return cfg

    def raw_config(self):
        return tomllib.loads(self.config_path.read_text())

    def revision(self):
        return hashlib.sha256(self.config_path.read_bytes()).hexdigest()

    def queue(self):
        return JobQueue(self.config().state_dir)

    def watches(self):
        return WatchStore(Path(self.config().state_dir) / 'watched.json')

    def controls(self):
        return ControlStore(Path(self.config().state_dir) / 'gui-control.json')

    def authority(self):
        return AuthorityStore(Path(self.config().state_dir) / 'authority.json')

    def task_types(self):
        items = tasks.catalog()
        for item in items:
            task = tasks.get(item['id'])
            item['allows_closed'] = bool(task and task.allows_closed)
        return {'items': items}

    def authority_view(self):
        store = self.authority()
        modes = {repo.full_name: store.mode(repo.full_name)
                 for repo in self.config().repos}
        return {'modes': modes, 'grants': store.grants(),
                'explanation': 'Drafts wait for you unless you choose to post '
                               'directly for a repository. Merging, deleting, '
                               'closing and releasing always ask.'}

    def set_authority(self, data):
        repo = self.validate_repo(data.get('repo'), configured=True)
        mode = data.get('mode')
        if mode not in AuthorityStore.MODES:
            raise ApiError('Choose “Ask me every time” or “Post directly for this repository”.')
        self.authority().set_mode(repo, mode, updated_by=str(data.get('source') or 'gui'))
        return {'repo': repo, 'mode': mode}

    def fence(self):
        """What could bill you or land unverified, per repository, in plain terms."""
        cfg = self.config()
        watched = self.watches().read()
        watch_rows = []
        for repo_name, entries in watched.items():
            if not isinstance(entries, dict):
                raise ApiError('Review watch state cannot be read. Existing records have been preserved.', 503)
            for number, meta in entries.items():
                if meta.get('scope') == 'all' and number != '*':
                    continue
                watch_rows.append({**meta, 'repo': repo_name,
                                   'number': int(number) if number != '*' else None,
                                   'mode': 'all' if number == '*' else 'selected'})
        healthy_watches = self.watch_health(watch_rows)
        active_states = {'watching', 'not-checked', 'queued', 'task-in-progress'}
        watch_labels = {'watching': 'Watching for new feedback', 'not-checked': 'Waiting for first feedback check',
                        'queued': 'Feedback task queued', 'task-in-progress': 'Feedback task in progress',
                        'disabled': 'Watch stopped', 'needs-folder': 'Needs a results folder',
                        'repository-removed': 'Project needs adding again', 'worker-stopped': 'Worker stopped',
                        'paused': 'Feedback checks paused', 'closed': 'Pull request closed',
                        'lookup-failed': 'GitHub check failed', 'daily-limit': 'Daily follow-up limit reached',
                        'author-not-allowed': 'Feedback needs an allowed person'}
        authority = self.authority()
        usage = ControlStore(Path(cfg.state_dir) / 'model-call-counts.json').read()
        calls_today = usage.get('count', 0) if usage.get('date') == datetime.now(timezone.utc).date().isoformat() else 0
        cancelled = 0
        for job in self.queue().all():
            cancelled += len(job.notes.get('cancelled_copilot_runs') or [])
        repos = []
        for repo in cfg.repos:
            rows = [row for row in healthy_watches if row['repo'] == repo.full_name]
            active = [row for row in rows if row['health'] in active_states]
            selected = (active or rows or [None])[0]
            watch_state = watch_labels.get(selected['health'], 'Watch needs checking') if selected else 'No review watch selected'
            watch_next_step = selected['next_step'] if selected else 'Open Review watches to choose a pull request and results folder for automatic follow-up.'
            if active and len(active) < len(rows):
                watch_state += '; some watches need attention'
                watch_next_step = 'Open Review watches to follow active checks and repair the saved watches that need attention.'
            repos.append({'repo': repo.full_name, 'configured': True,
                          'checks_configured': len(repo.checks),
                          'protection': 'safe' if repo.checks else 'no-checks',
                          'watches_armed': bool(active), 'watch_state': watch_state, 'watch_next_step': watch_next_step,
                          'post_authority': authority.mode(repo.full_name),
                          'rulesets_readable': 'unknown', 'rules_inspection': self.rules_inspection(repo.full_name),
                          'base_branch': repo.base_branch})
        coverage = self.fleet_overview()
        known = {r['repo'].casefold(): r for r in repos}
        for watched_repo in coverage['repositories']:
            name = watched_repo['repo']
            if name.casefold() in known:
                known[name.casefold()]['fleet_managed'] = True
                known[name.casefold()]['watches_armed'] |= watched_repo['status'] == 'watching'
                if watched_repo['status'] == 'watching':
                    known[name.casefold()].update(watch_state='Fleet watching for new work',
                        watch_next_step='Open Fleet to inspect its saved scope and latest checks.')
            else:
                repos.append({'repo': name, 'configured': False, 'checks_configured': 0, 'protection': 'automatic',
                    'watches_armed': watched_repo['status'] == 'watching', 'fleet_managed': True,
                    'watch_state': 'Fleet watching for new work' if watched_repo['status'] == 'watching' else 'Fleet ' + str(watched_repo['status']).replace('-', ' '),
                    'watch_next_step': 'Open Fleet to inspect its saved scope, status, and next step.',
                    'post_authority': authority.mode(name), 'rulesets_readable': 'unknown', 'rules_inspection': self.rules_inspection(name), 'base_branch': ''})
        return {'repos': repos, 'copilot_runs_cancelled': cancelled,
                'model_calls_today': calls_today + sum(coverage['usage'].values()),
                'actions_runs_started_by_worker': 0,
                'note': 'The worker starts no GitHub Actions runs and cancels '
                        'Copilot reviews on its own pushes. Missing verification '
                        'commands are shown as “no-checks”.'}

    def digest(self, limit=200):
        """Every saved outcome stays reachable, even when its worker is stopped."""
        jobs = [json.loads(j.to_json()) for j in self.queue().all() if not j.notes.get('archived_at')]
        done, waits = [], []
        for job in jobs:
            row = {'job_id': job['job_id'], 'repo': job['repo_full_name'], 'number': job['number'],
                   'task': job['kind'], 'at': job['updated_at'], 'text': job['notes'].get('agent_summary') or job.get('error') or job['notes'].get('title') or 'Open this task to read its result.'}
            if job['state'] == 'done':
                done.append(row)
            elif job['state'] in {'failed', 'stopped'}:
                row['text'] = job.get('error') or 'This task stopped before finishing. Open it to read the evidence, correct its request, or archive it.'
                waits.append(row)
        coverage = self.fleet_overview()
        path = Path(self.config().state_dir) / 'digest.jsonl'
        historical_watches = []
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    waits.append({'text': 'An older update could not be read. Its original record is kept; open setup checks for help.', 'at': '', 'kind': 'unreadable-history'})
                    continue
                if event.get('job_id') in {j['job_id'] for j in jobs}:
                    continue
                if event.get('kind') == 'done':
                    done.append(event)
                elif event.get('kind') in {'waiting-on-you', 'stale-pr'}:
                    waits.append(event)
                elif event.get('kind') == 'watched':
                    historical_watches.append(event)
        for item in self.fleet_store().snapshot().get('items', []):
            if item.get('archived_at'):
                continue
            row = {'fleet_item': True, 'item_id': item['id'], 'fleet_id': item['fleet_id'],
                   'task': item['kind'], 'repo': item['repo'], 'number': item.get('number'),
                   'at': item.get('updated_at'), 'text': item.get('error') or item.get('result', {}).get('summary') or item.get('title'),
                   'recovery': item.get('recovery', {})}
            from .recovery import failed_checks
            if item['status'] == 'done' and not failed_checks(item):
                done.append(row)
            elif item['status'] in {'blocked', 'failed'} or failed_checks(item):
                waits.append(row)
        watches = self.watch_health([dict(meta, repo=repo, number=None if key == '*' else int(key), mode='all' if key == '*' else 'selected')
            for repo, entries in self.watches().read().items() for key, meta in entries.items()
            if key == '*' or meta.get('scope') != 'all'])
        watched_names = {w['repo'].casefold() for w in watches}
        watches.extend(dict(row, mode='all', text=('This fleet is following ' if row['status'] == 'watching' else 'This fleet is paused for ') + row['repo'] + '. Open Agent fleet to read its current work.')
                       for row in coverage['repositories'] if row['repo'].casefold() not in watched_names)
        return {'done': sorted(done, key=lambda x: x.get('at') or '', reverse=True)[:limit],
                'waiting_on_you': {'events': sorted(waits, key=lambda x: x.get('at') or '', reverse=True),
                                   'drafts': [j for j in jobs if j['state'] == 'waiting-on-you']},
                'watched': historical_watches + [dict(w, text=w.get('text') or w.get('status_reason') or w.get('next_step') or 'Open Review watches to see what this watch is doing.') for w in watches],
                'hint': '', 'actions_available': ['jobs', 'fleet', 'watches']}

    def accounts(self):
        if self._accounts is not None:
            return self._accounts
        result = []
        for entry in pwd.getpwall():
            home = Path(entry.pw_dir)
            if entry.pw_uid >= 1000 and home.parent == Path('/home') and home.is_dir():
                result.append({'account': home.name, 'label': home.name, 'path': str(home), 'uid': entry.pw_uid})
        return sorted(result, key=lambda x: x['account'])

    def account_path(self, account, value=None, *, writable=False):
        entry = next((a for a in self.accounts() if a['account'] == account), None)
        if not entry:
            raise ApiError('Choose your account before selecting a folder.')
        root = Path(entry['path']).resolve()
        path = Path(value or root).expanduser().resolve()
        if not path.is_relative_to(root):
            raise ApiError('Choose a folder inside your own account space.', 403)
        if not path.is_dir():
            raise ApiError('This folder does not exist. Create it with New folder, or choose an existing folder.')
        if any(part.startswith('.') for part in path.relative_to(root).parts):
            raise ApiError('Choose a regular project folder, not a hidden configuration folder.', 403)
        if writable and not os.access(path, os.W_OK | os.X_OK):
            raise ApiError('The worker cannot write here. Choose a writable folder, or grant the company group read and write access to this folder.', 403)
        return path, root

    def destination(self, data):
        if not data.get('output_dir'):
            raise ApiError(DESTINATION_ERROR, 400)
        path, root = self.account_path(data.get('account'), data['output_dir'], writable=True)
        if path == root:
            raise ApiError('Choose or create a project folder inside your account, rather than your whole home folder.')
        return str(path)

    def member_home(self, account):
        return next(a['path'] for a in self.accounts() if a['account'] == account)

    def folders(self, account=None, path=None):
        accounts = self.accounts()
        account = account or (accounts[0]['account'] if accounts else '')
        folder, root = self.account_path(account, path)
        try:
            items = [{'name': p.name, 'path': str(p), 'is_dir': True} for p in folder.iterdir()
                     if not p.name.startswith('.') and p.is_dir() and not p.is_symlink() and os.access(p, os.R_OK | os.X_OK)]
        except PermissionError:
            raise ApiError('This folder is not readable by the worker. Choose a folder you have granted access to.', 403) from None
        return {'path': str(folder), 'parent': str(folder.parent) if folder != root else None,
                'roots': accounts, 'items': sorted(items, key=lambda p: p['name'].casefold()),
                'writable': os.access(folder, os.W_OK | os.X_OK)}

    def create_folder(self, data):
        parent, _ = self.account_path(data.get('account'), data.get('parent'), writable=True)
        name = str(data.get('name', '')).strip()
        if not name or len(name) > 100 or name in {'.', '..'} or '/' in name or '\\' in name or name.startswith('.'):
            raise ApiError('Use a folder name of 1–100 characters without slashes or a leading dot.')
        target = parent / name
        try:
            target.mkdir(mode=0o2775)
        except FileExistsError:
            raise ApiError('That folder already exists. Choose it, or use another name.', 409) from None
        except PermissionError:
            raise ApiError('The worker cannot create a folder here. Grant the company group access or choose another location.', 403) from None
        from .results import grant_member_access
        try:
            grant_member_access(target, self.member_home(data['account']))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            raise ApiError('The folder was created, but its member access could not be set. Choose a folder on this server’s regular filesystem, or ask the maintainer to check folder permissions.', 503) from None
        return {'path': str(target)}

    def cached(self, key, action, ttl=30):
        with self._cache_lock:
            old = self._cache.get(key)
            if old and old[0] > time.monotonic():
                return old[1]
        value = action()
        with self._cache_lock:
            self._cache[key] = (time.monotonic() + ttl, value)
        return value

    def github_status(self):
        try:
            user = self.cached('github-user', lambda: self.gh.rest('GET', 'user'), ttl=60)
            return {'configured': True, 'login': user['login'], 'id': user['id']}
        except Exception:
            return {'configured': False, 'login': '', 'error': 'GitHub is unavailable or needs connecting. Run connection checks for details.'}

    def service(self):
        result = self.runner(['systemctl', '--user', 'show', 'gha-worker.service', '--property=ActiveState,SubState,MainPID'], capture_output=True, text=True, timeout=10)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        service = {'state': values.get('ActiveState', 'unknown'), 'active': values.get('ActiveState') == 'active',
                   'pid': int(values.get('MainPID', '0') or 0), 'substate': values.get('SubState', '')}
        execution = ControlStore(Path(self.config().state_dir) / 'execution.json').read()
        if service['active'] and execution.get('pid') == service['pid']:
            service.update(current_job_id=execution.get('job_id'), member=execution.get('member'))
        return service

    def require_idle(self):
        if self.service().get('current_job_id') or any(j.state not in TERMINAL_STATES | {JobState.WAITING_ON_YOU} for j in self.queue().all()):
            raise ApiError('The worker has queued or running tasks. Stop those tasks first and wait for the current step to finish.', 409)
        from .fleet_store import FleetStore
        fleet = FleetStore(Path(self.config().state_dir) / 'fleet-v1').snapshot()
        if any(item.get('status') == 'running' for item in fleet.get('items', [])):
            raise ApiError('Agents are still working. Pause the fleet and let its current tasks finish, or use Stop in the Fleet page.', 409)

    def state(self):
        cfg = self.config()
        service = self.service()
        health = ControlStore(Path(cfg.state_dir) / 'health.json').read()
        control = self.controls().read()
        service.update({'poll_interval_seconds': cfg.poll_interval_seconds, 'last_poll_iso': health.get('last_poll_iso'),
                        'polling_enabled': cfg.poll_repos and not control.get('polling_paused', False),
                        'restart_required': control.get('config_revision') == self.revision()})
        watches = []
        for repo, entries in self.watches().read().items():
            if not isinstance(entries, dict):
                raise ApiError('Review watch state cannot be read. Restore the last known good file; it has not been changed.', 503)
            for number, meta in entries.items():
                if meta.get('scope') == 'all' and number != '*':
                    continue
                watches.append({**meta, 'repo': repo, 'number': int(number) if number != '*' else None,
                                'mode': 'all' if number == '*' else 'selected'})
        watches = self.watch_health(watches)
        jobs = [json.loads(j.to_json()) for j in self.queue().all()]
        jobs.reverse()
        usage = ControlStore(Path(cfg.state_dir) / 'model-call-counts.json').read()
        if usage:
            if type(usage.get('count')) is not int or usage['count'] < 0:
                raise ApiError('Model usage records cannot be read. Existing data has been preserved.', 503)
            try:
                datetime.fromisoformat(usage['date'])
            except (KeyError, TypeError, ValueError):
                raise ApiError('Model usage records have an invalid date. Existing data has been preserved.', 503) from None
        calls_today = usage.get('count', 0) if usage.get('date') == datetime.now(timezone.utc).date().isoformat() else 0
        fleet_summary = self.fleet_overview()
        from .config_loader import load_env_file
        from .fleet_providers import provider_status
        providers = provider_status(cfg, {**os.environ, **load_env_file(self.env_path)})
        return {'version': VERSION, 'build': BUILD, 'service': service, 'github': self.github_status(), 'model': dataclasses.asdict(cfg.model),
                'providers': providers,
                'fleet_summary': fleet_summary,
                'summary': {'jobs_open': sum(j['state'] not in {s.value for s in TERMINAL_STATES} for j in jobs),
                            'jobs_done_today': health.get('jobs_done_today', 0), 'model_calls_today': calls_today},
                'repos': [{**dataclasses.asdict(r), 'full_name': r.full_name} for r in cfg.repos], 'jobs': jobs, 'watches': watches,
                'settings': dataclasses.asdict(cfg), 'accounts': self.accounts(), 'config_revision': self.revision(),
                'capabilities': {'all_pr_watches': True, 'folder_browser': True, 'github_device_flow': True, 'provider_check': True}}

    def validate_repo(self, value, *, configured=False):
        if not isinstance(value, str) or not REPO.fullmatch(value):
            raise ApiError('Choose a repository in owner/name format.')
        if configured and value not in {r.full_name for r in self.config().repos}:
            raise ApiError('Add this repository on the Repositories page first. Then restart the worker to apply it.')
        return value

    def targets(self, repo, kind, state='open'):
        repo = self.validate_repo(repo)
        task = tasks.get(str(kind or ''))
        if task is None:
            raise ApiError('Choose a task type from the list first.')
        if task.takes == 'repo':
            raise ApiError('This task runs on the whole repository; there is no item to choose.')
        collection = 'pulls' if task.takes == 'pr' else 'issues'
        if state not in {'open', 'closed', 'all'}:
            raise ApiError('Choose open, closed, or all items.')
        result = self.gh.rest('GET', f'repos/{repo}/{collection}', body={'state': state, 'per_page': 100}, paginate=True) or []
        return {'items': [dict(number=r['number'], title=r['title'], state=r['state'], url=r['html_url'],
                               headRefName=r.get('head', {}).get('ref')) for r in result if collection == 'pulls' or 'pull_request' not in r]}

    def target(self, repo, kind, number):
        repo = self.validate_repo(repo)
        task = tasks.get(str(kind or ''))
        if task is None or task.takes == 'repo':
            raise ApiError('This task does not choose an issue or pull request.')
        number = target_number(number)
        item = self.gh.pr_view(repo, number) if task.takes == 'pr' else self.gh.issue_view(repo, number)
        if not item or (task.takes == 'issue' and item.get('pull_request')):
            raise ApiError('That item is not the requested type.')
        return item

    def repositories(self, query=''):
        result = self.cached('repositories', lambda: self.gh.rest('GET', 'user/repos', body={'per_page': 100, 'sort': 'updated', 'affiliation': 'owner,collaborator,organization_member'}, paginate=True), ttl=60)
        query = str(query).casefold()[:100]
        items = [{'full_name': r['full_name'], 'name': r['name'], 'description': r.get('description') or '',
                  'default_branch': r['default_branch'], 'private': r['private']} for r in result if query in r['full_name'].casefold()]
        return {'items': items[:200]}

    def branches(self, repo):
        repo = self.validate_repo(repo)
        result = self.gh.rest('GET', f'repos/{repo}/branches', body={'per_page': 100}, paginate=True) or []
        return {'items': [{'name': r['name']} for r in result]}

    def enqueue(self, data):
        with ControlStore(self.config_path.with_suffix('.edit')).lock():
            return self._enqueue(data)

    def _enqueue(self, data):
        if 'accept_no_checks' in data and type(data['accept_no_checks']) is not bool:
            raise ApiError('The one-task check exception must be explicitly selected or cleared.', 400)
        task = tasks.get(str(data.get('kind') or ''))
        if task is None:
            raise ApiError('Choose a task type from the list. The list shows what '
                           'this worker can do for you.')
        if task.takes == 'none':
            return self._enqueue_new_project(data, task)
        repo = self.validate_repo(data.get('repo'), configured=True)
        if self.connection_status().get('status') == 'waiting':
            raise ApiError('Finish connecting GitHub before starting a task. The connection code expires if you leave it unfinished.', 409)
        output = self.destination(data)
        learning_id = data.get('learning_session_id')
        if learning_id is not None:
            if task.profile != 'draft' or task.id not in {'write-docs', 'triage-issues'}:
                raise ApiError('Learning sessions prepare documentation or issue drafts. '
                               'Choose one of those tasks so you can review before publishing.', 409)
            session = self.learning_get(learning_id)['session']
            if (session.get('repo') != repo or session.get('account') != data.get('account')
                    or session.get('output_dir') != output):
                raise ApiError('Keep the learning session’s repository, account and results '
                               'folder together. Return to Explore & learn to start this task.', 409)
        if self.controls().read().get('config_revision') == self.revision():
            raise ApiError('Restart the worker to apply your saved settings before starting a task.', 409)
        if not self.service()['active']:
            raise ApiError('Start the worker before adding a task.', 409)
        item: dict = {}
        number = 0
        if task.takes in {'pr', 'issue'}:
            item = self.target(repo, task.id, data.get('number'))
            state = str(item.get('state', '')).upper()
            if state != 'OPEN' and not (task.allows_closed and state in {'MERGED', 'CLOSED'}):
                raise ApiError('This item is closed or merged. Choose an open item, '
                               'or a task type that acts on finished work.', 409)
            number = int(data['number'])
            if self.queue().find_open_by_repo_number(repo, number, kind=task.id):
                raise ApiError('A task of this type for this item is already queued or running. Open its details instead.', 409)
        elif self.queue().find_open_by_repo(repo, task.id):
            raise ApiError('This task is already queued or running for this repository. Open its details instead.', 409)
        values = {}
        for name, spec in task.fields.items():
            value = data.get(name)
            if spec.get('required') and (value is None or str(value).strip() == ''):
                raise ApiError(f"Fill in “{spec.get('label', name)}” — {task.help}")
            values[name] = value if value is None else str(value)[:2000]
        if task.validate:
            task.validate({'repo': repo, 'number': number, 'values': values,
                           'accept_no_checks': bool(data.get('accept_no_checks'))})
        repo_cfg = next((r for r in self.config().repos if r.full_name == repo), None)
        if task.profile in {'write', 'high-stakes'} and repo_cfg is not None \
                and not repo_cfg.checks and not data.get('accept_no_checks'):
            raise ApiError('This repository has no safety net: it has no verification '
                           'commands, so changes would land unverified. Add checks in '
                           'Settings, or confirm you accept unverified changes for this '
                           'one task.', 409)
        user = self.github_status()
        if not user['configured']:
            raise ApiError('Connect GitHub before starting a task.', 503)
        revision = (item.get('headRefOid', '')
                    or str(item.get('updatedAt') or '')
                    or 'repo-' + hashlib.sha256(
                        json.dumps(values, sort_keys=True).encode()).hexdigest()[:12])
        request = {'repo': repo, 'kind': task.id, 'number': number,
                   'values': values, 'output_dir': output,
                   'account': data['account'],
                   'accept_no_checks': bool(data.get('accept_no_checks'))}
        if learning_id is not None:
            request['learning_session_id'] = learning_id
        job = Job(job_id=new_job_id(), kind=JobKind(task.id), repo_full_name=repo,
                  number=number, requested_by=user['id'], command_comment_id=0,
                  head_sha=item.get('headRefOid', ''),
                  dedup_key=make_dedup_key(JobKind(task.id), repo, number, revision),
                  notes={'source': 'gui', 'task_type': task.id,
                         'title': item.get('title', '') or task.label,
                         'values': values, 'request': request,
                         'output_dir': output, 'member': data['account'],
                         'member_home': self.member_home(data['account'])})
        if learning_id is not None:
            job.notes['learning_session_id'] = learning_id
        if not self.queue().add(job):
            raise ApiError('An identical task has already completed. Change the '
                           'details and try again, or wait for new changes on the '
                           'item.', 409)
        return {'job': json.loads(job.to_json())}

    def job_action(self, job_id, action):
        job = self.queue().get(job_id)
        if not job:
            raise ApiError('This task could not be found.', 404)
        if action == 'stop':
            self.queue().stop_job(job_id)
            return {'job': json.loads(self.queue().get(job_id).to_json())}
        if action == 'retry':
            if job.notes.get('posting') or job.notes.get('publish_outcome') == 'uncertain':
                raise ApiError('The previous publication is not confirmed. Open this task and check GitHub before making a new publishing decision. Its evidence is kept.', 409, actions=['jobs'])
            if job.state not in {JobState.FAILED, JobState.STOPPED}:
                raise ApiError('Only failed or stopped tasks can be retried.', 409)
            original = job.notes.get('request')
            if isinstance(original, dict) and original.get('repo') and original.get('kind'):
                return self.enqueue({**original, **original.get('values', {})})  # restore every field from the saved request
            return self.enqueue({'repo': job.repo_full_name, 'kind': job.kind.value, 'number': job.number,
                                 'output_dir': job.notes.get('output_dir'), 'account': job.notes.get('member')})
        raise ApiError('This task action is not supported.')

    def _enqueue_new_project(self, data, task):
        """A task that creates a repository: nothing is configured yet."""
        identity = self.gh.rest('GET', 'user') or {}
        login = str(identity.get('login') or '').strip()
        owner = str(data.get('owner') or login).strip()
        if not login or owner.casefold() != login.casefold():
            raise ApiError('New repositories are created under the connected '
                           f'GitHub account ({login or "unknown"}).')
        values = {}
        for name, spec in task.fields.items():
            value = data.get(name)
            if spec.get('required') and (value is None or str(value).strip() == ''):
                raise ApiError(f"Fill in “{spec.get('label', name)}” — {task.help}")
            values[name] = None if value is None else str(value)[:200]
        name = str(values.get('name') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', name):
            raise ApiError('Choose a project name of 1–80 letters, numbers, dots, '
                           'dashes or underscores.')
        repo = f'{owner}/{name}'
        if self.connection_status().get('status') == 'waiting':
            raise ApiError('Finish connecting GitHub before starting a task.', 409)
        output = self.destination(data)
        if not self.service()['active']:
            raise ApiError('Start the worker before adding a task.', 409)
        user = self.github_status()
        if not user['configured']:
            raise ApiError('Connect GitHub before starting a task.', 503)
        if self.queue().find_open_by_repo(repo, task.id):
            raise ApiError('This project is already being created. Open its details instead.', 409)
        revision = 'repo-' + hashlib.sha256(
            json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]
        request = {'repo': repo, 'kind': task.id, 'number': 0,
                   'values': values, 'output_dir': output,
                   'account': data['account'], 'owner': owner}
        job = Job(job_id=new_job_id(), kind=JobKind(task.id), repo_full_name=repo,
                  number=0, requested_by=user['id'], command_comment_id=0,
                  head_sha='',
                  dedup_key=make_dedup_key(JobKind(task.id), repo, 0, revision),
                  notes={'source': 'gui', 'task_type': task.id,
                         'title': name, 'values': values, 'request': request,
                         'output_dir': output, 'member': data['account'],
                         'member_home': self.member_home(data['account'])})
        if not self.queue().add(job):
            raise ApiError('An identical project was already created. Choose '
                           'another name.', 409)
        return {'job': json.loads(job.to_json())}

    @contextlib.contextmanager
    def _draft_action_lock(self, job_id):
        """One decision per job, including across independent GUI processes.

        Keep lock files in place: removing a locked file could give a second
        request a different inode and let both requests claim the same draft.
        This is separate from the queue lock, so a slow post cannot block
        status reads, Stop, or decisions on unrelated jobs.
        """
        folder = Path(self.config().state_dir) / 'draft-actions'
        folder.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(str(job_id).encode()).hexdigest()
        fd = os.open(folder / f'{name}.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ApiError('Another draft action is in progress for this '
                               'task. Wait for its result, then reload.', 409) from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def draft_action(self, job_id, data):
        """Post / edit / discard one draft. Nothing here runs automatically."""
        from .fleet_locks import FleetBusy, repo_lock
        with self._draft_action_lock(job_id):
            job = self.queue().get(job_id)
            if job is None:
                raise ApiError('This task could not be found.', 404)
            try:
                # Independent comment/issue drafts can post together. Source
                # changes and branch cleanup still own the repository outright.
                metadata_only = job.kind in {JobKind.REVIEW_PR, JobKind.TRIAGE_ISSUES, JobKind.ORGANIZE_WORK}
                with repo_lock(self.config().state_dir, job.repo_full_name, exclusive=not metadata_only, blocking=False):
                    return self._draft_action(job_id, data)
            except FleetBusy:
                raise ApiError('An agent is working on this repository. Your draft is saved; post it after that task finishes.', 409) from None

    def _draft_action(self, job_id, data):
        queue = self.queue()
        job = queue.get(job_id)
        if not job:
            raise ApiError('This task could not be found.', 404)
        if job.state is not JobState.WAITING_ON_YOU:
            raise ApiError('This task is not waiting for a draft decision. '
                           'Reload its details to see its current status.', 409)
        if job.notes.get('posting'):
            raise ApiError('A previous post may already have reached GitHub. '
                           'Check GitHub and the task details before starting '
                           'another task; this draft will not be sent again.', 409)
        drafts = job.notes.get('drafts') or []
        if not drafts:
            raise ApiError('This task has no draft to act on. Open its details to see what it produced.')
        name = str(data.get('name') or drafts[0]['name'])
        draft = next((d for d in drafts if d.get('name') == name), None)
        if draft is None:
            raise ApiError('Choose one of the drafts listed for this task.')
        action = data.get('action')
        if action == 'discard':
            job.state = JobState.STOPPED
            job.notes['phase'] = 'discarded'
            partial = bool(job.notes.get('posted'))
            message = ('Remaining drafts discarded. Anything already posted '
                       'stays on GitHub.' if partial else
                       'Discarded by the person who asked for it. Nothing was posted.')
            job.error = message
            self._cleanup_job_worktree(job)
            job.updated_at = iso()
            queue.update(job)
            return {'job': json.loads(job.to_json()),
                    'hint': message if partial else
                            'Nothing was posted. Start a new task whenever you want.'}
        if name in {p['name'] for p in job.notes.get('posted', [])}:
            raise ApiError('This draft was already posted. Choose an unposted '
                           'draft or reload the task details.', 409)
        if action == 'edit':
            body = str(data.get('body') or '').strip()
            if not body:
                raise ApiError('Write the words you want posted, or choose Discard.')
            draft['body'] = body[:20000]
            job.notes['drafts'] = drafts
            job.updated_at = iso()
            self.queue().update(job)
            return {'job': json.loads(job.to_json()),
                    'hint': 'Saved. Choose Post when the words are right.'}
        if action != 'post':
            raise ApiError('Choose Post, Edit, or Discard.')
        task = tasks.get(job.kind.value)
        if task is None or not callable(task.apply):
            raise ApiError('Posting for this task type is not available yet. '
                           'Nothing was sent; your draft is safe in the task '
                           'details.')
        if task.post_confirm_required and data.get('confirm') is not True:
            raise ApiError('This is a high-stakes action. Confirm it before it '
                           'is applied to GitHub.', 409)
        cfg = self.config()  # trusted saved settings, never request data
        job.notes['posting'] = name
        job.updated_at = iso()
        queue.update(job)  # persisted BEFORE the remote write
        if job.state is not JobState.WAITING_ON_YOU:
            # A concurrent Stop is authoritative in JobQueue.update().
            raise ApiError('This task was stopped before posting. Nothing '
                           'was sent by this request.', 409)
        try:
            context = {'repo': job.repo_full_name,
                                 'number': job.number, 'draft': dict(draft),
                                 'values': job.notes.get('values') or {},
                                 'worktree': job.notes.get('worktree_path'),
                                 'config': cfg,
                                 'stop_check': lambda: (
                                     (current := self.queue().get(job_id)) is None
                                     or current.state is not JobState.WAITING_ON_YOU)
                                 }
            ensure_not_stopped(context)
            result = task.apply(context, GuardedGitHub(self.gh, context), job) or {}
        except Exception as exc:
            # Only the write guard can prove a refusal happened before every
            # remote mutation. A missing/broken guard must remain conservative
            # when this control is used with an older worker installation.
            try:
                from .task_write_guard import PreWriteRefusal
                safe_refusal = type(exc) is PreWriteRefusal
            except Exception:
                safe_refusal = False
            if safe_refusal:
                job.notes.pop('posting', None)
                job.updated_at = iso()
                queue.update(job)
                raise ApiError(redact_text(str(exc)), 409) from None
            # apply() can fail after GitHub accepted a write, including with
            # ValueError/ApiError. Keep the durable claim until the remote
            # outcome is checked, rather than allowing an unsafe retry.
            message = ('Posting could not be confirmed. Please check GitHub '
                       'and the task details before starting another task. '
                       'This draft will not be sent again.')
            job.notes['phase'] = 'posting-unconfirmed'
            job.notes['verification'] = message
            job.updated_at = iso()
            queue.update(job)
            if isinstance(exc, (ApiError, ValueError)):
                # Keep actionable validation hints visible, as before, without
                # treating their exception type as proof a retry is safe.
                message = redact_text(str(exc))[:500] + ' ' + message
            raise ApiError(message, 409) from None
        posted = job.notes.setdefault('posted', [])
        posted.append({'name': name, 'at': iso(),
                       'result': str(result.get('result', ''))[:500]})
        job.notes.pop('posting', None)
        remaining = [d for d in drafts
                     if d.get('name') not in {p['name'] for p in posted}]
        if not remaining:
            job.state = JobState.DONE
            job.notes['phase'] = 'posted'
            self._cleanup_job_worktree(job)
        job.updated_at = iso()
        queue.update(job)
        return {'job': json.loads(job.to_json()),
                'hint': str(result.get('result') or 'Posted.')[:300],
                'actions_available': ['reload']}

    def _cleanup_job_worktree(self, job):
        """Release the retained scratch checkout once a draft task is over."""
        wt = job.notes.get('worktree_path')
        if not wt:
            return
        if job.notes.get('output_dir'):
            try:
                from .results import export_worktree
                export_worktree(job, wt)
                job.notes.pop('result_export_error', None)
            except Exception as exc:
                job.notes['result_export_error'] = str(exc)[:300]
                job.notes['worktree_cleanup'] = 'Scratch retained because result export failed. Correct the destination access before recovery.'
                return
        try:
            from .wiring import WorktreeOpsAdapter
            from .executor import cleanup_owned_checkout
            cfg = self.config()
            ops = WorktreeOpsAdapter(cfg)
            cleanup_owned_checkout(ops, cfg, job, wt)
        except Exception:
            job.notes['worktree_cleanup'] = 'Scratch retained; remove it by hand once the task is finished.'

    def change_config(self, data, action):
        lock = ControlStore(self.config_path.with_suffix('.edit'))
        with lock.lock():
            if data.get('revision') != self.revision():
                raise ApiError('Settings changed in another window. Reload this page before saving so those changes are preserved.', 409)
            raw = self.raw_config()
            previous_config = self.config_path.read_bytes()
            self.controls().read()  # Refuse unreadable control state before replacing configuration.
            action(raw)
            try:
                cfg = Config(repos=[RepoConfig(**r) for r in raw.get('repos', [])], model=ModelConfig(**raw.get('model', {})),
                             **{k: v for k, v in raw.items() if k not in {'repos', 'model'}})
            except (TypeError, ValueError) as error:
                raise ApiError('These settings could not be validated. Check the named fields and try again.') from error
            self.validate_settings(cfg)
            text = toml_document(raw)
            tomllib.loads(text)
            backup(self.config_path)
            temp = self.config_path.with_name(self.config_path.name + '.gui-tmp')
            fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(text); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, self.config_path)
            try:
                self.controls().change(lambda control: control.update(config_revision=self.revision()))
            except Exception:
                try:
                    atomic_bytes(self.config_path, previous_config)
                except Exception:
                    raise ApiError('Saved settings changed, but their restart marker could not be recorded. Restore the dated configuration backup before starting work.', 503) from None
                raise ApiError('Settings could not be saved completely. The previous configuration was restored.', 503) from None
        return {'ok': True, 'restart_required': True}

    def validate_settings(self, cfg):
        from .config_loader import fleet_table_problems
        problems = fleet_table_problems(cfg)
        if problems:
            raise ApiError(' '.join(problems))
        from .config_loader import fleet_table_problems
        problems = fleet_table_problems(cfg)
        if problems:
            raise ApiError("; ".join(problems))
        for value in [cfg.poll_repos, cfg.model.disable_reasoning, *[r.allow_unknown_rulesets for r in cfg.repos]]:
            if type(value) is not bool:
                raise ApiError('Choose Yes or No for each on/off setting.')
        if not isinstance(cfg.rest_api_version, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', cfg.rest_api_version):
            raise ApiError('Use a GitHub API version date in YYYY-MM-DD format.')
        bounds = {'poll_interval_seconds': (10, 3600), 'max_tool_iterations': (1, 500),
                  'run_command_timeout_seconds': (10, 3600), 'run_command_output_cap': (1000, 10000000)}
        for key, (low, high) in bounds.items():
            value = getattr(cfg, key)
            if type(value) is not int or not low <= value <= high:
                raise ApiError(f'{key} must be a whole number from {low} to {high}.')
        for key, low, high in [('max_calls_per_job', 1, 1000), ('max_calls_per_day', 1, 100000), ('max_tokens', 64, 131072),
                               ('request_timeout_seconds', 5, 600), ('lease_ttl_seconds', 60, 900)]:
            value = getattr(cfg.model, key)
            if type(value) is not int or not low <= value <= high:
                raise ApiError(f'{key} must be a whole number from {low} to {high}.')
        if cfg.model.auth_mode not in {'env', 'broker-lease'}:
            raise ApiError('Choose saved API key or company credential broker.')
        for value in [cfg.model.base_url, cfg.model.broker_base_url]:
            url = urlparse(value)
            if url.scheme not in {'https', 'http'} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ApiError('Use an HTTP or HTTPS provider address without embedded credentials, query parameters, or fragments.')
        if not cfg.model.model_id or len(cfg.model.model_id) > 200:
            raise ApiError('Enter a model identifier of 1–200 characters.')
        if not re.fullmatch(r'/[A-Za-z][A-Za-z0-9_-]{0,30}', cfg.command_prefix):
            raise ApiError('The command prefix must start with / and a letter, for example /agent.')
        if not cfg.allowed_command_users or any(type(i) is not int or i <= 0 for i in cfg.allowed_command_users):
            raise ApiError('Add at least one positive numeric GitHub user ID.')
        for repo in cfg.repos:
            self.validate_repo(repo.full_name)
            if not repo.base_branch or repo.base_branch.startswith('-') or any(c.isspace() for c in repo.base_branch):
                raise ApiError('Choose a valid repository branch.')
            if not isinstance(repo.checks, list) or not repo.checks or any(not isinstance(c, str) or not c.strip() or len(c) > 1000 for c in repo.checks):
                raise ApiError('Add at least one verification command for each repository.')
        for key in DESTINATION_KEYS:
            if not str(getattr(cfg, key)).strip():
                raise ApiError(DESTINATION_ERROR)

    def save_settings(self, data):
        incoming = data.get('settings', {})
        if not isinstance(incoming, dict):
            raise ApiError('Supply named settings values.')
        allowed = {f.name for f in dataclasses.fields(Config)} - {'repos', 'github_host', 'state_dir', 'worktree_root'}
        def update(raw):
            for key, value in incoming.items():
                if key == 'model':
                    permitted = {f.name for f in dataclasses.fields(ModelConfig)}
                    if not isinstance(value, dict) or set(value) - permitted:
                        raise ApiError('An unsupported model setting was supplied.')
                    raw.setdefault('model', {}).update(value)
                elif key in allowed:
                    raw[key] = value
                elif key in {'github_host', 'state_dir', 'worktree_root', 'repos'}:
                    if value != dataclasses.asdict(self.config())[key]:
                        raise ApiError('Installation paths, GitHub host, and repository lists cannot be changed here. Use Repositories for repository changes.')
                else:
                    raise ApiError('An unsupported setting was supplied.')
        return self.change_config(data, update)

    def save_repo(self, data):
        full = self.validate_repo(f'{data.get("owner", "")}/{data.get("name", "")}')
        if not any(b['name'] == data.get('base_branch') for b in self.branches(full)['items']):
            raise ApiError('The selected branch no longer exists. Reload the branches and choose again.')
        def update(raw):
            repos = raw.setdefault('repos', [])
            original = data.get('original_full_name') or full
            existing = next((r for r in repos if f'{r["owner"]}/{r["name"]}' == original), None)
            value = {k: data[k] for k in ['owner', 'name', 'base_branch', 'checks']}
            value['allow_unknown_rulesets'] = bool(data.get('allow_unknown_rulesets', False))
            if existing:
                if any(f'{r["owner"]}/{r["name"]}' == full and r is not existing for r in repos):
                    raise ApiError('This repository is already configured.', 409)
                existing.update(value)
            else:
                repos.append(value)
        result = self.change_config(data, update)
        try:
            # Prepare a visible setup step. A project save does not authorize
            # model requests or choose a member's results folder.
            self.watches().put(full, '*', {'scope': 'all', 'armed_at': iso(),
                                           'source': 'repo-added', 'enabled': False,
                                           'setup_required': True}, only_new=True)
        except Exception:
            return {**result, 'repo': full, 'hint': 'Repository saved. Its review watch could not be prepared. Open Review watches and choose Create a review watch to finish setup.',
                    'actions_available': ['open-watches']}
        saved = self.watches().read().get(full, {}).get('*') or {}
        return {**result, 'repo': full, 'watch_setup_required': bool(saved.get('setup_required')),
                'hint': ('Repository saved. Finish watch setup to choose a results folder and enable automatic follow-up. Apply the saved repository settings with an idle-worker restart.'
                         if saved.get('setup_required') else 'Repository saved. Restart the idle worker to apply the settings.')}

    def remove_repo(self, data):
        full = self.validate_repo(data.get('repo'))
        if self.queue().find_open_by_repo_number(full, 0) or any(j.repo_full_name == full and j.state not in TERMINAL_STATES for j in self.queue().all()):
            raise ApiError('Finish or stop this repository’s active tasks before removing it.', 409)
        def update(raw):
            if len(raw.get('repos', [])) == 1 and f'{raw["repos"][0]["owner"]}/{raw["repos"][0]["name"]}' == full:
                raise ApiError('Keep at least one repository configured. Add its replacement before removing the last repository.', 409)
            raw['repos'] = [r for r in raw.get('repos', []) if f'{r["owner"]}/{r["name"]}' != full]
        return self.change_config(data, update)

    def stop_watch(self, data):
        repo = self.validate_repo(data.get('repo'))
        mode = data.get('mode')
        if mode not in {'all', 'selected'}:
            raise ApiError('Choose the review watch you want to stop.')
        key = '*' if mode == 'all' else str(target_number(data.get('number')))
        def update(rows):
            entries = rows.get(repo, {})
            if key not in entries:
                raise ApiError('This saved watch was not found. Refresh Review watches.', 404)
            keys = [k for k, meta in entries.items() if k == key or (key == '*' and meta.get('scope') == 'all')]
            for number in keys:
                entries[number].update(enabled=False, stopped_at=iso(), status_reason='You stopped this watch. Its earlier feedback positions and history are kept.')
                entries[number].pop('setup_required', None)
        self.watches().change(update)
        return {'ok': True, 'hint': 'Watch stopped. Its history and feedback positions are kept. Edit watch can enable it again.'}

    def add_watch(self, data):
        repo = self.validate_repo(data.get('repo'), configured=True)
        output = self.destination(data)
        mode = data.get('mode', 'selected')
        if mode not in {'all', 'selected'}:
            raise ApiError('Choose a selected pull request or all open pull requests.')
        key = '*' if mode == 'all' else str(target_number(data.get('number')))
        existing = self.watches().read().get(repo, {}).get(key)
        if existing and existing.get('setup_required') and mode == 'all':
            return self.edit_watch({**data, 'enabled': True})
        if existing:
            raise ApiError('This feedback is already covered by a saved watch. Open Review watches and choose Edit watch to change its folder while keeping its history.', 409,
                           actions=['open-watches'])
        meta = {'authorized_at': iso(), 'output_dir': output, 'member': data['account'],
                'member_home': self.member_home(data['account']), 'scope': mode,
                'enabled': True, 'watch_status': 'not-checked',
                'status_reason': 'Watch saved. The next polling check will inspect new feedback.'}
        if mode == 'selected':
            item = self.target(repo, 'fix-pr', data.get('number'))
            if str(item.get('state', '')).upper() != 'OPEN':
                raise ApiError('A closed or merged pull request cannot be watched.', 409)
            comments = self.gh.rest('GET', f'repos/{repo}/pulls/{int(data["number"])}/comments', paginate=True) or []
            meta.update(last_review_comment_id=max((int(c['id']) for c in comments), default=0), title=item.get('title', ''))
            created = self.watches().put(repo, int(data['number']), meta, only_new=True)
        elif mode == 'all':
            created = self.watches().put(repo, '*', meta, only_new=True)
        if not created:
            raise ApiError('This watch was saved in another window. Open Review watches and edit its current settings; its feedback history is kept.', 409,
                           actions=['open-watches'])
        return {'ok': True}

    def service_action(self, data):
        with ControlStore(self.config_path.with_suffix('.edit')).lock():
            return self._service_action(data)

    def _service_action(self, data):
        action = data.get('action')
        if action in {'pause', 'resume'}:
            self.controls().change(lambda control: control.update(polling_paused=action == 'pause'))
            return {'ok': True}
        if action not in {'start', 'stop', 'restart'}:
            raise ApiError('Choose start, stop, restart, pause, or resume.')
        if action == 'start' and self.service()['active'] and self.controls().read().get('config_revision') == self.revision():
            raise ApiError('The worker is already running with earlier settings. Choose Restart to apply your changes.', 409)
        if action in {'stop', 'restart'}:
            self.require_idle()
        result = self.runner(['systemctl', '--user', action, 'gha-worker.service'], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ApiError('The worker could not be controlled. Check that its user service is installed, then run Connection checks.', 503)
        if action in {'start', 'restart'}:
            self.controls().change(lambda control: control.pop('config_revision', None))
        return {'ok': True}

    def diagnostics(self):
        github = self.github_status()
        checks = [{'name': 'GitHub connection', 'status': 'pass' if github['configured'] else 'fail',
                   'message': f'Connected as {github["login"]}.' if github['configured'] else github['error']},
                  {'name': 'Worker service', 'status': 'pass' if self.service()['active'] else 'warn',
                   'message': 'Worker is running.' if self.service()['active'] else 'The worker is stopped. Start it from the overview.'}]
        cfg = self.config()
        for name, path in [('Worker records', cfg.state_dir), ('Scratch workspaces', cfg.worktree_root)]:
            exists = Path(path).is_dir() and os.access(path, os.W_OK | os.X_OK)
            checks.append({'name': name, 'status': 'pass' if exists else 'fail', 'message': f'{path}: ' + ('available and writable.' if exists else 'missing or not writable. Ask the tool maintainer to restore access.')})
        from .config_loader import load_env_file, model_auth_problems
        env = {**load_env_file(self.env_path), **os.environ}
        problems = model_auth_problems(cfg, env)
        checks.append({'name': 'Model credentials', 'status': 'fail' if problems else 'pass',
                       'message': ' '.join(problems) if problems else 'Credential source is configured. No model request was made.'})
        checks.extend(self.contextual_diagnostics())
        return {'checks': checks, 'hint': 'These are setup and recovery checks. A saved credential is not proof that a provider is reachable; test that provider separately.'}

    def provider_check(self, data):
        provider = data.get('provider')
        if provider in {'glm', 'deepseek'}:
            return self.check_fleet_provider(provider, data)
        if data.get('confirmed') is not True:
            raise ApiError('Confirm the single provider request before running this check.')
        if not self._probe_lock.acquire(blocking=False):
            raise ApiError('A provider check is already running.', 409)
        try:
            result = self.runner([sys.executable, '-m', 'ghagent.cli', 'provider-check', '--config', str(self.config_path), '--env-file', str(self.env_path)], capture_output=True, text=True, timeout=210)
            return {'ok': result.returncode == 0, 'output': redact_text((result.stdout + '\n' + result.stderr)[-4000:])}
        finally:
            self._probe_lock.release()

    def check_fleet_provider(self, provider, data):
        if data.get('confirmed') is not True:
            raise ApiError('Choose Send one test request to check this provider. It may use your provider allowance.', hint='No request was sent.', actions=['settings'])
        if not self._probe_lock.acquire(blocking=False):
            raise ApiError('A connection test is already running. Wait for its result.', 409)
        bearer = None
        try:
            from .config_loader import load_env_file
            from .fleet_providers import model_config
            from .model_client import ModelClient
            from .wiring import make_bearer
            cfg, env = model_config(self.config(), {**os.environ, **load_env_file(self.env_path)}, provider)
            cfg = dataclasses.replace(cfg, model=dataclasses.replace(cfg.model, max_tokens=64, max_calls_per_job=1))
            bearer = make_bearer(cfg, env)
            client = ModelClient(cfg.model, bearer, job_scope='connection-test-' + iso(),
                                 day_counter_path=str(Path(cfg.state_dir) / 'fleet-v1' / (provider + '-model-call-counts.json')), max_attempts=1)
            client.chat([{'role': 'user', 'content': 'Reply with OK.'}])
            outcome = {'ok': True, 'output': 'The ' + provider + ' provider answered one test request. No coding task or GitHub change was made.'}
        except Exception as exc:
            outcome = {'ok': False, 'output': redact_text(str(exc))[:2000], 'hint': 'Check this provider’s saved credential and address, then send a new test only when ready.'}
        finally:
            if bearer is not None:
                close = getattr(bearer, 'close', None)
                if callable(close):
                    close()
            self._probe_lock.release()
        ControlStore(Path(self.config().state_dir) / 'provider-tests.json').change(lambda rows: rows.update({provider: {**outcome, 'checked_at': iso()}}))
        return outcome

    def rules_inspection(self, repo):
        return ControlStore(Path(self.config().state_dir) / 'rules-inspections.json').read().get(repo, {'state': 'not-checked', 'reason': "I don't know yet. Choose Inspect rules to check GitHub.", 'checked_at': None})

    @staticmethod
    def _provider_env_document(env):
        """Keep the existing environment readable by both the worker and systemd."""
        lines = []
        for key, value in env.items():
            if any(c in value for c in '\r\n\x00'):
                raise ApiError('An existing environment value cannot be preserved safely. Ask the maintainer to correct the environment file.')
            if "'" not in value:
                lines.append(f"{key}='{value}'")
            elif '"' not in value and '\\' not in value:
                lines.append(f'{key}="{value}"')
            else:
                raise ApiError('An existing environment value cannot be preserved safely. Ask the maintainer to correct the environment file.')
        return ('\n'.join(lines) + '\n').encode()

    def set_provider_source(self, data):
        """A person's explicit source choice changes no secret or provider allowance."""
        from .config_loader import load_env_file
        from .fleet_providers import model_config, provider_status
        provider, source = data.get('provider'), data.get('source')
        if provider != 'deepseek' or source not in {'env', 'broker-lease'}:
            raise ApiError('Choose the dedicated DeepSeek key or company broker connection.', actions=['settings'])
        with ControlStore(self.config_path.with_suffix('.edit')).lock(), ControlStore(self.env_path.with_suffix('.edit')).lock():
            env = load_env_file(self.env_path)
            old_source = env.get('FATCHIN_DEEPSEEK_AUTH_MODE') or 'automatic'
            proposed = {**os.environ, **env, 'FATCHIN_DEEPSEEK_AUTH_MODE': source}
            try:
                model_config(self.config(), proposed, 'deepseek')
            except ValueError as exc:
                raise ApiError(str(exc), 409, hint='No source or credential was changed. Store the missing credential first, or choose another saved connection.', actions=['settings']) from None
            history = ControlStore(Path(self.config().state_dir) / 'provider-source-history.json')
            history.read()
            controls = self.controls()
            controls.read()
            previous_env = self.env_path.read_bytes() if self.env_path.exists() else None
            previous_history = history.path.read_bytes() if history.path.exists() else None
            previous_controls = controls.path.read_bytes() if controls.path.exists() else None
            env['FATCHIN_DEEPSEEK_AUTH_MODE'] = source
            content = self._provider_env_document(env)
            for path in (self.env_path, history.path, controls.path):
                backup(path)
            try:
                atomic_bytes(self.env_path, content)
                history.change(lambda rows: rows.setdefault('deepseek', []).append({
                    'at': iso(), 'previous_source': old_source, 'selected_source': source,
                    'reported_by': 'user', 'credential_changed': False}))
                controls.change(lambda control: control.update(config_revision=self.revision()))
            except Exception:
                restore_optional(self.env_path, previous_env)
                restore_optional(history.path, previous_history)
                restore_optional(controls.path, previous_controls)
                raise ApiError('The DeepSeek connection choice could not be saved. The previous choice and credentials were restored; try again after opening setup checks.', 503, actions=['diagnostics']) from None
            row = next(row for row in provider_status(self.config(), proposed) if row['id'] == 'deepseek')
        return {'ok': True, 'restart_required': True, 'provider': row,
                'hint': 'DeepSeek will use ' + row['credential_source_label'].lower() + ' after you apply the saved settings. No model request was sent and no credential was replaced.',
                'actions_available': ['settings']}

    def credential(self, data):
        names = {'model': 'GHA_MODEL_API_KEY', 'deepseek': 'DEEPSEEK_API_KEY', 'broker': 'MCP_SECRET_BROKER_ADMIN_TOKEN', 'deepseek-broker': 'MCP_SECRET_BROKER_ADMIN_TOKEN'}
        name = names.get(data.get('kind'))
        selected_source = data.get('provider_source')
        if selected_source is not None and (data.get('kind'), selected_source) not in {
                ('deepseek', 'env'), ('broker', 'broker-lease'), ('deepseek-broker', 'broker-lease')}:
            raise ApiError('Choose the dedicated DeepSeek key for its direct connection, or the company broker credential for the broker connection.')
        value = str(data.get('value', '')).strip()
        supplied = str(data.get('supplied_by', '')).strip()
        if not name or not value or len(value) > 10000 or any(c.isspace() or c in '\x00\"\'\\' for c in value) or not supplied:
            raise ApiError('Choose the credential type and enter its value and who supplied it. Line breaks are not allowed.')
        from .config_loader import load_env_file
        with ControlStore(self.config_path.with_suffix('.edit')).lock(), ControlStore(self.env_path.with_suffix('.edit')).lock():
            records = ControlStore(self.env_path.parent / 'credential-records.json')
            records.read()  # Validate descriptive state before changing any secret.
            previous_records = records.path.read_bytes() if records.path.exists() else None
            self.controls().read()
            env = load_env_file(self.env_path)
            env[name] = value
            if selected_source is not None:
                env['FATCHIN_DEEPSEEK_AUTH_MODE'] = selected_source
                from .fleet_providers import model_config
                try:
                    model_config(self.config(), {**os.environ, **env}, 'deepseek')
                except ValueError as exc:
                    raise ApiError(str(exc), 409, hint='No credential was replaced. Review the chosen DeepSeek connection.', actions=['settings']) from None
            previous_env = self.env_path.read_bytes() if self.env_path.exists() else None
            backup(self.env_path)
            backup(records.path)
            self.env_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.env_path.with_name('.env.gui-tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                for key, text in env.items():
                    if any(c in text for c in '\r\n\x00'):
                        raise ApiError('An existing environment value cannot be preserved safely. Ask the maintainer to correct the environment file.')
                    # Single quotes are literal to both systemd and load_env_file.
                    if "'" not in text:
                        handle.write(f"{key}='{text}'\n")
                    elif '"' not in text and '\\' not in text:
                        handle.write(f'{key}="{text}"\n')
                    else:
                        raise ApiError('An existing environment value cannot be preserved safely. Ask the maintainer to correct the environment file.')
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, self.env_path)
            description = {'what': 'DeepSeek fleet API credential' if name == 'DEEPSEEK_API_KEY' else 'Model provider API credential' if name == 'GHA_MODEL_API_KEY' else 'Company broker administration credential',
                           'does': 'Authenticates DeepSeek fleet model requests' if name == 'DEEPSEEK_API_KEY' else 'Authenticates gha-worker model requests' if name == 'GHA_MODEL_API_KEY' else 'Mints short-lived model access leases for gha-worker',
                           'not': 'Not GitHub authentication or application access', 'supplied_by': supplied, 'supplied_at': iso()}
            if selected_source is not None:
                description['selected_deepseek_source'] = selected_source
            try:
                records.change(lambda entries: entries.update({name: description}))
                self.controls().change(lambda control: control.update(config_revision=self.revision()))
            except Exception:
                try:
                    restore_optional(self.env_path, previous_env)
                    restore_optional(records.path, previous_records)
                except Exception:
                    raise ApiError('The credential changed but its description could not be saved. Restore the dated environment backup before restarting the worker.', 503) from None
                raise ApiError('The credential description could not be saved. The previous credential was restored; existing records were preserved.', 503) from None
        return {'ok': True, 'restart_required': True, 'record': description}

    def connect_github(self):
        with ControlStore(self.config_path.with_suffix('.edit')).lock():
            return self._connect_github()

    def _connect_github(self):
        self.require_idle()
        from .config_loader import load_env_file
        credentials = {**load_env_file(self.env_path), **os.environ}
        if any(credentials.get(k) for k in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')):
            raise ApiError('This worker uses a saved GitHub token. Ask the maintainer to remove that token before switching to browser sign-in. Existing repository access remains available.', 409)
        with self._connection_lock:
            if self._connection['status'] == 'waiting':
                return dict(self._connection)
            self._connection = {'status': 'waiting', 'verification_uri': 'https://github.com/login/device'}
        def run():
            try:
                env = {**os.environ, 'BROWSER': 'true', 'GH_BROWSER': 'true', 'GH_PROMPT_DISABLED': '1'}
                proc = subprocess.Popen([find_gh(), 'auth', 'login', '--hostname', self.config().github_host, '--git-protocol', 'https', '--web'],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                timer = threading.Timer(900, proc.terminate); timer.start()
                try:
                    for line in proc.stdout:
                        code = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', line)
                        if code:
                            with self._connection_lock:
                                self._connection['code'] = code.group(1)
                    exit_code = proc.wait()
                finally:
                    timer.cancel()
                    proc.stdout.close()
                with self._connection_lock:
                    self._connection['status'] = 'finished' if exit_code == 0 else 'error'
                    self._connection['message'] = 'GitHub connected. Refresh connection checks.' if exit_code == 0 else 'GitHub connection was not completed. Try again and approve the displayed device code.'
                    self._connection.pop('code', None)
                with self._cache_lock:
                    self._cache.pop('github-user', None)
            except Exception:
                with self._connection_lock:
                    self._connection.update(status='error', message='The GitHub connection process could not start. Check that gh is installed.')
        threading.Thread(target=run, daemon=True).start()
        return dict(self._connection)

    def connection_status(self):
        with self._connection_lock:
            return dict(self._connection)

    def github_user(self, login='', user_id=None):
        if user_id is not None:
            if not str(user_id).isdigit() or int(user_id) < 1:
                raise ApiError('Choose a positive numeric GitHub user ID.')
            item = self.gh.rest('GET', f'user/{int(user_id)}')
            return {'login': item['login'], 'id': item['id']}
        login = str(login).strip().removeprefix('@')
        if login and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', login):
            raise ApiError('Enter a GitHub username, for example example-owner.')
        item = self.gh.rest('GET', 'users/' + login if login else 'user')
        return {'login': item['login'], 'id': item['id']}

    def job_result_path(self, job_id, relative=''):
        job = self.queue().get(job_id)
        if not job:
            raise ApiError('Task not found.', 404)
        recoverable = job.state in TERMINAL_STATES | {JobState.WAITING_ON_YOU}
        output = job.notes.get('output_dir')
        if not output:
            if not recoverable:
                raise ApiError('This task has no selected result folder yet.', 404,
                               hint='Wait for its current step to finish, or download its saved report now. To choose a new result folder, stop the task first.',
                               actions=['download-report'])
            raise ApiError('This task has no selected result folder. Its saved record and GitHub links are kept.', 404,
                           hint='Choose Save existing results elsewhere to select a folder, or Download report to read its saved record. No model work or GitHub action will be repeated.',
                           actions=['save-results', 'download-report'])
        try:
            parent, _ = self.account_path(job.notes.get('member'), output)
        except ApiError as exc:
            if not recoverable:
                raise
            raise ApiError('The recorded result folder cannot be opened. ' + str(exc), 404,
                           hint='Choose Save existing results elsewhere to copy any retained files, or Download report to read its saved record. Earlier records and files are kept.',
                           actions=['save-results', 'download-report']) from None
        root = parent / ('gha-worker-' + job.job_id)
        if root.is_symlink() or not root.is_dir():
            if recoverable:
                raise ApiError('This task has no saved files in its recorded result folder.', 404,
                               hint='Choose Save existing results elsewhere to copy any retained files, or Download report to read its saved record. No model work or GitHub action will be repeated.',
                               actions=['save-results', 'download-report'])
            raise ApiError('This task has not exported result files yet. Refresh after its current step finishes.', 404)
        root = root.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or any(p.startswith('.') for p in Path(relative).parts):
            raise ApiError('Choose a file inside this task’s result folder.', 403)
        if target.name.lower() in {'env', 'credentials', 'secrets.json'} or target.suffix.lower() in {'.pem', '.key'}:
            raise ApiError('Credential files cannot be viewed through the task file browser.', 403)
        return root, target

    def job_download(self, job_id, relative):
        _, target = self.job_result_path(job_id, relative)
        if not target.is_file():
            raise ApiError('Choose an existing task result file.', 404)
        with target.open('rb') as handle:
            content = handle.read(50_000_001)
        if len(content) > 50_000_000:
            raise ApiError('This file exceeds the 50 MB browser download limit. Open it from your selected result folder.', 413)
        return target.name, content

    def job_files(self, job_id, relative=''):
        root, target = self.job_result_path(job_id, relative)
        if target.is_dir():
            return {'path': relative, 'items': [{'name': p.name, 'path': str(p.relative_to(root)), 'is_dir': p.is_dir(),
                                               'size': p.stat().st_size} for p in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
                                              if not p.name.startswith('.') and not p.is_symlink()][:500]}
        if target.is_file():
            with target.open('rb') as handle:
                content = handle.read(100001)
            if b'\x00' in content:
                raise ApiError('This binary file cannot be previewed. Use Download file or open it from your result folder.')
            return {'path': relative, 'content': redact_text(content[:100000].decode('utf-8', errors='replace')), 'truncated': len(content) > 100000}
        raise ApiError('This file is no longer present. The task record is still available.', 404)
