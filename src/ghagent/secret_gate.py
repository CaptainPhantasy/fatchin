"""Pre-commit gate for credentials and private infrastructure.

Every path that writes a commit calls :func:`check_commit` first. The gate
exists so a token or a private address cannot reach a pull request unnoticed,
but it is built to allow far more than it blocks. Design rules, in priority
order:

1. Few false positives. Missing a marginal case is cheaper than blocking a
   legitimate commit, so each detector demands a shape a real credential has
   and a placeholder does not: a length floor, a required context word, or a
   structural body check. Placeholder values, environment lookups, dotted
   expressions and the app's own ``fatchin-fleet:v1:`` markers never fire.
   Test and fixture trees are exempt from the low-confidence kinds only: a
   fixture password cannot be told from a leak, but a real key shape still
   reports everywhere. Connection strings aimed at reserved documentation
   hosts do not report.
2. Bounded. Files above ``MAX_FILE_BYTES``, binary files, vendored
   dependencies, ``.git``, lockfiles and a fixed extension list are never read.
3. Complete scans. Missing files, unreadable inputs, invalid allowlists,
   exhausted scan limits and scanner failures hold the commit. Repair the
   reported setup problem, then retry; raw exception text is never printed.
4. Overridable. A known-safe hit is acknowledged once, deliberately, in
   ``[secret_gate] allow = [...]``, in ``$FATCHIN_SECRET_ALLOW_FILE``, or in a
   ``.fatchin-secret-allow`` file at the repository root.
5. Actionable. A refusal names file, line and kind - never the value.

A finding carries a path, a line number, a kind and a redacted excerpt. The
scanner never returns or logs a credential value. Text it emits passes through
:func:`ghagent.github_client.redact_text`, so there is one redaction vocabulary
in the application, not two.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .github_client import redact_text

logger = logging.getLogger(__name__)

# Bytes above which a file is not read. A commit gate must stay cheap.
MAX_FILE_BYTES = 1_048_576
_SNIFF_BYTES = 8192
MAX_REPORTED = 10
MAX_FILES = 2000

# Directories never descended into.
_SKIP_DIRS = frozenset({
    '.git', '.hg', '.svn', 'node_modules', 'bower_components',
    '__pycache__', '.venv', 'venv', '.tox', '.eggs', 'site-packages',
    '.mypy_cache', '.pytest_cache', '.ruff_cache', '.idea', '.next',
})

# File names whose content is generated, not written by hand.
_SKIP_NAMES = frozenset({
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'npm-shrinkwrap.json',
    'Cargo.lock', 'poetry.lock', 'Pipfile.lock', 'composer.lock', 'go.sum',
    'uv.lock', 'Gemfile.lock', 'bun.lockb', 'flake.lock',
})

# Extensions that are binary or minified; scanning them is noise.
_SKIP_SUFFIXES = (
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tif', '.tiff',
    '.pdf', '.zip', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar', '.jar',
    '.war', '.class', '.so', '.dylib', '.dll', '.exe', '.bin', '.o', '.a',
    '.woff', '.woff2', '.ttf', '.otf', '.eot', '.mp3', '.mp4', '.mov', '.avi',
    '.wav', '.heic', '.psd', '.pyc', '.pyo', '.whl', '.sqlite', '.sqlite3',
    '.db', '.der', '.p12', '.pfx', '.keystore', '.jks', '.npy', '.npz',
    '.parquet', '.onnx', '.safetensors', '.pkl', '.pt', '.blend', '.sketch',
    '.min.js', '.min.css', '.js.map', '.css.map',
)

# The fleet stamps its own markers into commit bodies and PR text. They are
# never credentials, and pretending otherwise blocks the fleet's own writes.
_FLEET_MARKER = re.compile(r'fatchin-fleet:v1:[A-Za-z0-9._/-]*')

# Values that mean "not a real credential yet".
_PLACEHOLDER = re.compile(
    r'''(?ix)
    ^\s*$
  | your[\s_-]*(?:token|key|secret|password|api|pat)
  | example
  | sample
  | placeholder
  | change[\s_-]?me
  | replace[\s_-]?me
  | dummy
  | fake
  | redacted
  | x{4,}
  | \*{3,}
  | ^\.{3}$
  | ^<[^>]*>$
  | \$\{
  | %\(
  | process\.env
  | os\.environ
  | \bgetenv\b
  | ^(?:null|none|nil|true|false|todo|tbd|secret|password|token|test)$
    '''
)

# Characters that mark a value as an expression, a path or a glob rather than
# a literal credential.
_EXPRESSION_CHARS = '()[]{}<>.$%*\\'


class SecretGateRefusal(RuntimeError):
    """A commit was held because a secret was found or its scan was incomplete.

    Subclasses ``RuntimeError`` so existing callers that already treat worktree
    failures as runtime errors keep working unchanged.
    """


@dataclass(frozen=True)
class Finding:
    """One possible secret. ``excerpt`` is redacted or empty, never the value."""

    path: str
    line: int
    kind: str
    excerpt: str = ''
    count: int = 1

    def allow_key(self) -> str:
        return f'{self.path}:{self.kind}'


@dataclass
class Findings:
    """The result of a scan: what was found and what could not be read."""

    items: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.items and not self.errors

    def kinds(self) -> list[str]:
        return sorted({item.kind for item in self.items})

    def refusal(self) -> str:
        """The message a blocked commit shows. Names file, line and kind."""
        if self.errors:
            notes = '\n'.join('  ' + redact_text(error) for error in self.errors[:MAX_REPORTED])
            return ('FatChin held this commit: the secret scan could not finish.\n'
                    + notes + '\nRestore missing files, correct file access or the allowlist, '
                    'and retry the scan. No file contents or error details were printed. '
                    'Nothing was committed.')
        total = sum(item.count for item in self.items)
        lines = [
            f'FatChin held this commit: {total} possible secret'
            f'{"" if total == 1 else "s"} found.',
            '',
        ]
        shown = self.items[:MAX_REPORTED]
        width = max((len(item.path) for item in shown), default=0)
        for item in shown:
            extra = f'  (+{item.count - 1} more)' if item.count > 1 else ''
            lines.append(f'  {item.path.ljust(width)}:{item.line}  {item.kind}{extra}')
        if len(self.items) > len(shown):
            lines.append(f'  ...and {len(self.items) - len(shown)} more file/kind pairs')
        lines += [
            '',
            'The values themselves are never printed.',
            'If a hit is known-safe, acknowledge it once in the config:',
            '',
            '  [secret_gate]',
            f'  allow = ["{self.items[0].allow_key()}"]',
            '',
            'or add one "path:kind" line to .fatchin-secret-allow in the repository root.',
            'Nothing was committed.',
        ]
        return redact_text('\n'.join(lines))


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern


_RULES: tuple[_Rule, ...] = (
    _Rule('private-key', re.compile(r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----')),
    _Rule('github-token', re.compile(
        r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b')),
    _Rule('slack-token', re.compile(r'\bxox[baprse]-[A-Za-z0-9-]{10,}\b')),
    _Rule('aws-access-key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    _Rule('openai-key', re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')),
    _Rule('google-api-key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    _Rule('jwt', re.compile(
        r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')),
    _Rule('connection-string', re.compile(
        r'\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?'
        r'|amqps?|mssql|sqlserver|clickhouse)://[^\s:/@]*:[^\s@/]+@([A-Za-z0-9._-]+)')),
)

# A PEM header alone is not a key: fixtures use the header as a stub. A real
# key has at least one long base64 body line close behind the header.
_PEM_BODY = re.compile(r'^[A-Za-z0-9+/=]{40,}$')
_PEM_LOOKAHEAD = 4

# Test and fixture trees hold deliberately fake credentials. The strong kinds
# (a real key or token shape) still report there; the low-confidence kinds do
# not, because a fixture password is otherwise indistinguishable from a leak.
_TEST_PATH = re.compile(
    r'''(?ix)
    (?:^|/)(?:tests?|specs?|__tests__|testdata|test_data|fixtures?)(?:/|$)
    | (?:^|/)test_[^/]*\.py$
    | (?:^|/)[^/]*_test\.py$
    | (?:^|/)conftest\.py$
    '''
)

# Hosts reserved for documentation and tests carry no real credential.
_DOC_HOSTS = ('.example.com', '.example.net', '.example.org', '.test', '.invalid')
_DOC_BARE = ('example.com', 'example.net', 'example.org')

_ASSIGNMENT = re.compile(
    r'''(?i)\b(password|passwd|pwd|passphrase|api[_-]?key|apikey|access[_-]?key
        |secret[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token
        |refresh[_-]?token|private[_-]?key|token|secret)
        \b\s*[:=]\s*["']?([^\s"'\#,;)]{8,})''',
    re.X,
)

_RFC1918 = re.compile(
    r'\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}'
    r'|192\.168\.\d{1,3}\.\d{1,3})\b'
)

# A private address is only interesting when something names it as a host.
# Matched against the text *before* the address, so it must end at the address.
_NETWORK_CONTEXT = re.compile(
    r'''(?ix)
    (?:^|[^A-Za-z0-9])
    [A-Za-z0-9_]*(?:host|hostname|host_name|server|endpoint|address|addr|uri
        |url|ip|upstream|proxy|target|remote|broker|bind|listen|dns|nameserver)\b
    \s*[:=]\s*["']?\s*$
    '''
)
_PORT_SUFFIX = re.compile(r'^:\d{2,5}\b')

_INTERNAL_HOST = re.compile(
    r'\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+internal\b')


# ---------------------------------------------------------------------------
# Value screening
# ---------------------------------------------------------------------------

def is_placeholder(value: str) -> bool:
    """Whether a value is a documented stand-in rather than a credential."""
    return bool(_PLACEHOLDER.search((value or '').strip()))


def _credential_value(value: str) -> bool:
    """A literal-looking credential value, as opposed to code or a placeholder."""
    v = (value or '').strip().strip('\'"')
    if len(v) < 8 or is_placeholder(v):
        return False
    if any(ch in v for ch in _EXPRESSION_CHARS):
        return False
    if v.startswith(('/','~','-')):
        return False
    if re.fullmatch(r'[A-Z][A-Z0-9_]{2,}', v):    # GITHUB_TOKEN, API_KEY
        return False
    if re.fullmatch(r'[a-z_]+', v):               # bare word
        return False
    if re.fullmatch(r'[a-z_]+(?:\.[a-z_0-9]+)+', v):
        return False
    return True


def _plausible_token(matched: str) -> bool:
    """Reject token-shaped strings that are obviously fabricated."""
    if is_placeholder(matched):
        return False
    if len(set(matched)) < 8:                     # ghp_xxxx..., AKIA0000000000000000
        return False
    return True


def is_test_path(path: str) -> bool:
    """Whether a path lives in a test or fixture tree."""
    return bool(_TEST_PATH.search((path or '').replace('\\', '/')))


def _documentation_host(host: str) -> bool:
    """A connection aimed at a reserved documentation host."""
    host = (host or '').lower()
    return host in _DOC_BARE or host.endswith(_DOC_HOSTS)


def _private_key_at(lines: list[str], index: int) -> bool:
    """True when a PEM header at ``index`` is followed by a real key body."""
    for offset in range(1, _PEM_LOOKAHEAD + 1):
        if index + offset >= len(lines):
            return False
        candidate = lines[index + offset].strip()
        if not candidate:
            continue
        return bool(_PEM_BODY.match(candidate))
    return False


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _record(found: Findings, path: str, line: int, kind: str) -> None:
    for index, existing in enumerate(found.items):
        if existing.path == path and existing.kind == kind:
            found.items[index] = Finding(
                existing.path, existing.line, existing.kind,
                existing.excerpt, existing.count + 1)
            return
    found.items.append(Finding(path, line, kind))


def _network_context(line: str, start: int, end: int) -> bool:
    """Whether a private address at ``start:end`` is used as a host."""
    if '://' in line:
        return True
    if _PORT_SUFFIX.match(line[end:]):
        return True
    return bool(_NETWORK_CONTEXT.search(line[:start]))


def scan_text(text: str, path: str = '', allow=()) -> Findings:
    """Scan one text blob. ``path`` is only used to label findings."""
    found = Findings()
    lines = (text or '').splitlines()
    weak = not is_test_path(path)
    for number, raw in enumerate(lines, start=1):
        line = _FLEET_MARKER.sub(' ', raw)
        specific = False
        for rule in _RULES:
            match = rule.pattern.search(line)
            if not match:
                continue
            if rule.kind == 'private-key':
                if not _private_key_at(lines, number - 1):
                    continue
            elif rule.kind == 'connection-string':
                if _documentation_host(match.group(1)):
                    continue
            elif not _plausible_token(match.group(0)):
                continue
            _record(found, path, number, rule.kind)
            specific = True
        if not weak:
            continue
        # A named token kind is the better answer for this line; the generic
        # assignment check would only repeat it and force two allowlist entries.
        assignment = None if specific else _ASSIGNMENT.search(line)
        if assignment and _credential_value(assignment.group(2)):
            _record(found, path, number, 'credential-assignment')
        private_ip = _RFC1918.search(line)
        if private_ip and _network_context(line, private_ip.start(), private_ip.end()):
            _record(found, path, number, 'private-ip')
        host = _INTERNAL_HOST.search(line)
        if host and not is_placeholder(host.group(0)):
            _record(found, path, number, 'internal-hostname')
    return filter_allowed(found, allow, keep_errors=True)


def scan_diff(text: str, allow=()) -> Findings:
    """Scan the added lines of a unified diff, keeping real line numbers."""
    found = Findings()
    path = ''
    new_line = 0
    pending: list[tuple[str, int, str]] = []
    for raw in (text or '').splitlines():
        if raw.startswith('+++ '):
            path = raw[4:].strip()
            if path == '/dev/null':
                path = ''
            elif path.startswith('b/'):
                path = path[2:]
            new_line = 0
            continue
        if raw.startswith('@@'):
            hunk = re.search(r'\+(\d+)', raw)
            new_line = int(hunk.group(1)) if hunk else 0
            continue
        if raw.startswith('+') and not raw.startswith('+++'):
            if path:
                pending.append((path, new_line, raw[1:]))
            new_line += 1
            continue
        if raw.startswith('-') and not raw.startswith('---'):
            continue
        new_line += 1
    # Group by file so the PEM look-ahead sees neighbouring added lines.
    by_path: dict[str, list[tuple[int, str]]] = {}
    for item_path, number, line in pending:
        by_path.setdefault(item_path, []).append((number, line))
    for item_path, entries in by_path.items():
        body = '\n'.join(line for _, line in entries)
        inner = scan_text(body, item_path, allow=())
        for item in inner.items:
            start = max(0, item.line - 1)
            source_line = entries[start][0] if start < len(entries) else 0
            _record(found, item_path, source_line, item.kind)
    return filter_allowed(found, allow, keep_errors=True)


def _path_skip_reason(relative: str) -> str | None:
    name = Path(relative).name
    if name in _SKIP_NAMES:
        return 'lockfile'
    if name.lower().endswith(_SKIP_SUFFIXES):
        return 'binary or minified'
    if any(part in _SKIP_DIRS for part in Path(relative).parts[:-1]):
        return 'vendored or cache directory'
    return None


def _skip_reason(target: Path, relative: str) -> str | None:
    reason = _path_skip_reason(relative)
    if reason:
        return reason
    try:
        if target.stat().st_size > MAX_FILE_BYTES:
            return f'larger than {MAX_FILE_BYTES} bytes'
    except OSError:
        return 'unreadable'
    return None


def _read_text(target: Path) -> str | None:
    """Read a file as text, or None if it is binary."""
    with open(target, 'rb') as handle:
        head = handle.read(_SNIFF_BYTES)
        if b'\x00' in head:
            return None
        rest = handle.read(MAX_FILE_BYTES - len(head))
    blob = head + rest
    try:
        return blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob.decode('utf-8', errors='replace')


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _raise_walk_error(error):
    raise OSError('directory scan could not finish') from None


def _iter_files(paths, root: Path):
    for raw in paths:
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_dir():
            for base, dirs, files in os.walk(candidate, onerror=lambda error: _raise_walk_error(error)):
                here = Path(base)
                # A directory holding a .git *file* is a submodule: never descend.
                dirs[:] = sorted(d for d in dirs
                                 if d not in _SKIP_DIRS
                                 and not (here / d / '.git').is_file())
                for name in sorted(files):
                    yield Path(base) / name
        else:
            yield candidate


def scan_paths(paths, root='.', allow=()) -> Findings:
    """Scan the given files (or directories) under ``root``.

    Unreadable entries make the result incomplete. Intentional binary, vendor
    and size exclusions remain bounded; other files are still checked.
    """
    root_path = Path(root)
    found = Findings()
    seen = 0
    for target in _iter_files(paths, root_path):
        if seen >= MAX_FILES:
            note = f'The scan limit of {MAX_FILES} files was reached; split the change and retry.'
            found.errors.append(note)
            logger.warning('secret gate %s', note)
            break
        seen += 1
        relative = _display(target, root_path)
        reason = _skip_reason(target, relative)
        if reason is not None:
            if reason == 'unreadable':
                found.errors.append(f'{relative}: file is missing or unreadable.')
            continue
        try:
            text = _read_text(target)
        except OSError:
            found.errors.append(f'{relative}: file could not be read.')
            continue
        if text is None:
            continue
        partial = scan_text(text, relative, allow=())
        found.items.extend(partial.items)
        found.errors.extend(partial.errors)
    return filter_allowed(found, allow, keep_errors=True)


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def _allow_entries(config=None, root='.', extra=()) -> list[str]:
    entries: list[str] = []
    gate = {}
    if config is not None:
        gate = getattr(config, 'secret_gate', None)
        if gate is None and isinstance(config, dict):
            gate = config
        if not isinstance(gate, dict):
            raise ValueError('secret_gate must be a table')
        listed = gate.get('allow', [])
        if not isinstance(listed, (list, tuple)):
            raise ValueError('allow must be a list')
        entries.extend(listed)
    env_path = os.environ.get('FATCHIN_SECRET_ALLOW_FILE')
    if env_path:
        entries.extend(_read_allow_file(Path(env_path), required=True))
    entries.extend(_read_allow_file(Path(root) / '.fatchin-secret-allow'))
    if not isinstance(extra, (list, tuple)):
        raise ValueError('extra allow entries must be a list')
    entries.extend(extra)
    for entry in entries:
        if (not isinstance(entry, str) or ':' not in entry
                or not all(part.strip() for part in entry.rsplit(':', 1))):
            raise ValueError('allow entries must use path:kind')
    return [entry.strip() for entry in entries]


def _read_allow_file(path: Path, *, required=False) -> list[str]:
    """Missing optional repository allowlists are fine; broken inputs hold work."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        if required:
            raise ValueError('the configured allowlist file is missing') from None
        return []
    if not path.is_file() or size > 65536:
        raise ValueError('the allowlist must be a regular file under 65536 bytes')
    lines = path.read_text(encoding='utf-8').splitlines()
    return [entry for line in lines if (entry := line.split('#', 1)[0].strip())]


