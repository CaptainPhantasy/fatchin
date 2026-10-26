"""Discover familiar project checks and run them away from account credentials.

Saved commands win. Automatic commands come from recognized manifests, never
from review comments or model output. Every command actually runs before it is
reported as passing. Linux bubblewrap is required for autonomous repository
code; there is deliberately no unconfined fallback.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from .interfaces import RepoConfig


def _read(root, name):
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256_000:
        return ''
    if not path.resolve().is_relative_to(root.resolve()):
        return ''
    return path.read_text(encoding='utf-8', errors='replace')


def discover_checks(worktree):
    root = Path(worktree)
    package = _read(root, 'package.json')
    if package:
        try:
            scripts = json.loads(package).get('scripts', {})
        except (ValueError, AttributeError):
            scripts = {}
        checks = []
        # The default npm-init placeholder is a failure message, not a test.
        if scripts.get('test') and 'no test specified' not in str(scripts['test']).lower():
            checks.append('npm test -- --runInBand' if re.match(r'^jest(?:\s|$)', str(scripts['test'])) else 'npm test')
        if scripts.get('typecheck'):
            checks.append('npm run typecheck')
        elif not checks and scripts.get('build'):
            checks.append('npm run build')
        if checks:
            return checks
    if _read(root, 'Cargo.toml'):
        return ['cargo test --locked'] if _read(root, 'Cargo.lock') else ['cargo test']
    if _read(root, 'go.mod'):
        return ['go test ./...']
    tests = root / 'tests'
    if tests.is_dir() and not tests.is_symlink():
        files = [p for p in tests.rglob('test*.py') if p.is_file() and not p.is_symlink()][:200]
        if files:
            modules = set()
            for file in files:
                try:
                    tree = ast.parse(_read(root, str(file.relative_to(root))))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules.update(a.name.split('.')[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        modules.add(node.module.split('.')[0])
            uses_pytest = 'pytest' in modules
            if uses_pytest or _read(root, 'pytest.ini'):
                return ['python3 -m pytest -q']
            uses_unittest = 'unittest' in modules
            if uses_unittest:
                return ['python3 -m unittest discover -s tests -t .' if (tests / '__init__.py').is_file()
                        else 'python3 -m unittest discover -s tests']
    return []


def auto_repository_policy(cfg, gh, repo, ref, worktree):
    existing = next((r for r in cfg.repos if r.full_name.casefold() == repo.casefold()), None)
    if existing and existing.checks:
        return cfg
    commands = discover_checks(worktree)
    if not commands:
        raise ValueError('This project has no recognized test or build command. Use Set verification command on this item; the fleet will continue automatically after it is saved.')
    owner, name = repo.split('/')
    policy = dataclasses.replace(existing, checks=commands) if existing else RepoConfig(owner, name, base_branch=ref, checks=commands)
    return dataclasses.replace(cfg, repos=[r for r in cfg.repos if r is not existing] + [policy])


def _git_mounts(root: Path, allowed_root) -> list[str]:
    """Read-only Git metadata, without exposing sibling task checkouts.

    A task checkout is a linked worktree: `.git` is a file naming the real
    git directory inside the shared bare cache. Binding no metadata breaks
    every git command; binding the cache exposes other tasks' checkouts.
    Bind the common directory read-only, mask the other worktree entries,
    and bind this task's own metadata back.
    """
    marker = root / '.git'
    if marker.is_symlink():
        raise RuntimeError('The checkout Git marker is a symbolic link; '
                           'the command was not run.')
    if marker.is_dir():
        # Tests may write build artifacts, but must not rewrite Git
        # configuration, hooks or objects the publisher uses afterwards.
        return ['--ro-bind', str(marker), str(marker)]
    if not marker.is_file():
        return []
    text = marker.read_text(encoding='utf-8', errors='replace').strip()
    if not text.lower().startswith('gitdir:'):
        raise RuntimeError('The checkout Git marker is not understood; '
                           'the command was not run.')
    pointer = text.split(':', 1)[1].strip()
    if not pointer:
        raise RuntimeError('The checkout Git marker is empty; '
                           'the command was not run.')
    gitdir = Path(pointer)
    gitdir = (root / gitdir if not gitdir.is_absolute() else gitdir).resolve()
    allowed = Path(allowed_root).resolve() if allowed_root else root.parent.resolve()
    if not gitdir.is_relative_to(allowed) or not gitdir.is_dir():
        raise RuntimeError('The checkout Git directory is outside the task '
                           'workspace; the command was not run.')
    common = gitdir
    common_pointer = gitdir / 'commondir'
    if common_pointer.is_symlink():
        raise RuntimeError('The checkout Git common directory is a symbolic '
                           'link; the command was not run.')
    if common_pointer.is_file():
        target = Path(common_pointer.read_text(encoding='utf-8', errors='replace').strip())
        common = (gitdir / target if not target.is_absolute() else target).resolve()
    if not common.is_relative_to(allowed) or not common.is_dir():
        raise RuntimeError('The checkout Git common directory is outside the '
                           'task workspace; the command was not run.')
    parts = ['--ro-bind', str(common), str(common)]
    worktrees = common / 'worktrees'
    if worktrees.is_dir() and gitdir.is_relative_to(worktrees):
        # The mask lives only in the child's mount namespace, so other
        # tasks' metadata is invisible and the host cache is untouched.
        parts += ['--tmpfs', str(worktrees), '--ro-bind', str(gitdir), str(gitdir)]
    parts += ['--ro-bind', str(marker), str(marker)]
    return parts


def _sandbox(root, argv, *, network=False, allowed_root=None):
    executable = shutil.which('bwrap')
    if not executable:
        raise RuntimeError('The isolated check runner is unavailable. The tool maintainer must restore bubblewrap; repository code was not run outside it.')
    root = Path(root).resolve()
    cmd = [executable, '--die-with-parent', '--unshare-all']
    if network:
        cmd.append('--share-net')
    real_python = Path(sys.executable).resolve()
    prefixes = ['/usr', '/bin', '/lib', '/lib64', '/usr/local',
                str(Path(sys.prefix)), str(Path(sys.base_prefix)),
                str(real_python.parent)]
    if len(real_python.parents) > 2:
        # A versioned installation (Homebrew Cellar, pyenv) keeps its runtime
        # alongside the executable; mount that version directory, not the home.
        prefixes.append(str(real_python.parents[2]))
    if len(real_python.parents) > 4:
        # Homebrew's ELF loader is requested by absolute path from the
        # installation root (for example <brew>/lib/ld.so), which sits above
        # the versioned prefix. Mount that root read-only, never the home.
        install_root = real_python.parents[4]
        if install_root != Path('/') and len(install_root.parts) >= 3:
            prefixes.append(str(install_root))
    mounts, seen = [], set()
    for location in prefixes:
        if location in seen or location == '/' or not Path(location).exists():
            continue
        seen.add(location)
        cmd += ['--ro-bind', location, location]
        mounts.append(location)
    rust_bins = []
    # rustup's compiler directories contain runtimes, not the account's
    # cargo credentials/settings. Expose only installed toolchains.
    for toolchains in (Path.home() / '.rustup' / 'toolchains', Path('/opt/rust/toolchains')):
        if toolchains.is_dir():
            cmd += ['--ro-bind', str(toolchains), str(toolchains)]
            rust_bins += [str(p / 'bin') for p in sorted(toolchains.iterdir())
                          if p.is_dir() and (p / 'bin' / 'cargo').is_file()]
    for location in ('/etc/ssl/certs', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/alternatives'):
        if Path(location).exists():
            cmd += ['--ro-bind', location, location]
    cmd += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/tmp/home',
            '--bind', str(root), str(root)]
    cmd += _git_mounts(root, allowed_root)
    cmd += ['--chdir', str(root), '--clearenv',
            '--setenv', 'PATH', ':'.join(dict.fromkeys(
                rust_bins + [str(Path(sys.executable).parent),
                             str(Path(sys.prefix) / 'bin'),
                             '/usr/local/bin', '/usr/bin', '/bin'])),
            '--setenv', 'HOME', '/tmp/home', '--setenv', 'CI', 'true',
            '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
            '--setenv', 'PYTHONPATH', str(root / 'src') + ':' + str(root / '.fatchin-dependencies'),
            '--setenv', 'CARGO_HOME', str(root / '.fatchin-cargo'),
            '--setenv', 'GOPATH', str(root / '.fatchin-go'),
            '--setenv', 'GOCACHE', str(root / '.fatchin-go' / 'build'),
            '--setenv', 'GIT_CONFIG_NOSYSTEM', '1', '--setenv', 'GIT_CONFIG_GLOBAL', '/dev/null',
            '--setenv', 'GIT_TERMINAL_PROMPT', '0', '--setenv', 'PIP_DISABLE_PIP_VERSION_CHECK', '1',
            '--', *argv]
    return cmd


def _execute(cfg, root, argv, should_stop, *, network=False):
    # Logs are temporary regular files, so an excessively chatty command does
    # not accumulate unlimited Python memory. Return only a bounded tail.
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(_sandbox(root, argv, network=network,
                                             allowed_root=cfg.worktree_root),
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
        deadline = time.monotonic() + cfg.run_command_timeout_seconds
        reason = ''
        try:
            while process.poll() is None:
                if should_stop() or time.monotonic() >= deadline:
                    reason = 'Stopped by the member.' if should_stop() else 'Verification command timed out.'
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(.05)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        size = log.seek(0, os.SEEK_END)
        log.seek(max(0, size - min(cfg.run_command_output_cap, 16_000)))
        output = log.read().decode('utf-8', errors='replace')
        return (124 if reason else process.returncode), (output + '\n' + reason).strip()


_MANIFESTS = {'package.json', 'package-lock.json', 'npm-shrinkwrap.json',
              'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum', 'go.work', 'go.work.sum',
              'pyproject.toml', 'setup.cfg'}
_SKIP_DIRS = {'.git', 'node_modules', 'target', 'build', 'dist', 'venv', '.venv',
              '__pycache__', '.pytest_cache', '.fatchin-dependencies', '.fatchin-cargo', '.fatchin-go'}
_INSTALL = re.compile(r'\b(?:npm\s+(?:ci|install)|pip(?:3)?\s+install|cargo\s+fetch|go\s+mod\s+download)\b')


def _safe_manifest(root, relative):
    """Return only a bounded regular file wholly inside this checkout."""
    path = Path(relative)
    if path.is_absolute():
        raise ValueError('A local dependency points outside this checkout.')
    candidate = root / path
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('A local dependency points outside this checkout.')
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError('Dependency manifests cannot follow symbolic links.')
        current = current.parent
    if not candidate.is_file() or candidate.stat().st_size > 256_000:
        raise ValueError('A referenced dependency manifest is missing or too large: ' + str(relative))
    return candidate.resolve()


def _copy_manifests(root, stage):
    """Copy local workspace metadata, never repository code or credentials.

    Walking manifests also handles Cargo path dependencies and Go local
    replacements. Requirements include/constraint paths are followed explicitly
    because a valid included file need not be named requirements*.txt.
    """
    manifests, requirements, visited = set(), [], set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in _SKIP_DIRS and not name.startswith('.')
                   and not (Path(directory) / name).is_symlink()]
        for name in files:
            if name in _MANIFESTS or (name.startswith('requirements') and name.endswith('.txt')):
                relative = (Path(directory) / name).relative_to(root)
                _safe_manifest(root, relative)
                manifests.add(relative)
                if len(manifests) > 500:
                    raise ValueError('There are too many dependency manifests for one task.')
                if name.startswith('requirements'):
                    requirements.append(relative)
    while requirements:
        relative = requirements.pop()
        if relative in visited:
            continue
        visited.add(relative)
        for line in _safe_manifest(root, relative).read_text().splitlines():
            args = shlex.split(line, comments=True)
            if not args:
                continue
            include = None
            if args[0] in {'-r', '--requirement', '-c', '--constraint'} and len(args) == 2:
                include = args[1]
            elif args[0].startswith(('--requirement=', '--constraint=')):
                include = args[0].split('=', 1)[1]
            elif args[0].startswith(('-r', '-c')) and len(args[0]) > 2:
                include = args[0][2:]
            if include:
                target = (_safe_manifest(root, relative.parent / include)).relative_to(root)
                manifests.add(target)
                requirements.append(target)
            if len(manifests) > 500:
                raise ValueError('There are too many nested dependency manifests.')
    for relative in sorted(manifests):
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_safe_manifest(root, relative).read_bytes())
    # cargo fetch validates workspace targets but does not execute their code.
    # Minimal empty targets are enough to download declared dependencies.
    for relative in manifests:
        if relative.name != 'Cargo.toml':
            continue
        manifest = tomllib.loads((stage / relative).read_text())
        if not isinstance(manifest.get('package'), dict):
            continue
        targets = ['src/lib.rs']
        if isinstance(manifest.get('lib'), dict) and manifest['lib'].get('path'):
            targets.append(manifest['lib']['path'])
        for kind in ('bin', 'test', 'bench', 'example'):
            for target in manifest.get(kind, []):
                if isinstance(target, dict) and target.get('path'):
                    targets.append(target['path'])
        for name in targets:
            target = stage / relative.parent / str(name)
            if not target.resolve().is_relative_to(stage.resolve()):
                raise ValueError('A Cargo target points outside this checkout.')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('// Manifest-only dependency preparation.\n')
    return manifests


def _split_check(command):
    """Move only recognized install prefixes into manifest-only preparation.

    Ordinary saved commands retain their argv and run with networking disabled.
    Shell commands with installs are deliberately a narrow supported grammar:
    one familiar install, &&, then one or more plain verification commands.
    """
    args = shlex.split(command)
    if not args:
        raise ValueError('The verification command is empty.')
    if not _INSTALL.search(command):
        return [args]
    text = command
    if Path(args[0]).name in {'bash', 'sh'}:
        if len(args) != 3 or args[1] not in {'-c', '-lc'}:
            raise ValueError('Use a simple install followed by && and a test command; this compound install is unsupported.')
        text = args[2]
    lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|<>')
    lexer.whitespace_split = True
    tokens = list(lexer)
    groups, group = [], []
    for token in tokens:
        if token == '&&':
            if not group:
                raise ValueError('A compound verification command has an empty step.')
            groups.append(group)
            group = []
        elif token in {';', '|', '||', '&', '<', '>', '>>', '<<'} or '$' in token or '`' in token:
            raise ValueError('This install command needs shell expansion or redirection. Use separate dependency and verification steps.')
        else:
            group.append(token)
    if group:
        groups.append(group)
    if len(groups) < 2:
        raise ValueError('Installing dependencies is not a verification check. Add && followed by the project’s test or build command.')
    install = groups.pop(0)
    known = False
    if len(install) >= 2 and install[:2] in (['npm', 'ci'], ['npm', 'install']):
        flags = {'--ignore-scripts', '--no-audit', '--no-fund', '--include=dev'}
        known = all(arg in flags for arg in install[2:])
    elif install in (['cargo', 'fetch'], ['cargo', 'fetch', '--locked'], ['go', 'mod', 'download']):
        known = True
    else:
        prefix = 3 if install[:3] in (['python3', '-m', 'pip'], ['python', '-m', 'pip']) else 1 if install[0] in {'pip', 'pip3'} else 0
        if prefix and install[prefix:prefix + 1] == ['install']:
            options = install[prefix + 1:]
            known = (len(options) == 2 and options[0] in {'-r', '--requirement'}
                     and options[1] in {'requirements.txt', 'requirements-test.txt'})
    if not known or any(_INSTALL.search(shlex.join(group)) for group in groups):
        raise ValueError('This compound install is unsupported. Use the project’s test command directly; declared dependencies are prepared automatically.')
    return groups


def _exclude_path(root: Path) -> Path | None:
    """The repository's real info/exclude, resolved for linked worktrees.

    A task checkout is a linked worktree, so `<root>/.git` is a file and
    `<root>/.git/info/exclude` does not exist. Git still reports the common
    directory's exclude file, which is the one `ls-files --others
    --exclude-standard` reads.
    """
    try:
        found = subprocess.run(['git', 'rev-parse', '--git-path', 'info/exclude'],
                               cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    name = found.stdout.strip()
    if found.returncode or not name:
        return None
    path = Path(name)
    return path if path.is_absolute() else root / path


def _prepare(cfg, root, checks, should_stop):
    """Install declared dependencies in disposable manifest-only task space."""
    root = Path(root)
    exclude = _exclude_path(root)
    marker = '# FatChin disposable verification artifacts'
    if exclude is not None and not exclude.is_symlink():
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = (exclude.read_text(encoding='utf-8', errors='replace')
                        if exclude.is_file() else '')
            if marker not in existing:
                with exclude.open('a', encoding='utf-8') as handle:
                    handle.write('\n' + marker + '\n/node_modules/\n/.fatchin-dependencies/\n'
                                 '/.fatchin-cargo/\n/.fatchin-go/\n__pycache__/\n'
                                 '/.pytest_cache/\n/target/\n')
        except OSError:
            # Result export also skips these paths, so a read-only exclude
            # file cannot turn a successful check into a failed task.
            pass
    commands = []
    if any(re.search(r'\bnpm\b', c) for c in checks) and _read(root, 'package.json'):
        if not (root / 'node_modules').exists():
            commands.append(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund']
                            if (root / 'package-lock.json').is_file() else
                            ['npm', 'install', '--ignore-scripts', '--no-audit', '--no-fund', '--package-lock=false'])
    python_checks = any(re.search(r'\b(?:python(?:3(?:\.\d+)?)?|pytest)\b', c) for c in checks)
    requirements_present = (root / 'requirements-test.txt').is_file() or (root / 'requirements.txt').is_file()
    if python_checks and (requirements_present or any('pytest' in shlex.split(c) for c in checks)):
        requirements = 'requirements-test.txt' if (root / 'requirements-test.txt').is_file() else 'requirements.txt'
        command = ['python3', '-m', 'pip', 'install', '--target', str(root / '.fatchin-dependencies'), '--no-input']
        commands.append(command + (['-r', requirements] if requirements_present else ['pytest']))
    if any(shlex.split(c)[0] == 'cargo' for c in checks if shlex.split(c)):
        commands.append(['cargo', 'fetch', '--locked'] if _read(root, 'Cargo.lock') else ['cargo', 'fetch'])
    if any(shlex.split(c)[0] == 'go' for c in checks if shlex.split(c)):
        commands.append(['go', 'mod', 'download'])
    for command in commands:
        if should_stop():
            return 'Dependency preparation was stopped.'
        with tempfile.TemporaryDirectory(prefix='fatchin-deps-', dir=root.parent) as temp:
            stage = Path(temp)
            _copy_manifests(root, stage)
            safe_command = [str(stage / '.fatchin-dependencies') if a == str(root / '.fatchin-dependencies') else a for a in command]
            code, output = _execute(cfg, stage, safe_command, should_stop, network=True)
            if code:
                return 'Dependency preparation failed: ' + output[-3000:]
            for name in ('node_modules', '.fatchin-dependencies', '.fatchin-cargo', '.fatchin-go'):
                if (stage / name).exists() and not (root / name).exists():
                    shutil.move(str(stage / name), str(root / name))
            # These are temporary verification artifacts when absent from the
            # candidate. run_repository_checks removes only its newly created
            # copies afterwards; pre-existing member files are never removed.
            for name in ('Cargo.lock', 'go.sum', 'go.work.sum'):
                if (stage / name).is_file() and not (root / name).exists():
                    shutil.copy2(stage / name, root / name)
    return ''


def run_repository_checks(cfg, root, checks, should_stop=lambda: False):
    from .learning import _redact
    root = Path(root)
    ephemeral = [root / name for name in ('Cargo.lock', 'go.sum', 'go.work.sum')
                 if not (root / name).exists() and not (root / name).is_symlink()]
    results = []
    try:
        try:
            plans = [_split_check(command) for command in checks]
            effective = [shlex.join(argv) for plan in plans for argv in plan]
            problem = _prepare(cfg, root, effective, should_stop)
        except ValueError as exc:
            problem = str(exc)
        except Exception as exc:
            problem = 'Dependency preparation could not finish (' + type(exc).__name__ + ').'
        if problem:
            return [{'command': str(checks[0]) if checks else 'Repository verification', 'passed': False,
                     'exit': 1, 'output_tail': _redact(problem), 'stage': 'dependencies'}]
        for command, plan in zip(checks, plans):
            if should_stop():
                break
            outputs, code = [], 0
            for argv in plan:
                if should_stop():
                    code, output = 124, 'Verification was stopped.'
                else:
                    try:
                        code, output = _execute(cfg, root, argv, should_stop, network=False)
                    except Exception as exc:
                        code, output = 1, 'The isolated verification command could not run (' + type(exc).__name__ + ').'
                outputs.append(output)
                if code:
                    break
            results.append({'command': command, 'passed': code == 0, 'exit': code,
                            'output_tail': _redact('\n'.join(outputs)[-4000:]), 'isolated': True})
            if code:
                break
        return results
    finally:
        for path in ephemeral:
            if path.is_file() or path.is_symlink():
                path.unlink()  # This task's disposable, previously absent lock artifact.
