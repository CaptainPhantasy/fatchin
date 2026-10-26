"""The shared checks/automation gate for task-managed branch writes.

Only the caller's Config and persisted job policy are authoritative. Draft
text and incoming context cannot waive checks or GitHub automation inspection.
All refusals here precede the first remote mutation; callers may safely keep
the draft and retry them. A failed push or PR creation is a different contract.
"""
from __future__ import annotations

import re

from . import triggers
from .coding_agent import CodingAgent
from .github_client import redact_text
from .interfaces import Config


class PreWriteRefusal(ValueError):
    """A confirmed refusal before any remote mutation, safe to retry."""


def _refuse(message: str) -> None:
    raise PreWriteRefusal(redact_text(message))


def _stop_requested(context: dict) -> bool:
    """A trusted caller may supply queue/global Stop without exposing storage."""
    check = context.get('stop_check')
    if check is None:
        return False
    if not callable(check):
        raise ValueError('The task Stop status could not be verified.')
    try:
        return bool(check())
    except Exception:
        raise ValueError('The task Stop status could not be verified.') from None


def ensure_not_stopped(context: dict, *, after_write: bool = False) -> None:
    """Hold writes on Stop or unreadable Stop; never mark a partial write safe."""
    if after_write:
        # A task may push with git before using REST. Carry that fact into
        # a later guarded REST call, even when Stop was not set at this check.
        context['_github_write_attempted'] = True
    after_write = context.get('_github_write_attempted') is True
    try:
        stopped = _stop_requested(context)
    except ValueError as exc:
        message = str(exc)
    else:
        if not stopped:
            return
        message = 'The task was stopped.'
    if after_write:
        raise ValueError(message + ' An earlier GitHub write was already attempted; '
                         'its outcome may be incomplete. No further write was started. '
                         'Check GitHub before starting another task.')
    _refuse(message + ' Nothing was sent to GitHub.')


class GuardedGitHub:
    """Check trusted Stop immediately before each task's REST mutation.

    Reads and other client attributes are delegated unchanged. This covers
    REST writes through this wrapper; task-managed git/CLI writes still need
    their explicit boundary checks. A failed request may already have reached
    GitHub, so an attempt is recorded before entering the client.
    """
    def __init__(self, gh, context: dict):
        self._gh = gh
        self._context = context
        self.write_attempted = context.get('_github_write_attempted') is True

    def __getattr__(self, name):
        return getattr(self._gh, name)

    def rest(self, method, path, *args, **kwargs):
        if method.upper() not in {'GET', 'HEAD', 'OPTIONS'}:
            ensure_not_stopped(self._context, after_write=self.write_attempted)
            self.write_attempted = True
            self._context['_github_write_attempted'] = True
        return self._gh.rest(method, path, *args, **kwargs)


def repository_policy(context: dict, job):
    """Require current trusted settings and this job's explicit no-check waiver."""
    cfg = context.get('config')
    if not isinstance(cfg, Config):
        _refuse('Trusted repository settings are missing. Nothing was pushed; '
                'reload the workspace before choosing Post again.')
    repo = str(getattr(job, 'repo_full_name', '') or '')
    if not repo or (context.get('repo') and context['repo'] != repo):
        _refuse('The draft repository does not match the saved task. Nothing was pushed.')
    configured = next((r for r in cfg.repos if r.full_name == repo), None)
    if configured is None:
        _refuse('This repository is no longer configured. Nothing was pushed; '
                'add it in Settings before choosing Post again.')
    request = (getattr(job, 'notes', None) or {}).get('request') or {}
    acknowledged = isinstance(request, dict) and request.get('accept_no_checks') is True
    if not configured.checks and not acknowledged:
        _refuse('This repository has no verification commands. Nothing was pushed; '
                'add checks in Settings, or start a new task and explicitly '
                'accept unverified changes for that one task.')
    return cfg, configured


def _git(git, args: list[str], worktree: str, action: str) -> str:
    try:
        rc, out, err = git(args, cwd=worktree)
    except Exception:
        _refuse(f'The {action} could not be verified. Nothing was pushed.')
    if rc:
        _refuse(f'The {action} could not be verified. Nothing was pushed.')
    return out or ''


def _oid(git, args: list[str], worktree: str) -> str:
    value = _git(git, args, worktree, 'prepared checkout').strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}', value):
        _refuse('The prepared checkout has no known commit or file tree. Nothing was pushed.')
    return value


def commit_id(git, worktree: str, ref: str) -> str:
    return _oid(git, ['rev-parse', '--verify', ref], worktree)


def _snapshot(git, worktree: str) -> tuple[str, str]:
    head = _oid(git, ['rev-parse', '--verify', 'HEAD'], worktree)
    tree = _oid(git, ['write-tree'], worktree)
    _git(git, ['diff', '--quiet', '--'], worktree,
         'match between the staged files and the checkout')
    return head, tree


def ensure_prepared_tree(git, worktree: str, tree: str) -> str:
    """Bind a successful check result to the exact commit about to be pushed."""
    head, staged = _snapshot(git, worktree)
    committed = _oid(git, ['rev-parse', '--verify', 'HEAD^{tree}'], worktree)
    if tree != staged or tree != committed:
        _refuse('The prepared files changed after verification. Nothing was pushed; '
                'review the checkout and run the task again.')
    return head


