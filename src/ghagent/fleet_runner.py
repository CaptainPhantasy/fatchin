"""One fenced fleet task. Model tools never execute commands or remote writes.

The standard module CLI is used by the supervisor. All network and Git side
 effects have injectable seams; durable mutation admission precedes dispatch.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from . import fleet_prompts, triggers
from .github_client import GitHubClient, GitHubError, NotFound, RateLimited
from .learning import _redact, _sensitive_path
from .model_client import ModelClient, ModelError, SpendLimitExceeded
from .wiring import make_bearer

MAX_FILE = 256 * 1024
MAX_DIFF = 512 * 1024
MAX_FILES = 200
MAX_FINDINGS = 3
REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
SHA = re.compile(r'^[0-9a-f]{40,64}$')


class FleetBlocked(RuntimeError):
    """A visible, non-replayable task refusal."""


class FleetStopped(FleetBlocked):
    pass


class FleetRetry(FleetBlocked):
    """A bounded automatic retry must first reconcile any saved intent."""
    pass


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _safe_error(exc):
    if isinstance(exc, FleetBlocked):
        return _redact(str(exc))[:1000]
    match = re.search(r'\bHTTP\s+([45][0-9]{2})\b', str(exc))
    return ('The configured service returned HTTP ' + match[1] + '.' if match else
            'The task could not finish (' + type(exc).__name__ + '). Its saved evidence was retained.')


def _valid_repo(value):
    if not isinstance(value, str) or not REPO.fullmatch(value):
        raise FleetBlocked('The repository identity is missing or invalid.')
    return value


def _valid_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise FleetBlocked('The exact source commit could not be established.')
    return value


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 500 or '\\' in value or '\x00' in value:
        raise FleetBlocked('A source file path is invalid.')
    path = Path(value)
    if path.is_absolute() or any(p in {'', '.', '..', '.git'} for p in path.parts) or _sensitive_path(value):
        raise FleetBlocked('Credential, Git-internal and outside-checkout paths are excluded.')
    return path.as_posix()


def _path(root, value, *, existing=True):
    relative = _relative(value)
    current = Path(root)
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise FleetBlocked('Symbolic links are excluded from fleet file tools.')
    if existing and not current.is_file():
        raise FleetBlocked('The requested source file is unavailable.')
    return current


def _read(root, value):
    path = _path(root, value)
    if path.stat().st_size > MAX_FILE:
        raise FleetBlocked('The source file exceeds the bounded review size; a manual review is needed.')
    raw = path.read_bytes()
    if b'\x00' in raw:
        raise FleetBlocked('A binary file needs manual review.')
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        raise FleetBlocked('A non-UTF-8 file needs manual review.') from None


def _safe_data(value):
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_safe_data(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _safe_data(v) for k, v in value.items()}
    return value


def _tool(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': list(required), 'additionalProperties': False}}}


READ_TOOLS = [
    _tool('read_file', 'Read one UTF-8 source file, with line numbers.',
          {'path': {'type': 'string'}}, ['path']),
    _tool('list_dir', 'List one checkout directory; credential and Git internals are excluded.',
          {'path': {'type': 'string'}}),
    _tool('finish', 'Return the evidence-supported result; no remote action is performed.',
          {'summary': {'type': 'string'}, 'complete': {'type': 'boolean'},
           'actionable': {'type': 'boolean'}, 'paths': {'type': 'array', 'items': {'type': 'string'}},
           'findings': {'type': 'array', 'items': {'type': 'object', 'properties': {
               'title': {'type': 'string'}, 'body': {'type': 'string'},
               'path': {'type': 'string'}, 'line': {'type': 'integer'}},
               'required': ['title', 'body', 'path', 'line'], 'additionalProperties': False}}}, ['summary', 'complete']),
]
EDIT_TOOL = _tool('edit_file', 'Replace one exact, unique string inside an allowed file.',
                 {'path': {'type': 'string'}, 'old_text': {'type': 'string'}, 'new_text': {'type': 'string'}},
                 ['path', 'old_text', 'new_text'])
CREATE_TOOL = _tool('create_file', 'Create a new source or test file only inside the declared fix scope.',
                    {'path': {'type': 'string'}, 'content': {'type': 'string'}}, ['path', 'content'])
DELETE_TOOL = _tool('delete_file', 'Delete one existing regular file only when the finding requires it.',
                    {'path': {'type': 'string'}}, ['path'])


class RestrictedAgent:
    def __init__(self, model, root, active, *, limit=40, on_call=None):
        self.model, self.root, self.active = model, Path(root), active
        self.limit = min(60, max(1, limit))
        self.on_call = on_call

    def run(self, role, evidence, allowed=()):
        allowed = {_relative(x) for x in allowed}
        system = {'review': fleet_prompts.REVIEW, 'classify': fleet_prompts.CLASSIFY,
                  'fix': fleet_prompts.FIX}[role]
        messages = [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(_safe_data(evidence), ensure_ascii=False)}]
        tools = READ_TOOLS + ([EDIT_TOOL, CREATE_TOOL, DELETE_TOOL] if role == 'fix' else [])
        for _ in range(self.limit):
            self.active('Explaining and inspecting the assigned code')
            response = self.model.chat(messages, tools=tools)
            if self.on_call:
                self.on_call(int(getattr(self.model, 'calls_used', 0) or 0))
            self.active('Reading the model response')
            calls = response.get('tool_calls') or []
            if not isinstance(calls, list) or len(calls) > 12:
                raise FleetBlocked('The model returned an invalid tool batch.')
            if not calls:
                raise FleetBlocked('The model did not return a structured completed review.')
            assistant = {'role': 'assistant', 'content': response.get('content'), 'tool_calls': calls}
            if response.get('reasoning_content') is not None:
                assistant['reasoning_content'] = response['reasoning_content']
            messages.append(assistant)
            for call in calls:
                self.active('Inspecting the next file operation')
                function = call.get('function') or {}
                name = function.get('name')
                try:
                    args = json.loads(function.get('arguments') or '{}')
                    if not isinstance(args, dict):
                        raise ValueError('tool arguments must be named fields')
                    if name == 'finish':
                        if args.get('complete') is not True:
                            raise FleetBlocked('The model could not complete the review: ' + str(args.get('summary', 'missing evidence')))
                        if not isinstance(args.get('summary'), str) or not args['summary'].strip():
                            raise FleetBlocked('The model returned no evidence summary.')
                        return args
                    result = self.dispatch(name, args, role, allowed)
                except FleetBlocked as exc:
                    if name == 'finish':
                        raise
                    result = {'error': str(exc)}
                except (TypeError, ValueError):
                    result = {'error': 'Invalid tool arguments.'}
                messages.append({'role': 'tool', 'tool_call_id': str(call.get('id') or ''),
                                 'content': json.dumps(_safe_data(result), ensure_ascii=False)})
        raise FleetBlocked('This item reached its bounded model-step limit. Other items can continue.')

    def dispatch(self, name, args, role, allowed):
        if name == 'read_file':
            content = _read(self.root, args.get('path'))
            return {'path': args['path'], 'content': '\n'.join(f'{i}: {line}' for i, line in enumerate(content.splitlines(), 1))}
        if name == 'list_dir':
            raw = args.get('path') or ''
            path = self.root if raw in {'', '.'} else _path(self.root, raw, existing=False)
            if not path.is_dir():
                raise FleetBlocked('That directory is unavailable.')
            entries = [p.name + ('/' if p.is_dir() else '') for p in sorted(path.iterdir())
                       if not p.is_symlink() and p.name != '.git' and not _sensitive_path(p.name)]
            if len(entries) > 500:
                raise FleetBlocked('This directory is too large; choose a more specific source folder.')
            return {'entries': entries}
        if name in {'create_file', 'delete_file'} and role == 'fix':
            relative = _relative(args.get('path'))
            if relative not in allowed:
                raise FleetBlocked('That file is outside this finding’s approved scope.')
            path = _path(self.root, relative, existing=name == 'delete_file')
            if name == 'delete_file':
                path.unlink()
                return {'path': relative, 'deleted': True}
            content = args.get('content')
            if path.exists() or not isinstance(content, str) or len(content.encode()) > MAX_FILE or '\x00' in content:
                raise FleetBlocked('Create only a new text file within the source-size limit.')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as handle:
                handle.write(content)
            return {'path': relative, 'created': True}
        if name == 'edit_file' and role == 'fix':
            path = _relative(args.get('path'))
            if path not in allowed:
                raise FleetBlocked('That file is outside this finding’s approved scope.')
            old, new = args.get('old_text'), args.get('new_text')
            if not isinstance(old, str) or not old or not isinstance(new, str) or len(new.encode()) > MAX_FILE:
                raise FleetBlocked('Use a nonempty exact replacement within the source-size limit.')
            source = _read(self.root, path)
            if source.count(old) != 1:
                raise FleetBlocked('The old text must match exactly once; read the file again.')
            updated = source.replace(old, new, 1)
            if len(updated.encode()) > MAX_FILE:
                raise FleetBlocked('The updated source file is too large.')
            _path(self.root, path).write_text(updated, encoding='utf-8')
            return {'path': path, 'edited': True}
        raise FleetBlocked('This tool is unavailable. Fleet models cannot run commands or contact GitHub.')


class Task:
    def __init__(self, cfg, env, item, store, gh, runner):
        self.cfg, self.env, self.item, self.store, self.gh = cfg, env, item, store, gh
        self.run_process = runner or subprocess.run
        self.fleet = item.get('fleet') or {}
        self.ident = str(item.get('id') or '')
        self.lease = str(item.get('lease') or '')
        self.repo = _valid_repo(item.get('repo'))
        self.number = int(item.get('number') or 0)
        self.head = _valid_sha(item.get('head_sha'))
        self.metadata = {}
        self.root = None
        self.known = list(item.get('mutation_intents') or [])
        self.observed_findings = []
        self.recorded_checks = []
        self.review_url = ''
        self.dry_run = bool((item.get('fleet') or {}).get('dry_run'))
        self.dry_run_actions = []
        with _destination(self.fleet):
            pass

    def active(self, stage='Working'):
        if not self.store.alive(self.ident, self.lease):
            raise FleetStopped('This task was stopped or replaced. No new write was admitted.')
        if self.store.heartbeat(self.ident, self.lease, stage) is False:
            raise FleetStopped('The worker lease is no longer active.')

    def note_calls(self, used):
        try:
            self.store.note_calls(self.ident, self.lease, used)
        except Exception:
            pass

    def identity(self):
        self.active('Checking the connected GitHub account')
        expected = self.fleet.get('github_id')
        if not expected or self.gh.current_user_id() != expected:
            raise FleetBlocked('The connected GitHub account changed. Restart this fleet for the connected account.')

    def git(self, args, *, cwd=None, check=True, timeout=None):
        self.active('Working in this task’s isolated checkout')
        if self.dry_run and 'push' in args[:3]:
            self.dry_run_actions.append({'kind': 'git-push', 'args': [str(a) for a in args]})
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        child_env = dict(os.environ)
        child_env.update(self.env)
        for key in list(child_env):
            if key.startswith('GIT_'):
                child_env.pop(key)
        child_env.update(GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1',
                         GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
        argv = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
                '-c', 'filter.lfs.smudge=', '-c', 'filter.lfs.required=false', *args]
        proc = self.run_process(argv, cwd=str(cwd or self.root) if cwd or self.root else None,
                                env=child_env, capture_output=True, text=True,
                                timeout=timeout or min(600, self.cfg.run_command_timeout_seconds))
        if check and proc.returncode:
            raise FleetBlocked('A trusted Git operation failed: ' + _redact(str(proc.stderr or proc.stdout))[:350])
        return proc

    def repository(self, name):
        data = self.gh.rest('GET', 'repos/' + _valid_repo(name))
        if not isinstance(data, dict) or type(data.get('id')) is not int or data.get('full_name', '').casefold() != name.casefold():
            raise FleetBlocked('The repository’s numeric identity could not be verified.')
        return data

    def inspect(self):
        self.identity()
        repository = self.repository(self.repo)
        if self.item.get('repo_id') and repository['id'] != self.item['repo_id']:
            raise FleetBlocked('The repository identity changed since this item was queued.')
        self.repo_id = repository['id']
        if self.number:
            pr = self.gh.rest('GET', f'repos/{self.repo}/pulls/{self.number}')
            if not isinstance(pr, dict) or pr.get('state') != 'open':
                raise FleetBlocked('The pull request is no longer open.')
            if (pr.get('head') or {}).get('sha') != self.head:
                raise FleetBlocked('The pull request changed. Discovery will queue its newest commit.')
            head, base = pr.get('head') or {}, pr.get('base') or {}
            source = head.get('repo') or {}
            self.source_repo = _valid_repo(source.get('full_name'))
            self.source_id = source.get('id')
            if type(self.source_id) is not int:
                raise FleetBlocked('The source repository identity is unavailable.')
            self.source_ref = str(head.get('ref') or '')
            self.base_sha = _valid_sha(base.get('sha'))
            self.metadata = {'pull': pr, 'repository': repository}
        else:
            self.source_repo, self.source_id = self.repo, self.repo_id
            self.source_ref = str(self.item.get('source_ref') or self.item.get('default_branch') or repository.get('default_branch') or '')
            self.base_sha = self.head
            self.metadata = {'repository': repository}
        for key, actual in [('source_repo', self.source_repo), ('source_repo_id', self.source_id), ('source_ref', self.source_ref)]:
            if self.item.get(key) is not None and self.item[key] != actual:
                raise FleetBlocked('The source repository or branch changed since this item was queued.')
        if not self.source_ref or self.source_ref.startswith('-') or '\x00' in self.source_ref:
            raise FleetBlocked('The source branch is unavailable.')
        ref = self.gh.rest('GET', f'repos/{self.source_repo}/git/ref/heads/{quote(self.source_ref, safe="")}')
        if (ref.get('object') or {}).get('sha') != self.head:
            raise FleetBlocked('The source branch moved; this item needs a fresh review.')
        return self.metadata

    def fresh(self):
        self.identity()
        source = self.repository(self.source_repo)
        if source['id'] != self.source_id:
            raise FleetBlocked('The source repository was replaced; all writes were held.')
        if self.number:
            pr = self.gh.rest('GET', f'repos/{self.repo}/pulls/{self.number}')
            if pr.get('state') != 'open' or (pr.get('head') or {}).get('sha') != self.head:
                raise FleetBlocked('The pull request moved or closed; no stale write was started.')
        ref = self.gh.rest('GET', f'repos/{self.source_repo}/git/ref/heads/{quote(self.source_ref, safe="")}')
        if (ref.get('object') or {}).get('sha') != self.head:
            raise FleetBlocked('The source branch moved; no stale write was started.')

    def preflight(self, repo=None, ref=None):
        self.fresh()
        name = repo or self.source_repo
        policy = next((r for r in self.cfg.repos if r.full_name.casefold() == name.casefold()), None)
        report = triggers.preflight(self.gh, name, head_ref=ref or self.source_ref,
                                    allow_workflows=False,
                                    allow_unknown_rulesets=bool(policy and policy.allow_unknown_rulesets))
        if report.verdict != triggers.SAFE:
            reason = report.reasons[0] if report.reasons else 'Automation could not be verified.'
            raise FleetBlocked('GitHub automation inspection held this write: ' + reason)
        self.active('GitHub automation inspection passed')

    def intent(self, kind, payload):
        self.identity()
        metadata = {'host': self.cfg.github_host, 'github_id': self.fleet['github_id'],
                    'repository_id': self.source_id, **payload}
        operation = self.store.intent(self.ident, self.lease, kind, metadata)
        if not operation:
            raise FleetStopped('Stop held this write before dispatch.')
        return operation

    def receipt(self, operation, result):
        self.store.receipt(self.ident, self.lease, operation['id'], result)
        return result

    def clone(self):
        scratch = Path(self.cfg.worktree_root) / 'fleet-v1'
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='item-' + _hash(self.ident)[:12] + '-', dir=scratch)) / 'checkout'
        (self.root.parent / 'owner.json').write_text(json.dumps({'item_id': self.ident}), encoding='utf-8')
        url = f'https://{self.cfg.github_host}/{self.source_repo}.git'
        self.git(['clone', '--no-checkout', '--no-tags', '--', url, str(self.root)], cwd=self.root.parent)
        self.git(['fetch', '--no-tags', 'origin', self.head])
        self.git(['checkout', '--detach', self.head])
        if self.git(['rev-parse', 'HEAD']).stdout.strip() != self.head:
            raise FleetBlocked('The checkout is not the requested commit.')

    def diff_evidence(self):
        if not self.number:
            paths = [str(p) for p in (self.item.get('paths') or [self.item.get('path')]) if p]
            if not paths or len(paths) > 60:
                raise FleetBlocked('This commit batch has no bounded file list.')
            base = str(self.item.get('base_sha') or '')
            patches, size = [], 0
            for raw in paths:
                path = _relative(raw)
                if base:
                    patch = self.git(['diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                                      base, self.head, '--', path]).stdout
                else:
                    patch = _read(self.root, path)
                size += len(patch.encode())
                if size > MAX_DIFF or 'Binary files ' in patch or 'GIT binary patch' in patch:
                    raise FleetBlocked('This change needs a manual review because its complete diff cannot be inspected.')
                patches.append({'path': path, 'patch': patch})
            return {'head_sha': self.head, 'base_sha': base,
                    'commit_subject': self.item.get('commit_subject'),
                    'truncated': bool(self.item.get('truncated')), 'files': patches}
        base_repo = _valid_repo((self.metadata['pull'].get('base', {}).get('repo') or {}).get('full_name') or self.repo)
        self.git(['fetch', '--no-tags', f'https://{self.cfg.github_host}/{base_repo}.git', self.base_sha])
        merge_base = self.git(['merge-base', self.base_sha, self.head]).stdout.strip()
        listing = self.git(['diff', '--name-only', '-z', merge_base, self.head]).stdout.split('\x00')
        paths = [p for p in listing if p]
        if len(paths) > MAX_FILES:
            raise FleetBlocked('This pull request is too large for one complete bounded review.')
        patches = []
        size = 0
        for path in paths:
            _relative(path)
            patch = self.git(['diff', '--no-ext-diff', '--no-textconv', '--no-renames', merge_base, self.head, '--', path]).stdout
            size += len(patch.encode())
            if size > MAX_DIFF or 'Binary files ' in patch or 'GIT binary patch' in patch:
                raise FleetBlocked('This change needs a manual review because its complete diff cannot be inspected.')
            patches.append({'path': path, 'patch': patch})
        return {'head_sha': self.head, 'base_sha': self.base_sha, 'merge_base': merge_base, 'files': patches}

    def marker(self, suffix):
        return '<!-- fatchin-fleet:v1:' + _hash(self.ident + ':' + suffix)[:32] + ' -->'

    def post(self, kind, path, body, *, marker, repo=None, intended_sha=None):
        """A marker alone is insufficient: reconcile exact body and author too."""
        repo = repo or self.repo
        self.preflight(repo, (self.metadata.get('pull') or {}).get('base', {}).get('ref') if repo == self.repo else self.source_ref)
        if self.dry_run:
            self.dry_run_actions.append({'kind': kind, 'path': path, 'repo': repo,
                                         'body': copy.deepcopy(body),
                                         'intended_sha': intended_sha})
            return {'dry_run': True, 'id': 0, 'number': 0, 'html_url': '',
                    'body': str(body.get('body') or '')}
        if not self.store.reserve_publication(
                self.item.get('fleet_id') or '', repo,
                getattr(self.cfg, 'max_fleet_publications_per_repo_per_day', 12)):
            raise FleetBlocked('The daily publication limit for this repository was reached. '
                               'No further public writes were sent today.')
        digest = _hash(body['body'])
        payload = {'target': path, 'expected_old_sha': self.head, 'body_sha256': digest,
                   'marker': marker, 'body': body, 'repository_id': self.repository(repo)['id'],
                   'intended_new_sha': intended_sha}
        operation = self.intent(kind, payload)
        if operation.get('state') == 'confirmed':
            return operation.get('result') or {}
        search_path = path
        if kind == 'create_pr':
            search_path = f'repos/{repo}/pulls'
            matches = self.gh.rest('GET', search_path, body={'state': 'all', 'head': repo.split('/')[0] + ':' + body['head']}, paginate=True)
        elif kind == 'create_issue':
            matches = self.gh.rest('GET', path, body={'state': 'all', 'creator': self.fleet.get('github_login'), 'per_page': 100}, paginate=True)
        else:
            matches = self.gh.rest('GET', search_path, body={'per_page': 100}, paginate=True)
        matches = matches or []
        if not isinstance(matches, list):
            raise FleetBlocked('The earlier GitHub write could not be reconciled safely.')
        found = [x for x in matches if isinstance(x, dict) and marker in str(x.get('body') or '')]
        if found:
            if len(found) != 1 or _hash(found[0].get('body') or '') != digest or (found[0].get('user') or {}).get('id') != self.fleet['github_id']:
                raise FleetBlocked('An earlier GitHub write has conflicting marker, body or account evidence.')
            if kind == 'create_pr':
                self._confirm_pr(found[0], repo, body, intended_sha)
            return self.receipt(operation, found[0])
        # An existing prepared operation with no remote result is ambiguous;
        # a crash can have happened after dispatch while GitHub is still settling.
        if any(x.get('id') == operation['id'] for x in self.known):
            raise FleetBlocked('An earlier GitHub write remains uncertain. No duplicate request was sent.')
        self.fresh()
        try:
            result = self.gh.rest('POST', path, body=body)
        except Exception:
            raise FleetRetry('GitHub did not confirm this write. Its saved intent will be reconciled before any retry.') from None
        if not isinstance(result, dict) or not result.get('id'):
            raise FleetBlocked('GitHub returned no confirmed write identity; the intent remains pending.')
        if kind == 'create_pr':
            self._confirm_pr(result, repo, body, intended_sha)
        return self.receipt(operation, result)

    def _confirm_pr(self, value, repo, body, intended_sha):
        head, base = value.get('head') or {}, value.get('base') or {}
        if (head.get('ref') != body['head'] or head.get('sha') != intended_sha or base.get('ref') != body['base']
                or (head.get('repo') or {}).get('full_name', '').casefold() != repo.casefold()
                or (base.get('repo') or {}).get('full_name', '').casefold() != repo.casefold()):
            raise FleetBlocked('The correction PR does not match the intended source commit and destination branch.')

    def open_pull_request(self):
        """Promote one pushed, same-repository branch to a draft pull request."""
        repository = self.metadata['repository']
        base = str(self.item.get('base_ref') or repository.get('default_branch') or '')
        if not base or base == self.source_ref:
            raise FleetBlocked('This branch has no pull-request destination.')
        self.active('Checking GitHub automation for a new pull request')
        self.preflight(self.repo, self.source_ref)
        subject = str(self.item.get('commit_subject') or '').strip() or f'Changes from {self.source_ref}'
        marker = self.marker('open-pr:' + self.source_ref)
        body = (f'Opened by FatChin for `{self.source_ref}` after a push.\n\n'
                f'A separate worker reviews commit `{self.head}` next. This pull request '
                'starts as a draft; opening it does not authorize a merge.\n\n' + marker)
        created = self.post('create_pr', f'repos/{self.repo}/pulls',
                            {'title': subject[:200], 'body': body, 'head': self.source_ref,
                             'base': base, 'draft': True},
                            marker=marker, intended_sha=self.head)
        if created.get('dry_run'):
            return {'status': 'done',
                    'summary': f'Dry run: would open a draft pull request for {self.source_ref}.',
                    'complete': True, 'ready': False, 'findings': []}
        number = created.get('number')
        if type(number) is not int:
            raise FleetBlocked('GitHub created the pull request without returning its number.')
        return {'status': 'done',
                'summary': f'Opened draft pull request #{number} for {self.source_ref}.',
                'complete': True, 'ready': False, 'findings': [],
                'created_pr': {'number': number, 'url': created.get('html_url')},
                'open_pr': {'number': number, 'url': created.get('html_url'),
                            'head_sha': self.head, 'branch': self.source_ref}}

    def review(self, model):
        evidence = self.diff_evidence()
        saved = [op for op in self.known if op.get('kind') == 'review_result']
        result = (saved[0]['payload']['decision'] if saved else RestrictedAgent(model, self.root, self.active, limit=self.cfg.max_tool_iterations, on_call=self.note_calls).run('review', {
            'repository': self.repo, 'pull_request': self.number, 'title': self.item.get('title'),
            'evidence': evidence, 'task': 'review_pr' if self.number else 'review_code'}))
        if not saved:
            self.intent('review_result', {'head_sha': self.head, 'decision': result})
        findings = result.get('findings')
        if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
            raise FleetBlocked('The model returned an invalid findings list.')
        output = self.observed_findings
        allowed_paths = {_relative(p) for p in
                         (self.item.get('paths') or [self.item.get('path')]) if p}
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                raise FleetBlocked('A finding has no structured evidence.')
            path = _relative(finding.get('path'))
            content = _read(self.root, path)
            line = finding.get('line')
            if type(line) is not int or not 1 <= line <= max(1, len(content.splitlines())):
                raise FleetBlocked('A finding does not cite an existing source line.')
            title, body = finding.get('title'), finding.get('body')
            if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip():
                raise FleetBlocked('A finding has no concrete explanation.')
            if not self.number and path not in allowed_paths:
                raise FleetBlocked('The background finding expanded beyond its assigned files.')
            fingerprint = _hash(json.dumps([self.repo_id, self.source_repo, self.source_ref, path, re.sub(r'\W+', ' ', title).strip().casefold()], ensure_ascii=False))
            marker = '<!-- fatchin-fleet:v1:finding:' + fingerprint + ' -->'
            text = f'{marker}\nFatChin finding: {_redact(title)[:160]}\n\n`{path}:{line}` at `{self.head}`\n\n{_redact(body)[:6000]}\n\nA separate worker will address only this finding. The comment stays open until its correction is merged.'
            payload = {'body': text}
            if self.number:
                endpoint, kind = f'repos/{self.repo}/issues/{self.number}/comments', 'comment'
            else:
                endpoint, kind = f'repos/{self.repo}/issues', 'create_issue'
                payload['title'] = 'FatChin review: ' + _redact(title)[:160]
            with _finding_lock(self.cfg, self.source_repo, fingerprint):
                existing = self.existing_finding(fingerprint, marker)
                if existing:
                    output.append({**existing, 'head_sha': self.head, 'base_sha': self.base_sha,
                                   'source_repo': self.source_repo, 'source_repo_id': self.source_id,
                                   'source_ref': self.source_ref})
                    continue
                posted = self.post(kind, endpoint, payload, marker=marker)
            output.append({'id': _hash(marker), 'fingerprint': fingerprint, 'title': title[:160],
                           'body': _redact(body), 'path': path, 'line': line, 'repo': self.repo,
                           'repo_id': self.repo_id, 'number': self.number, 'head_sha': self.head,
                           'base_sha': self.base_sha, 'source_repo': self.source_repo,
                           'source_repo_id': self.source_id, 'source_ref': self.source_ref,
                           'target_repo': self.source_repo, 'target_repo_id': self.source_id,
                           'target_ref': self.source_ref, 'root_finding_id': self.item.get('root_finding_id') or _hash(marker),
                           'correction_pr': self.item.get('correction_pr'),
                           'posted_comment_id': posted['id'], 'posted_issue_number': posted.get('number') if not self.number else None, 'marker': marker,
                           'url': posted.get('html_url') or self.item.get('url'), 'author_slot': self.item.get('slot')})
        if not findings and self.number:
            marker = self.marker('review-clear')
            text = f'{marker}\nFatChin reviewed `{self.head}`.\n\n{_redact(result["summary"])[:6000]}\n\nNo supported blocking defect was found in this review. GitHub checks, required approvals and mergeability are checked separately. This is a comment, not a separate GitHub-account approval.'
            posted = self.post('comment', f'repos/{self.repo}/issues/{self.number}/comments', {'body': text}, marker=marker)
            self.review_url = posted.get('html_url') or ''
        verification = self.verify_review() if self.number else {'checks': [], 'verification': 'This background review does not publish source.'}
        verified = not self.number or verification.get('verified_head_sha') == self.head
        readiness = {}
        if self.number and verified and not findings:
            # The final review hands the current merge evidence straight back
            # to the queue; account discovery remains a later freshness check.
            from .fleet_discovery import FleetDiscovery
            try:
                readiness, _, _ = FleetDiscovery(self.gh, self.cfg, self.store)._readiness(
                    self.repo, self.number, self.head, {'result': verification})
            except Exception:
                readiness = {'ready': False, 'head_sha': self.head,
                             'reason': 'Current merge requirements could not be confirmed yet.'}
        return {'status': 'done' if verified else 'blocked', 'summary': _redact(result['summary']),
                'error': '' if verified else verification.get('verification', 'Verification is pending.'), 'findings': output,
                'reviewed_head_sha': self.head, 'complete': True, 'ready': False,
                **verification, 'readiness': readiness, 'reviewer_slot': self.item.get('slot'),
                'review_url': self.review_url or (output[0].get('url') if output else '')}

    def existing_finding(self, fingerprint, marker):
        records = self.store.internal().get('items') or {}
        values = records.values() if isinstance(records, dict) else records
        for previous in values:
            if (previous.get('kind') == 'fix_comment' and previous.get('fingerprint') == fingerprint
                    and not previous.get('resolved_at') and previous.get('status') != 'dismissed'):
                return {k: value for k, value in previous.items() if k in {
                    'id', 'fingerprint', 'title', 'body', 'path', 'line', 'repo', 'repo_id', 'number',
                    'head_sha', 'base_sha', 'source_repo', 'source_repo_id', 'source_ref', 'target_repo',
                    'target_repo_id', 'target_ref', 'root_finding_id', 'correction_pr', 'posted_comment_id',
                    'posted_issue_number', 'marker', 'url', 'author_slot'}}
            if previous.get('id') == self.ident:
                continue
            for operation in previous.get('mutation_intents') or []:
                if (operation.get('payload') or {}).get('marker') == marker:
                    raise FleetRetry('This finding is already being published by another worker; its receipt will be reused after that task finishes.')
        return None

    def verify_review(self):
        try:
            verified_cfg = _repository_policy(self.cfg, self.gh, self.source_repo, self.source_ref, str(self.root))
            policy = next((r for r in verified_cfg.repos if r.full_name.casefold() == self.source_repo.casefold()), None)
            if policy is None or not policy.checks:
                return self.verify_github_checks()
            checks = _run_repository_checks(verified_cfg, str(self.root), policy.checks,
                                            lambda: not self.store.alive(self.ident, self.lease))
            self.active('Independent verification completed')
            clean = not self.git(['diff', '--name-only', 'HEAD']).stdout.strip()
            passed = clean and len(checks) == len(policy.checks) and all(c.get('passed') is True for c in checks)
            self.fresh()
            return {'checks': _safe_data(checks), 'verified_head_sha': self.head if passed else '',
                    'verification': 'Configured checks passed on this reviewed commit.' if passed else 'Verification failed or changed the reviewed files.'}
        except (FleetStopped, RateLimited):
            raise
        except Exception as exc:
            if any(term in str(exc).lower() for term in ('verification command', 'no recognized test or build')):
                return self.verify_github_checks()
            return {'checks': [], 'verification': 'Verification is pending: ' + _safe_error(exc),
                    'next_action': 'configure_checks' if any(term in str(exc).lower() for term in ('verification command', 'no recognized test or build')) else ''}

    def verify_github_checks(self):
        """Existing successful GitHub checks can verify a review-only task.

        Fix publication still requires local candidate checks. Never use this
        fallback to conceal a configured local command that actually failed.
        """
        owner, name = self.repo.split('/')
        query = '''query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){
          pullRequest(number:$number){headRefOid commits(last:1){nodes{commit{oid statusCheckRollup{state}}}}}}}'''
        try:
            response = self.gh.graphql(query, {'owner': owner, 'name': name, 'number': self.number})
            pr = ((response or {}).get('repository') or {}).get('pullRequest') or {}
            nodes = (pr.get('commits') or {}).get('nodes') or []
            commit = (nodes[-1].get('commit') or {}) if nodes else {}
            if (pr.get('headRefOid') == self.head and commit.get('oid') == self.head and
                    (commit.get('statusCheckRollup') or {}).get('state') == 'SUCCESS'):
                self.fresh()
                return {'checks': [], 'verified_head_sha': self.head, 'verification_source': 'GitHub',
                        'verification': 'GitHub checks passed on this exact reviewed commit.'}
        except (FleetStopped, RateLimited):
            raise
        except Exception:
            pass
        return {'checks': [], 'verification': 'No passing GitHub checks or recognized local verification command are available for this commit.',
                'next_action': 'configure_checks'}

    def prepare_finalize(self):
        self.identity()
        correction = (self.item.get('correction_pr') or self.item.get('fix_pr') or
                      (self.item.get('result') or {}).get('fix_pr'))
        proof = self.item.get('merge_proof') or {}
        if not isinstance(correction, dict) or not correction.get('target_ref'):
            raise FleetBlocked('The correction destination is missing; the original finding stays open.')
        self.correction = correction
        self.source_repo = _valid_repo(correction.get('repo'))
        self.source_id = self.repository(self.source_repo)['id']
        self.source_ref = correction['target_ref']
        self.repo_id = self.repository(self.repo)['id']
        if proof and (proof.get('repo') != self.source_repo or int(proof.get('number') or 0) != int(correction.get('number') or 0)):
            raise FleetBlocked('The merge proof belongs to a different correction.')

    def finalize(self):
        """Close only confirmed merged findings; never infer merge from closure."""
        if self.dry_run:
            self.dry_run_actions.append({'kind': 'finalize', 'item': self.ident,
                                         'branch': self.source_ref})
            return {'status': 'done',
                    'summary': 'Dry run: would resolve this thread or close its linked '
                               'issues after the confirmed merge. No GitHub write was sent.',
                    'complete': True, 'ready': False, 'findings': []}
        self.prepare_finalize()
        correction = self.correction
        pr = self.gh.rest('GET', f'repos/{self.source_repo}/pulls/{int(correction["number"])}')
        base = pr.get('base') or {}
        if (not pr.get('merged') or not pr.get('merged_at') or base.get('ref') != self.source_ref
                or (base.get('repo') or {}).get('id') != self.source_id):
            raise FleetBlocked('The correction has not been confirmed merged into its intended source branch.')
        merged = _valid_sha(pr.get('merge_commit_sha'))
        current = self._remote_ref(self.source_repo, self.source_ref)
        if not current:
            raise FleetBlocked('The corrected source branch is unavailable; its finding stays open.')
        ancestry = self.gh.rest('GET', f'repos/{self.source_repo}/compare/{merged}...{current}')
        if (ancestry.get('status') not in {'identical', 'ahead'} or
                (ancestry.get('merge_base_commit') or {}).get('sha') != merged):
            raise FleetBlocked('The correction merge is not confirmed in the current source branch.')
        policy = next((r for r in self.cfg.repos if r.full_name.casefold() == self.source_repo.casefold()), None)
        report = triggers.preflight(self.gh, self.source_repo, head_ref=self.source_ref,
                                    allow_workflows=False, allow_unknown_rulesets=bool(policy and policy.allow_unknown_rulesets))
        if report.verdict != triggers.SAFE:
            raise FleetBlocked('GitHub automation inspection held finding closure: ' + '; '.join(report.reasons))
        thread_id = self.item.get('thread_id')
        if thread_id:
            query = """query($id:ID!){node(id:$id){... on PullRequestReviewThread{
              id isResolved viewerCanResolve pullRequest{number repository{databaseId nameWithOwner}}
              comments(last:1){nodes{databaseId updatedAt}}}}}"""
            thread = (self.gh.graphql(query, {'id': thread_id}) or {}).get('node') or {}
            pull = thread.get('pullRequest') or {}
            if thread.get('id') != thread_id or pull.get('number') != self.number or (pull.get('repository') or {}).get('databaseId') != self.repo_id:
                raise FleetBlocked('The review thread does not belong to the recorded source pull request.')
            latest = (((thread.get('comments') or {}).get('nodes') or [{}])[-1]).get('databaseId')
            expected = self.item.get('comment_id') or self.item.get('posted_comment_id')
            if expected is not None:
                numeric = re.search(r'(?:^|:)([0-9]+)$', str(expected))
                if not numeric:
                    raise FleetBlocked('The original review comment identity is invalid.')
                expected = int(numeric[1])
            if not thread.get('isResolved') and expected and latest != expected:
                raise FleetBlocked('Newer feedback exists on this thread; it must be reviewed before resolution.')
            if not thread.get('isResolved') and not thread.get('viewerCanResolve'):
                raise FleetBlocked('This GitHub account cannot resolve the reviewed thread.')
            payload = {'target': thread_id, 'repository_id': self.repo_id, 'expected_old_sha': self.head,
                       'merge_commit_sha': merged, 'source_head_sha': current,
                       'marker': self.marker('resolve-thread'), 'body_sha256': _hash(str(thread_id) + merged)}
            operation = self.intent('resolve_thread', payload)
            if not thread.get('isResolved'):
                self.identity()
                try:
                    resolved = self.gh.graphql('mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}', {'id': thread_id})
                except Exception:
                    raise FleetRetry('The thread resolution response was lost; the next task will verify its current state before retrying.') from None
                node = ((resolved or {}).get('resolveReviewThread') or {}).get('thread') or {}
                if node.get('id') != thread_id or node.get('isResolved') is not True:
                    raise FleetRetry('GitHub has not confirmed review-thread resolution.')
            self.receipt(operation, {'thread_id': thread_id, 'resolved': True, 'merge_commit_sha': merged})
        issue_number = self.item.get('posted_issue_number')
        if issue_number:
            endpoint = f'repos/{self.repo}/issues/{int(issue_number)}'
            issue = self.gh.rest('GET', endpoint)
            if ((issue.get('user') or {}).get('id') != self.fleet['github_id'] or
                    not self.item.get('marker') or self.item['marker'] not in str(issue.get('body') or '')):
                raise FleetBlocked('The background issue is not the recorded fleet finding; it was not closed.')
            payload = {'target': endpoint, 'repository_id': self.repo_id, 'expected_old_sha': self.head,
                       'merge_commit_sha': merged, 'marker': self.item['marker'],
                       'body_sha256': _hash(json.dumps({'state': 'closed', 'state_reason': 'completed'}, sort_keys=True))}
            operation = self.intent('close_finding', payload)
            if issue.get('state') != 'closed':
                self.identity()
                try:
                    closed = self.gh.rest('PATCH', endpoint, body={'state': 'closed', 'state_reason': 'completed'})
                except Exception:
                    raise FleetRetry('The finding closure response was lost; its state will be checked before retrying.') from None
                if closed.get('state') != 'closed':
                    raise FleetRetry('GitHub has not confirmed finding closure.')
            self.receipt(operation, {'issue_number': int(issue_number), 'closed': True, 'merge_commit_sha': merged})
        return {'status': 'done', 'resolved': True, 'summary': 'The correction is merged into its intended branch; the original finding is resolved.',
                'fix_pr': correction, 'merge_commit_sha': merged, 'ready': False, 'checks': []}

    def _remote_ref(self, repo, branch):
        try:
            value = self.gh.rest('GET', f'repos/{repo}/git/ref/heads/{quote(branch, safe="")}')
        except NotFound:
            return None
        return _valid_sha((value.get('object') or {}).get('sha'))

    def _push(self, data, operation=None):
        repo, branch = data['repo'], data['branch']
        if data.get('source_repo') != self.source_repo or data.get('source_ref') != self.source_ref:
            raise FleetBlocked('The saved correction source no longer matches this pull request.')
        self.fresh()
        if self.repository(repo)['id'] != data['repository_id']:
            raise FleetBlocked('The correction destination repository changed.')
        remote = self._remote_ref(repo, branch)
        intended, expected = data['prepared_commit_sha'], data['expected_old_sha']
        if remote == intended:
            if operation is None:
                operation = self.intent('push', data)
            self.receipt(operation, {'sha': intended, 'branch': branch, 'repo': repo})
            return
        if remote != expected:
            raise FleetBlocked('The correction branch moved or conflicts with another writer. Nothing was overwritten.')
        checkout = Path(data['prepared_checkout'])
        scratch = (Path(self.cfg.worktree_root) / 'fleet-v1').resolve()
        if not checkout.is_dir() or not checkout.resolve().is_relative_to(scratch) or checkout.is_symlink():
            raise FleetBlocked('The original prepared commit is unavailable. It was not regenerated or pushed.')
        if self.git(['rev-parse', 'HEAD'], cwd=checkout).stdout.strip() != intended:
            raise FleetBlocked('The original prepared checkout no longer matches its saved commit.')
        self.preflight(repo, data['target_ref'])
        operation = operation or self.intent('push', data)
        # Intent is the atomic admission boundary. Stop after this point may
        # not cancel an already-transmitted request; recovery probes this SHA.
        proc = self.git(['push', '--', f'https://{self.cfg.github_host}/{repo}.git',
                         intended + ':refs/heads/' + branch], cwd=checkout, check=False)
        confirmed = self._remote_ref(repo, branch)
        if confirmed != intended:
            raise FleetRetry('GitHub did not confirm the exact prepared commit. The push intent remains pending; no new commit will be generated on retry.')
        self.receipt(operation, {'sha': intended, 'branch': branch, 'repo': repo})

    def _publish_fix(self, data, operation=None):
        if self.dry_run:
            self.dry_run_actions.append({
                'kind': 'push', 'repo': data['repo'], 'branch': data['branch'],
                'head_sha': data['prepared_commit_sha'],
                'target_ref': data['target_ref'],
                'diffstat': self.git(['show', '--stat', '--format=%h %s', 'HEAD']).stdout[:4000]})
            return {'status': 'done',
                    'summary': 'Dry run: would push this correction branch and open its '
                               'pull request. No GitHub write was sent.',
                    'checks': data.get('checks') or [], 'ready': False}
        self._push(data, operation)
        correction = data.get('correction_pr')
        if correction:
            pr = self.gh.rest('GET', f'repos/{data["repo"]}/pulls/{int(correction["number"])}')
            if pr.get('state') != 'open' or (pr.get('head') or {}).get('sha') != data['prepared_commit_sha']:
                raise FleetBlocked('The updated correction pull request could not be confirmed.')
            result_pr = {**correction, 'head_sha': data['prepared_commit_sha'],
                         'repair_round': int(correction.get('repair_round') or 0) + 1,
                         'author_slot': self.item.get('slot')}
        else:
            marker = data['pr_marker']
            posted = self.post('create_pr', f'repos/{data["repo"]}/pulls',
                               {'title': data['pr_title'], 'body': data['pr_body'],
                                'head': data['branch'], 'base': data['target_ref'], 'draft': False},
                               marker=marker, repo=data['repo'], intended_sha=data['prepared_commit_sha'])
            result_pr = {'repo': data['repo'], 'repo_id': data['repository_id'],
                         'number': posted['number'], 'url': posted.get('html_url'),
                         'branch': data['branch'], 'head_sha': data['prepared_commit_sha'],
                         'target_ref': data['target_ref'], 'source_repo': self.source_repo,
                         'source_ref': self.source_ref, 'root_number': self.number,
                         'root_finding_id': data['root_finding_id'], 'repair_round': 0,
                         'author_slot': self.item.get('slot')}
        return {'status': 'done', 'summary': data['summary'], 'fix_pr': result_pr,
                'checks': data['checks'], 'ready': False,
                'source_comment_url': self.item.get('url'), 'root_finding_id': data['root_finding_id'],
                'published_commit': data['prepared_commit_sha']}

    def resume_fix(self):
        pushes = [op for op in self.known if op.get('kind') == 'push']
        if not pushes:
            return None
        if len(pushes) != 1:
            raise FleetBlocked('Several prepared commits exist for this item; manual reconciliation is required.')
        op = pushes[0]
        data = op.get('payload') or {}
        retained = Path(str(data.get('prepared_checkout') or ''))
        scratch = (Path(self.cfg.worktree_root) / 'fleet-v1').resolve()
        if retained.is_dir() and retained.resolve().is_relative_to(scratch) and not retained.is_symlink():
            try:
                owned = json.loads((retained.parent / 'owner.json').read_text())
            except (OSError, ValueError):
                owned = {}
            if owned.get('item_id') == self.ident:
                self.root = retained
        if data.get('github_id') != self.fleet.get('github_id') or data.get('host') != self.cfg.github_host:
            raise FleetBlocked('The saved publication belongs to a different GitHub account or host.')
        # A correction repair already moved the very branch it started from.
        # Inspect the original expected source while permitting only our saved
        # exact result, then restore the ordinary current-head fencing.
        if data.get('correction_pr'):
            current = self._remote_ref(data['repo'], data['branch'])
            if current == data['prepared_commit_sha']:
                self.head = current
                self.item = {**self.item, 'head_sha': current}
        self.inspect()
        return self._publish_fix(data, op)

    def fix(self, model):
        correction = self.item.get('correction_pr')
        if correction and int(correction.get('repair_round') or 0) >= 3:
            raise FleetBlocked('This finding has reached three correction rounds. It needs a person’s review before more edits.')
        destination = self.repository(self.source_repo)
        if not (destination.get('permissions') or {}).get('push') or destination.get('archived') or destination.get('disabled'):
            raise FleetBlocked('This GitHub account cannot publish a correction to the source repository or fork.')
        verified_cfg = _repository_policy(self.cfg, self.gh, self.source_repo, self.source_ref, str(self.root))
        policy = next((r for r in verified_cfg.repos if r.full_name.casefold() == self.source_repo.casefold()), None)
        if policy is None or not policy.checks:
            raise FleetBlocked('Add verification commands for this source repository in Settings before automatic fixes can publish.')
        evidence = {'comment': self.item.get('body') or self.item.get('comment_body'),
                    'path': self.item.get('path'), 'line': self.item.get('line'),
                    'head_sha': self.head, 'title': self.item.get('title'), 'url': self.item.get('url')}
        agent = RestrictedAgent(model, self.root, self.active, limit=min(20, self.cfg.max_tool_iterations), on_call=self.note_calls)
        decision = agent.run('classify', evidence)
        if decision.get('actionable') is False:
            return {'status': 'dismissed', 'summary': _redact(decision['summary']),
                    'ready': False, 'checks': [], 'actionable': False}
        if decision.get('actionable') is not True:
            raise FleetBlocked('The comment’s requested fix remains unclear; no edit was made.')
        paths = decision.get('paths')
        if not isinstance(paths, list) or not 1 <= len(paths) <= 3:
            raise FleetBlocked('The comment does not identify a bounded set of source files.')
        paths = sorted({_relative(path) for path in paths})
        if self.item.get('path'):
            cited = _relative(self.item['path'])
            body = str(evidence['comment'] or '')
            for path in set(paths) - {cited}:
                requested_test = bool(re.search(r'\btests?\b', body, re.I)) and any('test' in part.lower() for part in Path(path).parts)
                if path not in body and not requested_test:
                    raise FleetBlocked('The proposed fix expanded beyond the finding’s cited file.')
        for path in paths:
            candidate = _path(self.root, path, existing=False)
            if candidate.exists():
                _read(self.root, path)
        result = RestrictedAgent(model, self.root, self.active, limit=self.cfg.max_tool_iterations, on_call=self.note_calls).run(
            'fix', {**evidence, 'allowed_paths': paths, 'minimal_fix': decision['summary']}, paths)
        changed = [p for p in (self.git(['diff', '--name-only', '-z']).stdout + self.git(['ls-files', '--others', '--exclude-standard', '-z']).stdout).split('\x00') if p]
        if not changed:
            return {'status': 'dismissed', 'summary': 'The model produced no source change. ' + _redact(result['summary']), 'ready': False, 'checks': []}
        if set(changed) - set(paths):
            raise FleetBlocked('The candidate changed files outside the single finding’s scope.')
        self.git(['add', '--', *changed])
        before = self.git(['write-tree']).stdout.strip()
        self.active('Running configured verification commands')
        checks = _run_repository_checks(verified_cfg, str(self.root), policy.checks, lambda: not self.store.alive(self.ident, self.lease))
        self.recorded_checks = _safe_data(checks)
        self.active('Verification commands finished')
        if len(checks) != len(policy.checks) or any(c.get('passed') is not True for c in checks):
            names = ', '.join(str(c.get('command')) for c in checks if c.get('passed') is not True)
            raise FleetBlocked('Configured verification failed; no correction was published. ' + names[:300])
        if self.git(['diff', '--name-only']).stdout.strip() or self.git(['ls-files', '--others', '--exclude-standard']).stdout.strip():
            raise FleetBlocked('A verification command changed files. The checked candidate was not published.')
        if self.git(['write-tree']).stdout.strip() != before:
            raise FleetBlocked('The prepared files changed during verification.')
        from .task_write_guard import _inspect_prepared_tree
        def checked_git(args, cwd):
            out = self.git(args, cwd=cwd, check=False)
            return out.returncode, out.stdout, out.stderr
        report = _inspect_prepared_tree(checked_git, str(self.root), before, False)
        if report.verdict != triggers.SAFE:
            raise FleetBlocked('The prepared source introduces GitHub automation or LFS; publishing was held.')
        self.preflight()
        root_finding = str(self.item.get('root_finding_id') or self.ident)
        branch = str(correction['branch']) if correction else 'fatchin/fix-' + _hash(root_finding)[:24]
        if correction and (correction.get('repo') != self.source_repo or branch != self.source_ref):
            raise FleetBlocked('The correction’s recorded branch does not match its current source branch.')
        expected = self.head if correction else None
        if self._remote_ref(self.source_repo, branch) != expected:
            raise FleetBlocked('The deterministic correction branch already exists or moved; it was not overwritten.')
        self.git(['-c', 'user.name=FatChin', '-c', 'user.email=fatchin@inference',
                  '-c', 'commit.gpgSign=false', 'commit', '-m', 'fix: ' + str(self.item.get('title') or 'address the recorded review finding')[:120]])
        commit = _valid_sha(self.git(['rev-parse', 'HEAD']).stdout.strip())
        if self.git(['rev-parse', 'HEAD^{tree}']).stdout.strip() != before:
            raise FleetBlocked('The committed source differs from the verified candidate.')
        marker = self.marker('correction-pr')
        summary = _redact(result['summary'])[:6000]
        body = f'{marker}\nAddresses only the recorded finding: {self.item.get("url") or self.ident}\n\n{summary}\n\nBase source: `{self.head}`.\n\nVerification: ' + ', '.join(_redact(str(c['command'])) for c in checks) + '\n\nA different FatChin slot must review this head. The original finding remains open until this correction is merged.'
        data = {'repo': self.source_repo, 'repository_id': self.source_id, 'branch': branch,
                'source_repo': self.source_repo, 'source_ref': self.source_ref,
                'target_ref': correction.get('target_ref', self.source_ref) if correction else self.source_ref,
                'expected_old_sha': expected, 'prepared_commit_sha': commit,
                'prepared_checkout': str(self.root), 'marker': marker, 'root_finding_id': root_finding,
                'summary': summary, 'checks': _safe_data(checks), 'correction_pr': correction,
                'pr_marker': marker, 'pr_title': 'fix: ' + _redact(str(self.item.get('title') or 'address review finding'))[:160],
                'pr_body': body, 'body_sha256': _hash(body)}
        return self._publish_fix(data)


def _run_repository_checks(cfg, root, checks, should_stop):
    from .fleet_checks import run_repository_checks
    return run_repository_checks(cfg, root, checks, should_stop)


def _repository_policy(cfg, gh, repo, ref, worktree):
    from .fleet_checks import auto_repository_policy
    try:
        return auto_repository_policy(cfg, gh, repo, ref, worktree)
    except ValueError as exc:
        raise FleetBlocked(str(exc)) from None


@contextlib.contextmanager
def _model(cfg, env, item, factory):
    provider = item.get('provider', 'glm')
    if provider not in {'glm', 'deepseek'}:
        raise FleetBlocked('The selected fleet provider is unsupported.')
    if factory is not None:
        value = factory(cfg, env, item)
        if hasattr(value, '__enter__'):
            with value as model:
                yield model
        else:
            yield value
        return
    from .fleet_providers import model_config
    resolved, model_env = model_config(cfg, env, provider)
    model_cfg = dataclasses.replace(resolved.model, max_calls_per_job=100, max_calls_per_day=sys.maxsize)
    bearer = make_bearer(dataclasses.replace(resolved, model=model_cfg), model_env)
    try:
        model = ModelClient(model_cfg, bearer, job_scope='fleet-' + str(item['id']),
                            day_counter_path=str(Path(cfg.state_dir) / 'fleet-v1' / (provider + '-model-call-counts.json')),
                            max_attempts=1)
        yield model
    finally:
        close = getattr(bearer, 'close', None)
        if close:
            close()


@contextlib.contextmanager
def _finding_lock(cfg, repo, fingerprint):
    from .fleet_locks import target_lock
    with target_lock(cfg.state_dir, repo, 'finding:' + fingerprint, blocking=True):
        yield


@contextlib.contextmanager
def _locks(cfg, task):
    from .fleet_locks import repo_lock, target_lock
    names = sorted({task.repo, task.source_repo})
    with contextlib.ExitStack() as stack:
        for repo in names:
            stack.enter_context(repo_lock(cfg.state_dir, repo, exclusive=False, blocking=True))
        target = task.source_ref if task.item.get('kind') == 'fix_comment' else 'review:' + str(task.number or task.item.get('path')) + ':' + task.head
        stack.enter_context(target_lock(cfg.state_dir, task.source_repo, target, blocking=True))
        yield


@contextlib.contextmanager
def _destination(fleet):
    destination = Path(str(fleet.get('output_dir') or ''))
    home = Path(str(fleet.get('member_home') or ''))
    if not destination.is_absolute() or not home.is_absolute() or not destination.is_relative_to(home):
        raise FleetBlocked('The fleet needs an existing destination inside the member’s account folder.')
    if '..' in destination.parts or '..' in home.parts:
        raise FleetBlocked('The destination must be a direct folder inside the member’s account.')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in destination.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd, destination
    finally:
        os.close(fd)


def _report(item, result):
    with _destination(item.get('fleet') or {}) as (fd, destination):
        folder = 'fatchin-fleet-' + _hash(item['id'])[:24] + '-' + _hash(item['lease'])[:10]
        os.mkdir(folder, mode=0o2770, dir_fd=fd)
        child = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            from .results import grant_member_access
            grant_member_access(f'/proc/{os.getpid()}/fd/{child}', item['fleet']['member_home'])
            output = os.open('report.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=child)
        finally:
            os.close(child)
        with os.fdopen(output, 'w') as handle:
            json.dump(_safe_data({'item_id': item['id'], 'repo': item['repo'], 'result': result}), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return str(destination / folder / 'report.json')


def execute_item(cfg, env, item, store, *, gh=None, model_factory=None, runner=None):
    """Run one fresh bounded item; the caller persists its final result."""
    task = None
    try:
        if gh is None:
            gh = GitHubClient(host=cfg.github_host, api_version=cfg.rest_api_version)
            for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
                if env.get(key):
                    gh._env[key] = env[key]
        task = Task(cfg, env, item, store, gh, runner)
        if item.get('kind') not in {'review_pr', 'fix_comment', 'review_code', 'open_pr'}:
            raise FleetBlocked('This fleet item type is unsupported.')
        if item.get('author_slot') is not None and item.get('author_slot') == item.get('slot'):
            raise FleetBlocked('A different worker slot must take the next review or correction stage.')
        # Resume publication without a second model conversation or regenerated
        # commit. The supervisor only reclaims after proving the old child gone.
        if item.get('phase') == 'resolve_thread':
            task.prepare_finalize()
            with _locks(cfg, task):
                result = task.finalize()
        elif item.get('kind') == 'fix_comment' and any(op.get('kind') == 'push' for op in task.known):
            prior = next(op['payload'] for op in task.known if op.get('kind') == 'push')
            if prior.get('correction_pr') and task._remote_ref(prior['repo'], prior['branch']) == prior['prepared_commit_sha']:
                task.head = prior['prepared_commit_sha']
            task.inspect()
            with _locks(cfg, task):
                result = task.resume_fix()
        elif item.get('kind') == 'open_pr':
            task.inspect()
            with _locks(cfg, task):
                task.inspect()
                result = task.open_pull_request()
                result['model_calls'] = 0
        else:
            task.inspect()
            with _locks(cfg, task):
                task.inspect()
                task.clone()
                with _model(cfg, env, item, model_factory) as model:
                    result = task.fix(model) if item['kind'] == 'fix_comment' else task.review(model)
                    result['model_calls'] = int(getattr(model, 'calls_used', 0) or 0)
        if getattr(task, 'dry_run', False):
            result['dry_run_actions'] = copy.deepcopy(task.dry_run_actions)
        result['report_path'] = _report(item, result)
        return result
    except Exception as exc:
        status = getattr(exc, 'http_status', None)
        if status is None:
            match = re.search(r'HTTP ([45][0-9]{2})\b', str(exc))
            status = int(match[1]) if match else None
        rate_limited = status == 429 or isinstance(exc, RateLimited)
        transient = isinstance(exc, (FleetRetry, TimeoutError, subprocess.TimeoutExpired))
        if isinstance(exc, (ModelError, GitHubError)) and not isinstance(exc, (NotFound, SpendLimitExceeded)):
            transient = status not in {400, 401, 403, 404, 422}
        transient = transient or rate_limited
        delay = (getattr(exc, 'retry_after', None) or getattr(exc, 'retry_after_seconds', None) or 900) if rate_limited else 30
        retry_kind = 'rate_limit' if rate_limited else 'service_unavailable' if isinstance(exc, (ModelError, GitHubError, TimeoutError)) and transient else 'reconcile_write' if isinstance(exc, FleetRetry) else ''
        return {'status': 'retrying' if transient else 'blocked', 'retry_after': delay if transient else None,
                'retry_kind': retry_kind, 'retry_scope': 'github' if isinstance(exc, GitHubError) else 'provider',
                'auto_recheck': not isinstance(exc, SpendLimitExceeded) and 'three correction rounds' not in str(exc),
                'summary': _safe_error(exc), 'error': _safe_error(exc),
                'ready': False, 'findings': task.observed_findings if task else [],
                'checks': task.recorded_checks if task else [],
                'next_action': 'configure_checks' if any(text in str(exc).lower() for text in ('verification command', 'no recognized test or build')) else '',
                'recovery_path': str(task.root) if task and task.root else None}
    finally:
        # Prepared unpublished commits are retained for exact-SHA recovery.
        # Only this task's own temp tree is eligible for automatic cleanup.
        if task and task.root:
            intents = task.store.internal()
            saved = intents.get('items', {})
            if isinstance(saved, list):
                saved = {x.get('id'): x for x in saved}
            record = saved.get(task.ident) or {}
            pending = any(op.get('kind') == 'push' and op.get('state') != 'confirmed'
                          for op in record.get('mutation_intents', []))
            if not pending and task.root.parent.exists():
                # Only disposable paths bearing this exact item's ownership
                # record may be removed, including a recovered prepared tree.
                owner = json.loads((task.root.parent / 'owner.json').read_text())
                if owner.get('item_id') == task.ident:
                    shutil.rmtree(task.root.parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Execute exactly one FatChin fleet item, then retire.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--item', required=True)
    parser.add_argument('--lease', required=True)
    args = parser.parse_args(argv)
    from .config_loader import load_config, load_env_file
    from .fleet_store import FleetStore
    cfg = load_config(args.config)
    env = {**os.environ, **load_env_file(args.env_file)}
    store = FleetStore(Path(cfg.state_dir) / 'fleet-v1')
    internal = store.internal()
    items = internal.get('items') or {}
    item = items.get(args.item) if isinstance(items, dict) else next((x for x in items if x.get('id') == args.item), None)
    if not item or item.get('lease') != args.lease or not store.alive(args.item, args.lease):
        return 2
    if not item.get('fleet'):
        fleets = internal.get('fleets') or {}
        item['fleet'] = fleets.get(item.get('fleet_id')) if isinstance(fleets, dict) else next((x for x in fleets if x.get('id') == item.get('fleet_id')), {})
    stop = threading.Event()
    def pulse():
        while not stop.wait(5):
            if not store.heartbeat(args.item, args.lease, 'Working on the assigned task'):
                return
    heartbeat = threading.Thread(target=pulse, daemon=True)
    heartbeat.start()
    try:
        result = execute_item(cfg, env, item, store)
        if not store.alive(args.item, args.lease):
            return 2
        store.finish(args.item, args.lease, result)
        return 0 if result.get('status') in {'done', 'dismissed'} else 1
    finally:
        stop.set()
        heartbeat.join(timeout=6)


if __name__ == '__main__':
    raise SystemExit(main())