def load_allow(config=None, root='.', extra=()) -> list[str]:
    """Load validated acknowledgments; malformed or unreadable inputs hold work."""
    try:
        return _allow_entries(config, root, extra)
    except Exception:
        raise SecretGateRefusal(
            'FatChin held this commit: the secret allowlist could not be read or is invalid. '
            'Check [secret_gate] allow entries use path:kind, and check access to '
            '.fatchin-secret-allow or FATCHIN_SECRET_ALLOW_FILE, then retry. '
            'No values were printed. Nothing was committed.') from None


def is_allowed(finding: Finding, allow) -> bool:
    """``path:kind``, with ``fnmatch`` globs allowed on the path."""
    for entry in allow or ():
        text = str(entry).strip()
        if ':' not in text:
            continue
        pattern_path, pattern_kind = text.rsplit(':', 1)
        if pattern_kind != '*' and pattern_kind != finding.kind:
            continue
        if fnmatch.fnmatch(finding.path, pattern_path):
            return True
    return False


def filter_allowed(found: Findings, allow, *, keep_errors: bool = True) -> Findings:
    if not allow:
        return found if keep_errors else Findings(found.items)
    kept = [item for item in found.items if not is_allowed(item, allow)]
    return Findings(kept, list(found.errors) if keep_errors else [])