def _inspect_prepared_tree(git, worktree: str, tree: str,
                           allow_workflows: bool) -> triggers.TriggerReport:
    """Inspect the actual candidate tree, including newly introduced automation.

    GitHub cannot read an unpushed merge/commit. Reuse the existing trigger
    scanner on those local blobs as well as its normal remote inspection.
    """
    report = triggers.TriggerReport(details={'workflows': {}, 'prepared_tree': tree})
    listing = _git(git, ['ls-tree', '-r', '-z', tree, '--',
                         '.github/workflows', '.gitattributes'], worktree,
                   'automation in the prepared files')
    workflows = 0
    for entry in listing.split('\0'):
        if not entry:
            continue
        try:
            metadata, path = entry.split('\t', 1)
            mode, kind, oid = metadata.split()
        except ValueError:
            _refuse('Automation in the prepared files could not be read. Nothing was pushed.')
        if path != '.gitattributes' and not path.startswith('.github/workflows/'):
            continue
        if kind != 'blob' or mode not in {'100644', '100755'}:
            _refuse('Automation in the prepared files is not a regular file. Nothing was pushed.')
        size = _git(git, ['cat-file', '-s', oid], worktree, 'automation file size').strip()
        if not size.isdecimal() or int(size) > triggers.MAX_WORKFLOW_BYTES:
            _refuse('Automation in the prepared files is too large to inspect. Nothing was pushed.')
        text = _git(git, ['show', f'{tree}:{path}'], worktree, 'automation file contents')
        if path == '.gitattributes':
            if triggers._has_lfs_filter(text):
                report.reasons.append('The prepared .gitattributes uses Git LFS; writes are held.')
            continue
        workflows += 1
        if workflows > triggers.MAX_WORKFLOW_FILES:
            _refuse('There are too many prepared workflows to inspect. Nothing was pushed.')
        triggers._flag_workflow_text(path, 'prepared checkout', text,
                                    allow_workflows, report)
    report.verdict = triggers.REVIEW if report.reasons else triggers.SAFE
    return report


def verify_before_write(context: dict, gh, job, worktree: str, git,
                        refs: list[str]) -> str:
    """Run configured checks and inspect all relevant automation; return tested tree.

    The existing CodingAgent check runner is used without running an agent or
    a model. This preserves its argv parsing, timeout, output cap, and scrubbed
    child environment. The staged tree must match the files the checks read.
    """
    cfg, repo_cfg = repository_policy(context, job)
    ensure_not_stopped(context)
    notes = job.notes
    before = _snapshot(git, worktree)
    runner = CodingAgent(None, cfg)
    runner._active_worktree = worktree
    def should_stop():
        # Keep completed check evidence, then raise the typed refusal below.
        try:
            return _stop_requested(context)
        except ValueError:
            return True
    checks = runner._run_checks(list(repo_cfg.checks), should_stop)
    notes['checks'] = [{**c, 'command': redact_text(str(c.get('command', ''))),
                        'output_tail': redact_text(str(c.get('output_tail', '')))}
                       for c in checks]
    notes['tested_base'], notes['tested_tree'] = before
    ensure_not_stopped(context)
    failed = [c for c in checks if c.get('passed') is not True]
    if failed or len(checks) != len(repo_cfg.checks):
        notes['verification'] = 'Configured repository checks failed; all writes were held.'
        name = str(failed[0].get('command', 'unknown check')) if failed else 'missing check result'
        _refuse(f'Repository verification failed: {name}. Nothing was pushed; '
                'read the check results and correct the draft before trying again.')
    if before != _snapshot(git, worktree):
        _refuse('A verification command changed the prepared checkout. Nothing was pushed; '
                'review the changes and run the task again.')
    # The single-task waiver is deliberately not described as passed checks.
    notes['verification'] = (f'All {len(checks)} configured checks passed on the prepared files.'
                             if checks else 'No repository checks configured; this task explicitly accepts unverified changes.')
    allow_workflows = notes.get('allow_workflows') is True
    reports = [_inspect_prepared_tree(git, worktree, before[1], allow_workflows)]
    for ref in dict.fromkeys(refs):
        ensure_not_stopped(context)
        try:
            report = triggers.preflight(
                gh, job.repo_full_name, head_ref=ref,
                allow_workflows=allow_workflows,
                allow_unknown_rulesets=repo_cfg.allow_unknown_rulesets is True)
        except Exception:
            _refuse('GitHub automation inspection was unreadable. Nothing was pushed; try again later.')
        reports.append(report)
        ensure_not_stopped(context)
    reasons = [reason for report in reports for reason in report.reasons]
    verdict = triggers.SAFE if all(r.verdict == triggers.SAFE for r in reports) else triggers.REVIEW
    notes['trigger_verdict'] = {'verdict': verdict, 'reasons': reasons,
                                'refs': list(dict.fromkeys(refs)), 'prepared_tree': before[1]}
    notes['verdict'] = triggers.verdict_line(triggers.TriggerReport(verdict, reasons))
    if verdict != triggers.SAFE:
        reason = reasons[0] if reasons else 'unverified automation remains'
        _refuse(f'GitHub automation inspection held all writes: {reason}. Nothing was pushed.')
    if before != _snapshot(git, worktree):
        _refuse('The prepared checkout changed during automation inspection. Nothing was pushed.')
    ensure_not_stopped(context)
    return before[1]


def require_remote_refs(git, worktree: str, expected: dict[str, str]) -> None:
    """A moved source/target invalidates the reviewed branch-sync plan."""
    output = _git(git, ['ls-remote', 'origin', *expected], worktree,
                  'current remote branches')
    actual = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2:
            actual[parts[1]] = parts[0]
    if any(actual.get(ref) != sha for ref, sha in expected.items()):
        _refuse('A source or target branch moved since this plan was prepared. '
                'Nothing was pushed; run the task again to review the current commits.')
