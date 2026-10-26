"""Export task evidence to the member's explicitly selected folder."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .github_client import redact_text


FILE_EXPORT_LIMIT_BYTES = 10_000_000
EXPORT_LIMIT_BYTES = 50_000_000
# Created by the worker while preparing or verifying a task; never task
# results, and never a reason to report that export was incomplete.
WORKER_ARTIFACT_DIRS = {'.fatchin-cargo', '.fatchin-dependencies', '.fatchin-go',
                        '.gha-drafts', 'node_modules'}


def _file_export_limit(job, relative):
    """Only the backup task's dated bundles get the larger file allowance."""
    if job.kind.value == 'backup-repo':
        from .tasks.backup_repo import BUNDLE_DIR, BUNDLE_PREFIX, BUNDLE_SUFFIX
        pattern = re.escape(BUNDLE_PREFIX) + r'[0-9]{8}' + re.escape(BUNDLE_SUFFIX)
        if relative.parent == Path(BUNDLE_DIR) and re.fullmatch(pattern, relative.name):
            return EXPORT_LIMIT_BYTES
    return FILE_EXPORT_LIMIT_BYTES


def grant_member_access(path, home):
    """Grant the destination's actual owner access to service-created output.

    A named POSIX access entry leaves other account permissions unchanged.
    The directory default is inherited by files and subdirectories.
    """
    uid = Path(home).stat().st_uid
    if uid != os.geteuid():
        result = subprocess.run(['setfacl', '-m', f'u:{uid}:rwx,d:u:{uid}:rwx', str(path)],
                                capture_output=True, timeout=5)
        if result.returncode:
            raise RuntimeError('The result filesystem could not grant its member access. Choose a folder on a filesystem supporting POSIX access entries.')


def result_root(job):
    output = job.notes.get('output_dir')
    if not output:
        return None  # Historical CLI jobs predate per-task result selection.
    parent = Path(output).resolve(strict=True)
    home = job.notes.get('member_home') or str(Path('/home') / str(job.notes.get('member', '')))
    home = Path(home).resolve(strict=True)
    if parent == home or not parent.is_relative_to(home):
        raise ValueError('The selected result folder must remain inside the member account space.')
    if not parent.is_dir() or not re.fullmatch(r'[A-Za-z0-9_-]+', job.job_id):
        raise ValueError('The selected result folder is unavailable.')
    root = parent / ('gha-worker-' + job.job_id)
    if root.is_symlink():
        raise ValueError('The task result folder must not be a symbolic link.')
    root.mkdir(mode=0o2775, exist_ok=True)
    grant_member_access(root, home)
    job.notes['artifact_dir'] = str(root)
    return root


def write_file(path, content):
    """Atomic updates only inside this task's unique result directory."""
    path.parent.mkdir(mode=0o2775, parents=True, exist_ok=True)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('Result files must not follow symbolic links.')
    temp = path.with_name('.' + path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o664)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def write_report(job):
    root = result_root(job)
    if root is not None:
        write_file(root / 'report.json', redact_text(job.to_json()).encode())
        write_file(root / 'report.md', redact_text(_report_markdown(job)).encode())


_STATE_RESULT = {
    'done': 'The work is complete.',
    'failed': 'The work stopped before finishing.',
    'stopped': 'The work was stopped.',
    'waiting-on-you': 'A draft is ready for your decision.',
}


def _report_markdown(job):
    """Plain-language report ending with the Three Lines."""
    notes = job.notes or {}
    state = getattr(job.state, 'value', str(job.state))
    lines = [f"# {notes.get('title') or job.kind.value}",
             '',
             f"Task {job.job_id} · {job.repo_full_name}"
             + (f" #{job.number}" if job.number else '') + f" · {job.kind.value}",
             '']
    summary = notes.get('agent_summary') or notes.get('summary')
    if summary:
        lines += [str(summary), '']
    verification = notes.get('verification')
    if verification:
        lines += [f"How it was checked: {verification}", '']
    commit = (job.produced_commits[0] if job.produced_commits else '') or notes.get('tested_base') or ''
    if commit:
        lines += [f"Commit: {commit}", '']
    if notes.get('error'):
        lines += [f"What went wrong: {notes['error']}", '']
    # The Three Lines
    lines += ['---', '',
              f"**What happened.** {_STATE_RESULT.get(state, 'The task is still running.')}", '']
    yours = {
        'waiting-on-you': 'Review the draft and choose Post, Edit, or Discard — nothing is posted until you do.',
        'done': 'Nothing needs you. If you want this on GitHub, that decision stays yours.',
        'failed': 'Check the reason above and retry when ready.',
    }.get(state, 'No action is needed right now.')
    lines += [f"**What only you can decide.** {yours}", '']
    running = 'The review watch stays armed: new feedback on this work is handled automatically.'
    lines += [f"**What keeps running on its own.** {running if state == 'done' else 'Nothing is running on its own for this task.'}", '']
    return '\n'.join(lines)


def export_worktree(job, worktree):
    root = result_root(job)
    if root is None:
        return
    source = Path(worktree).resolve(strict=True)
    base = job.started_commit or job.head_sha
    args = ['git', 'diff', '--binary', '--no-ext-diff']
    if base:
        if not re.fullmatch(r'[0-9a-fA-F]{7,64}', base):
            raise ValueError('The starting commit is invalid; a patch could not be exported.')
        args.append(base)
    result = subprocess.run(args + ['--'], cwd=source, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Git could not export the task patch.')
    if len(result.stdout) > EXPORT_LIMIT_BYTES:
        raise ValueError('The patch exceeds the 50 MB export limit.')
    write_file(root / 'changes.patch', result.stdout)
    # Recover from the actual checkout, including files a failed agent never
    # managed to declare. A patch alone does not contain untracked new files.
    tracked_args = ['git', 'diff', '--name-only', '-z'] + ([base] if base else []) + ['--']
    discovered = []
    untracked = set()
    for command in [tracked_args, ['git', 'ls-files', '--others', '--exclude-standard', '-z']]:
        found = subprocess.run(command, cwd=source, capture_output=True, timeout=30)
        if found.returncode:
            raise RuntimeError('Git could not inventory result files. Scratch was retained for recovery.')
        names = [os.fsdecode(name) for name in found.stdout.split(b'\0') if name]
        discovered.extend(names)
        if command[1] == 'ls-files':
            untracked.update(names)
    copied = []
    skipped = []
    total = 0
    incomplete = []
    names = list(dict.fromkeys([*job.notes.get('changed_files', []), *discovered]))
    for name in names:
        relative = Path(name)
        if any(part in WORKER_ARTIFACT_DIRS for part in relative.parts):
            skipped.append(name)
            continue
        if relative.is_absolute() or any(p in {'.', '..'} or p.startswith('.') for p in relative.parts):
            skipped.append(name)
            if name in untracked:
                incomplete.append(name)
            continue
        target = (source / relative).resolve()
        if not target.is_relative_to(source) or not target.is_file() or (source / relative).is_symlink():
            skipped.append(name)
            if name in untracked:
                incomplete.append(name)
            continue
        size = target.stat().st_size
        if size > _file_export_limit(job, relative) or total + size > EXPORT_LIMIT_BYTES:
            skipped.append(name)
            incomplete.append(name)
            continue
        content = target.read_bytes()
        total += len(content)
        write_file(root / 'files' / relative, content)
        copied.append(name)
    job.notes['exported_files'] = copied
    if skipped:
        job.notes['export_skipped_files'] = skipped
    write_report(job)
    if incomplete:
        raise RuntimeError('Some new or oversized result files could not be exported. Scratch was retained so those files remain recoverable.')