# ---------------------------------------------------------------------------
# Commit integration
# ---------------------------------------------------------------------------

def git_result(run):
    """Adapt a callable returning a CompletedProcess to a ``(rc, out, err)`` tuple."""
    def adapter(argv):
        result = run(argv)
        return (result.returncode, result.stdout or '', result.stderr or '')
    return adapter


def _scan_failure(what: str) -> None:
    """Never expose raw scanner/git exceptions, which may contain file contents."""
    raise SecretGateRefusal(
        f'FatChin held this commit: {what}. Restore file access and a working Git '
        'checkout, then retry the secret scan. No error values were printed. '
        'Nothing was committed.') from None


def _index_output(run_git, argv):
    """Read Git metadata or blob contents; never replace a failed index read."""
    try:
        code, out, _ = run_git(argv)
    except Exception:
        _scan_failure('a staged file could not be read from Git')
    if code != 0 or not isinstance(out, (str, bytes)):
        _scan_failure('a staged file could not be read from Git')
    return out


def _scan_index(run_git, paths, allow=()) -> Findings:
    """Scan pinned stage-zero blobs, without reading their working-tree files.

    Path, binary, size and submodule exclusions match the explicit path scanner.
    Git adapters may return text or bytes; undecodable text stays fail-closed.
    """
    found = Findings()
    for index, path in enumerate(paths):
        if index >= MAX_FILES:
            found.errors.append(f'The scan limit of {MAX_FILES} files was reached; split the change and retry.')
            break
        if _path_skip_reason(path):
            continue
        # Literal pathspecs keep brackets, colons, tabs and newlines in a name
        # from being interpreted as patterns. NUL records preserve those names.
        record = _index_output(run_git, ['--literal-pathspecs', 'ls-files', '--stage', '-z', '--', path])
        if isinstance(record, bytes):
            record = record.decode('utf-8')
        records = [entry for entry in record.split('\x00') if entry]
        if len(records) != 1:
            _scan_failure('the staged file entry is missing or unmerged')
        metadata, separator, listed_path = records[0].partition('\t')
        fields = metadata.split()
        if (not separator or listed_path != path or len(fields) != 3 or fields[2] != '0'
                or not re.fullmatch(r'[0-9a-fA-F]{40,64}', fields[1])):
            _scan_failure('the staged file entry could not be verified')
        mode, oid, _ = fields
        if mode == '160000':
            continue  # Submodule contents are not part of this repository blob.
        size = int(_index_output(run_git, ['cat-file', '-s', oid]).strip())
        if size < 0:
            _scan_failure('the staged file size could not be verified')
        if size > MAX_FILE_BYTES:
            continue
        blob = _index_output(run_git, ['show', oid])
        if isinstance(blob, bytes):
            if b'\x00' in blob[:_SNIFF_BYTES]:
                continue
            text = blob.decode('utf-8', errors='replace')
        else:
            if '\x00' in blob[:_SNIFF_BYTES]:
                continue
            text = blob
        partial = scan_text(text, path)
        found.items.extend(partial.items)
        found.errors.extend(partial.errors)
    return filter_allowed(found, allow, keep_errors=True)


