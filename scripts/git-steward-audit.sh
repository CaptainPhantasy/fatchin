#!/bin/sh
# Portable, read-only pre-commit/push gate. Never prints matched values.
# Usage: scripts/git-steward-audit.sh [--private] [--skip-tests] [--build]
#          [--message 'chore(release): prepare release'] [commit-message]
set -eu
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" - "$@" <<'PY'
import argparse
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description='Audit candidate files without displaying secret values.')
parser.add_argument('--private', action='store_true', help='Permit existing deployment metadata as warnings; secrets still fail.')
parser.add_argument('--skip-tests', action='store_true', help='Content-only audit; tests must be verified separately before publication.')
parser.add_argument('--build', action='store_true', help='Also build packages without installing dependencies.')
parser.add_argument('--message')
parser.add_argument('commit_message', nargs='?')
args = parser.parse_args()
failures = 0
warnings = 0

def report(level, path, line, category):
    global failures, warnings
    # Escaping paths prevents terminal-control characters from becoming output.
    print(f'{level} {ascii(str(path))}:{line} [{category}]')
    failures += level == 'FAIL'
    warnings += level == 'WARN'

def git(*argv):
    return subprocess.check_output(['git', *argv], stderr=subprocess.DEVNULL)

try:
    git('rev-parse', '--show-toplevel')
    tracked = set(filter(None, git('ls-files', '-z').decode().split('\0')))
    new = set(filter(None, git('ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')))
    staged = set(filter(None, git('diff', '--cached', '--name-only', '-z', '--diff-filter=ACMR').decode().split('\0')))
    unmerged = set(filter(None, git('diff', '--name-only', '--diff-filter=U', '-z').decode().split('\0')))
except (subprocess.CalledProcessError, UnicodeDecodeError):
    print('FAIL Git candidate could not be read; audit aborted.')
    sys.exit(2)
for path in sorted(unmerged):
    report('FAIL', path, 0, 'unresolved-merge')

# Exact synthetic credentials used by the repository's disclosure regression tests.
# A test directory alone never exempts a credential from review.
DUMMY_TOKENS = {
    'sk-existing-test-credential', 'sk-new-test-credential-0123456789',
    'ghp_123456789012345678901234567890123456',
}
DUMMY_VALUES = DUMMY_TOKENS | {
    'super-secret-token-value', 'super-secret-broker-token-value',
    'fixture-provider-secret-without-a-standard-prefix',
    'example-arbitrary-source-value', 'example-patch-secret-marker',
    'example-question-assignment-marker', 't0p-S3cret-value', 'abc123def456ghi', 'Sup3rS3cret!',
    'example-broker-value', 'example-parent-only', 'example-key', 'test-only-key',
    'example-opaque-json-marker', 'password', 'do-not-display', 'also-private',
    'fake-dedicated-key', 'fake-test-key', '9f8e7d6c5b4a39281706',
}
TOKEN = re.compile(r'(?<![A-Za-z0-9_-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{15,})')
JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----')
ASSIGNMENT = re.compile(r'''(?i)\b(?:[A-Za-z_]*(?:api_?key|access_?token|auth_?token|password|passwd|secret|token))["']?\s*[=:]\s*(["'])([^"'\r\n]{8,})\1''')
BARE_ASSIGNMENT = re.compile(r'(?i)\b[A-Za-z_]*(?:api_?key|access_?token|auth_?token|password|passwd|secret|token)\s*[=:]\s*([A-Za-z0-9_+/=-]{16,})(?=$|[\s,}])')
CREDENTIAL_URL = re.compile(r'(?i)(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://([^\s/:]+):([^\s/@]+)@([^\s/]+)')
IP = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
HOST = re.compile(r'(?i)(?<![a-z0-9_.])(?:[a-z0-9][a-z0-9.-]*\.(?:ts\.net|internal|lan)|inference(?=:\d|/))(?![a-z0-9_.(])')
PERSONAL_PATH = re.compile(r'/(?:home/(?:floyd|douglas)|Users/(?!example\b|user\b)[A-Za-z][A-Za-z0-9._-]*)(?=/|\b)')
BUSINESS = re.compile(r'(?i)\b(?:company confidential|confidential (?:client|strategy|roadmap)|internal (?:strategy|roadmap)|slack\.com/archives/)\b')
ARTIFACT = re.compile(r'(^|/)(?:audit|records|backups|node_modules|__pycache__|\.venv|venv|dist|build|\.idea|\.vscode)(/|$)|(?:\.py[co]|\.log|\.sql|\.sqlite3?|\.db|\.pem|\.key|\.p12|\.pfx|\.bak(?:[-.]|$))$|(^|/)(?:\.env[^/]*|credentials(?:[^/]*\.json)?|\.git-credentials|\.netrc|\.npmrc|\.pypirc|id_rsa|id_ed25519|secrets\.(?:json|ya?ml)|\.DS_Store|Thumbs\.db)$')
CREDENTIAL_FILE = re.compile(r'(^|/)(?:\.env[^/]*|credentials[^/]*\.json|\.git-credentials|\.netrc|\.npmrc|\.pypirc|id_rsa|id_ed25519|secrets\.(?:json|ya?ml))$')
seen = set()
counts = {'working': 0, 'staged': 0, 'binary': 0}

def finding(level, path, line, category):
    key = (level, path, line, category)
    if key not in seen:
        seen.add(key)
        report(level, path, line, category)

def scan(path, data, source):
    counts[source] += 1
    metadata_level = 'WARN' if args.private else 'FAIL'
    fixture = path.startswith('tests/')
    if ARTIFACT.search(path) and not re.search(r'\.env\.(?:example|sample)$', path):
        # Private dated verification logs are existing internal evidence.
        level = 'WARN' if args.private and path.startswith('docs/gui-verification/') else 'FAIL'
        finding(level, path, 0, 'tracked-or-candidate-runtime-artifact')
    if len(data) > 5 * 1024 * 1024:
        finding('WARN', path, 0, 'large-file-review-lfs')
    if b'\0' in data:
        counts['binary'] += 1
        return
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        counts['binary'] += 1
        return
    for number, line in enumerate(text.splitlines(), 1):
        for match in TOKEN.finditer(line):
            if match.group() not in DUMMY_TOKENS and match.group() != 'AKIA' + '0' * 16:
                finding('FAIL', path, number, 'provider-token')
        if JWT.search(line):
            finding('FAIL', path, number, 'jwt')
        if PRIVATE_KEY.search(line):
            finding('FAIL', path, number, 'private-key')
        for match in ASSIGNMENT.finditer(line):
            value = match.group(2)
            dummy = fixture and (value in DUMMY_VALUES or value == 'test-key-with-quote\\')
            expression = value.startswith(('$', '{', '<', '\\'))
            if not dummy and not expression:
                finding('FAIL', path, number, 'credential-literal')
        for match in BARE_ASSIGNMENT.finditer(line):
            value = match.group(1)
            if not (fixture and value in DUMMY_VALUES) and re.search(r'\d', value):
                finding('FAIL', path, number, 'unquoted-credential-literal')
        for match in CREDENTIAL_URL.finditer(line):
            dummy = fixture and match.group(3).split(':')[0].endswith(('.example', '.example.com'))
            if not dummy:
                finding('FAIL', path, number, 'credential-url')
        for match in IP.finditer(line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if address.is_loopback or address.is_unspecified or any(address in ipaddress.ip_network(net) for net in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')):
                continue
            if address == ipaddress.ip_network('100.64.0.0/10').network_address and line[match.end():].startswith('/10'):
                continue  # Public protocol range, not a deployment endpoint.
            if path in {'tests/test_secret_gate.py', 'scripts/git-steward-audit.sh'} and match.group() in {'10.0.0.5', '172.20.1.4', '192.168.1.10', '10.0.0.1', '192.168.0.1'}:
                continue  # Exact synthetic address cases in the detector's own tests.
            if address.is_private or address in ipaddress.ip_network('100.64.0.0/10'):
                finding(metadata_level, path, number, 'private-address')
        for match in HOST.finditer(line):
            if match.group().lower() != 'example.ts.net' and not match.group().lower().endswith('.example.ts.net') and not (path in {'tests/test_secret_gate.py', 'scripts/git-steward-audit.sh'} and match.group() == 'app.prod.internal') and not line[match.end():].lstrip().startswith('='):
                finding(metadata_level, path, number, 'internal-hostname')
        if PERSONAL_PATH.search(line):
            finding(metadata_level, path, number, 'personal-machine-path')
        if BUSINESS.search(line) and not (path == 'scripts/git-steward-audit.sh' and line.startswith('BUSINESS =')):
            finding(metadata_level, path, number, 'private-business-content-review')

for path in sorted(tracked | staged | new):
    file = Path(path)
    if CREDENTIAL_FILE.search(path) and not re.search(r'\.env\.(?:example|sample)$', path):
        finding('FAIL', path, 0, 'credential-file-must-not-be-published')
        continue  # Identify by name; never open local credential stores.
    if file.is_symlink():
        finding('WARN', path, 0, 'symlink-target-review')
        scan(path, os.readlink(file).encode(), 'working')
        continue
    if file.is_file():
        try:
            scan(path, file.read_bytes(), 'working')
        except OSError:
            finding('FAIL', path, 0, 'unreadable-candidate')
for path in sorted(staged - unmerged):
    if CREDENTIAL_FILE.search(path) and not re.search(r'\.env\.(?:example|sample)$', path):
        finding('FAIL', path, 0, 'credential-file-must-not-be-published')
        continue
    try:
        scan(path, git('show', ':' + path), 'staged')
    except subprocess.CalledProcessError:
        finding('FAIL', path, 0, 'unreadable-staged-candidate')

# Check ignored new files without opening their contents, including credentials.
for sample in ('.env', '.env.production', 'node_modules/a', '.idea/workspace.xml', '.DS_Store', 'audit/a', 'records/a', 'dist/a', 'credentials.json'):
    if subprocess.run(['git', 'check-ignore', '-q', '--no-index', sample], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        finding('FAIL', '.gitignore', 0, 'missing-ignore-' + sample.replace('/', '-'))
message = args.message or args.commit_message
if message and not re.fullmatch(r'(feat|fix|docs|style|refactor|perf|test|build|ci|chore)(\([a-z0-9._-]+\))?!?: [^\r\n]{3,}', message.splitlines()[0]):
    finding('FAIL', 'COMMIT_MESSAGE', 1, 'conventional-commit-required')
elif not message:
    finding('WARN', 'COMMIT_MESSAGE', 0, 'message-not-supplied')
try:
    branch = git('symbolic-ref', '--short', 'HEAD').decode().strip()
    if branch in ('main', 'master'):
        finding('WARN', 'HEAD', 0, 'direct-main-commit-review')
    try:
        upstream = git('rev-parse', '--abbrev-ref', '@{upstream}').decode().strip()
        behind = int(git('rev-list', '--count', 'HEAD..' + upstream))
        if behind:
            finding('FAIL', 'HEAD', 0, 'branch-behind-tracking-ref')
    except subprocess.CalledProcessError:
        finding('WARN', 'HEAD', 0, 'upstream-not-configured')
except subprocess.CalledProcessError:
    finding('FAIL', 'HEAD', 0, 'detached-head')
print('SCAN COUNTS ' + ' '.join(f'{key}={value}' for key, value in counts.items()))
# Never run candidate code after finding a disclosure or unresolved merge.
if not failures:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH='src')
    if args.skip_tests:
        finding('WARN', 'tests', 0, 'tests-skipped-content-audit-only')
    else:
        with tempfile.TemporaryFile() as log:
            result = subprocess.run([sys.executable, '-W', 'error::ResourceWarning', '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'], stdout=log, stderr=subprocess.STDOUT, env=env)
        if result.returncode:
            finding('FAIL', 'tests', 0, 'unit-suite-exit-' + str(result.returncode))
        else:
            print('PASS unit suite exited 0')
    if args.build:
        with tempfile.TemporaryDirectory(prefix='steward-build-') as output, tempfile.TemporaryFile() as log:
            result = subprocess.run([sys.executable, '-m', 'build', '--no-isolation', '--outdir', output], stdout=log, stderr=subprocess.STDOUT, env=env)
        if result.returncode:
            finding('FAIL', 'pyproject.toml', 0, 'package-build-exit-' + str(result.returncode))
        else:
            print('PASS package build exited 0')
print(f'STEWARD AUDIT: {"FAIL" if failures else "PASS"} ({failures} failures, {warnings} warnings)')
print('Scope: candidate content and requested checks; history, binary pixels and remote CI require separate review.')
sys.exit(1 if failures else 0)
PY