def check_commit(run_git, root='.', *, allow=(), config=None) -> Findings:
    """Scan exactly what is staged, then raise if anything looks like a secret.

    ``run_git`` takes an argv list and returns ``(returncode, stdout, stderr)``;
    wrap a real git runner with :func:`git_result`. Returns clean ``Findings``
    only when the staged set was scanned completely without findings. Missing
    inputs and scanner failures raise a redacted, actionable refusal.
    """
    entries = load_allow(config, root, allow)
    try:
        code, out, err = run_git(
            ['diff', '--cached', '--name-only', '-z', '--diff-filter=ACMR'])
    except Exception:
        _scan_failure('the staged file list is unavailable')
    if code != 0:
        _scan_failure('the staged file list is unavailable')
    if not isinstance(out, str):
        _scan_failure('the staged file list could not be read')
    paths = [item for item in out.split('\x00') if item]
    if not paths:
        return Findings()
    try:
        findings = _scan_index(run_git, paths, allow=entries)
    except SecretGateRefusal:
        raise
    except Exception:
        _scan_failure('the secret scan failed')
    if not findings.ok:
        raise SecretGateRefusal(findings.refusal())
    return findings


def check_paths(paths, root='.', *, allow=(), config=None) -> Findings:
    """Scan an explicit path list and raise on findings (same contract)."""
    entries = load_allow(config, root, allow)
    try:
        findings = scan_paths(paths, root=root, allow=entries)
    except Exception:
        _scan_failure('the secret scan failed')
    if not findings.ok:
        raise SecretGateRefusal(findings.refusal())
    return findings
