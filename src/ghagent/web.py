"""FatChin private mesh GUI. Python standard library; no build step.

Run: python3 -m ghagent.web --host 127.0.0.1 --port 18465
The installed mesh listener uses the certificate managed by Tailscale.
"""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .github_client import GitHubError, redact_text
from .web_control import ApiError, VERSION, WebControl, target_number

WEB_ROOT = Path(__file__).resolve().parents[2] / 'web'


class GuiServer(ThreadingHTTPServer):
    """TLS handshakes run in request threads; renewed certificates load live."""

    def __init__(self, address, handler, *, tls_cert=None, tls_key=None):
        if bool(tls_cert) != bool(tls_key):
            raise ValueError('Supply both the TLS certificate and private key.')
        self._tls_paths = (Path(tls_cert).expanduser(), Path(tls_key).expanduser()) if tls_cert else None
        self._tls_context = None
        self._tls_signature = None
        self._tls_update_failed = False
        if self._tls_paths:
            self._reload_tls(initial=True)
        super().__init__(address, handler)

    def _reload_tls(self, *, initial=False):
        try:
            stats = [p.stat() for p in self._tls_paths]
            signature = tuple((s.st_mtime_ns, s.st_ino, s.st_size) for s in stats)
            if signature == self._tls_signature:
                return
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(['http/1.1'])
            context.load_cert_chain(*map(str, self._tls_paths))
        except (OSError, ValueError):
            if initial:
                raise
            if not self._tls_update_failed:
                print('FatChin could not load the certificate update; the previous certificate remains active.', file=sys.stderr)
            self._tls_update_failed = True
            return
        self._tls_context = context
        self._tls_signature = signature
        self._tls_update_failed = False

    def get_request(self):
        connection, address = super().get_request()
        if self._tls_paths:
            self._reload_tls()
            try:
                connection = self._tls_context.wrap_socket(connection, server_side=True,
                                                            do_handshake_on_connect=False)
            except Exception:
                connection.close()
                raise
        return connection, address

    def handle_error(self, request, client_address):
        if self._tls_paths and isinstance(sys.exc_info()[1], (ssl.SSLError, TimeoutError, ConnectionError)):
            print('FatChin closed an incomplete or invalid TLS connection.', file=sys.stderr)
        else:
            super().handle_error(request, client_address)


def mesh_hostname(value):
    value = str(value).lower().rstrip('.')
    if len(value) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.')):
        raise argparse.ArgumentTypeError('Use a DNS hostname without a scheme, path, or port.')
    return value


def handler_factory(control, *, web_root=WEB_ROOT):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'FatChin/' + VERSION
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, fmt, *args):
            # Never log a request body, key, auth code or query value.
            print(f'FatChin {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ""}', file=sys.stderr)

        def send(self, data, status=200, content_type='application/json; charset=utf-8', extra=None):
            # Additive envelope: every JSON reply names its outcome, its next
            # step, and the actions available — without changing any existing
            # key, because the page reads those keys directly.
            if isinstance(data, dict) and content_type.startswith('application/json'):
                data = dict(data)
                data.setdefault('status', 'ok' if status < 400 else 'error')
                data.setdefault('hint', '')
                data.setdefault('actions_available', [])
            payload = json.dumps(data, ensure_ascii=False).encode() if not isinstance(data, bytes) else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)

        def params(self):
            return {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}

        def guard(self):
            host = self.headers.get('Host', '').lower()
            allowed = set(getattr(self.server, 'allowed_hosts', []))
            if host not in allowed:
                raise ApiError('Open the application from its published mesh address.', 403)
            origin = self.headers.get('Origin')
            scheme = 'https' if isinstance(self.connection, ssl.SSLSocket) else 'http'
            if origin and origin != f'{scheme}://{host}':
                raise ApiError('This action must be requested from the FatChin page.', 403)
            # A launcher can open the workspace from a different browser site.
            # Only the entry document gets this navigation exception; API reads,
            # embeds and mutations still require the normal origin checks.
            entry_navigation = (self.command == 'GET'
                                and urlsplit(self.path).path in {'/', '/index.html'}
                                and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                                and self.headers.get('Sec-Fetch-Dest') == 'document')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not entry_navigation:
                raise ApiError('Open FatChin directly before continuing.', 403)
            required = getattr(self.server, 'admin_token', '')
            if required and urlsplit(self.path).path.startswith('/api/'):
                supplied = self.headers.get('Authorization', '')
                if supplied.lower().startswith('bearer '):
                    supplied = supplied[7:]
                supplied = supplied or self.headers.get('X-Fatchin-Token', '')
                if not hmac.compare_digest(supplied, required):
                    raise ApiError('Authentication required. Enter the admin token for this FatChin instance.', 401)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                self.guard()
                path = urlsplit(self.path).path
                p = self.params()
                if path == '/api/state':
                    return self.send(control.state())
                if path == '/api/fleet':
                    return self.send(control.fleet_state())
                if path == '/api/explore/catalog':
                    return self.send(control.explore_catalog())
                if path == '/api/explore':
                    return self.send(control.explore(p))
                if path == '/api/learn':
                    return self.send(control.learning_get(p.get('id', '')))
                if path == '/api/diagnostics':
                    return self.send(control.diagnostics())
                if path == '/api/github/repositories':
                    return self.send(control.repositories(p.get('q', '')))
                if path == '/api/github/user':
                    return self.send(control.github_user(p.get('login', ''), p.get('id')))
                if path == '/api/github/branches':
                    return self.send(control.branches(p.get('repo')))
                if path == '/api/github/targets':
                    return self.send(control.targets(p.get('repo'), p.get('kind'), p.get('state', 'open')))
                if path == '/api/github/target':
                    return self.send({'item': control.target(p.get('repo'), p.get('kind'), p.get('number'))})
                if path == '/api/github/connect':
                    return self.send(control.connection_status())
                if path == '/api/folders':
                    return self.send(control.folders(p.get('account'), p.get('path')))
                if path == '/api/task-types':
                    return self.send(control.task_types())
                if path == '/api/fence':
                    return self.send(control.fence())
                if path == '/api/digest':
                    return self.send(control.digest())
                if path == '/api/authority':
                    return self.send(control.authority_view())
                if path == '/api/jobs/files':
                    return self.send(control.job_files(p.get('job_id'), p.get('path', '')))
                if path.startswith('/api/jobs/'):
                    parts = path.strip('/').split('/')
                    if len(parts) == 4 and parts[3] == 'download':
                        name, content = control.job_download(parts[2], p.get('path', ''))
                        return self.send(content, content_type='application/octet-stream',
                                         extra={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe='')})
                    job = control.queue().get(parts[2]) if len(parts) >= 3 else None
                    if not job:
                        raise ApiError('Task not found.', 404)
                    value = json.loads(job.to_json())
                    if len(parts) == 4 and parts[3] == 'report':
                        return self.send(value, extra={'Content-Disposition': f'attachment; filename="gha-worker-{job.job_id}.json"'})
                    if len(parts) == 3:
                        return self.send({'job': value})
                if path.startswith('/api/'):
                    raise ApiError('This application page or operation was not found.', 404)
                if path in {'/', '/index.html'}:
                    target = web_root / 'index.html'
                elif path.startswith('/static/'):
                    target = (web_root / unquote(path[len('/static/'):])).resolve()
                    if not target.is_relative_to(web_root.resolve()) or any(part.startswith('.') for part in target.relative_to(web_root.resolve()).parts):
                        raise ApiError('Asset not found.', 404)
                elif path.startswith('/docs/'):
                    name = path.rsplit('/', 1)[-1]
                    if name not in {'GUI-GUIDE.md', 'GUI-OPERATIONS.md', 'GUI-CAPABILITIES.md', 'GUI-ACCEPTANCE.md',
                                    'EXPLORE-AND-LEARN.md', 'GITHUB-EXPLORER-FACTS.md'}:
                        raise ApiError('Guide not found.', 404)
                    target = WEB_ROOT.parent / 'docs' / name
                else:
                    raise ApiError('Page not found.', 404)
                if not target.is_file():
                    raise ApiError('This interface asset is not installed. Reinstall the matching GUI package.', 503)
                mime = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
                if target.suffix == '.md':
                    mime = 'text/plain; charset=utf-8'
                self.send(target.read_bytes(), content_type=mime)
            except Exception as error:
                self.error(error)

        def do_POST(self):
            try:
                self.guard()
                if self.headers.get_content_type() != 'application/json':
                    raise ApiError('This action requires JSON data.', 415)
                if self.headers.get('Transfer-Encoding'):
                    raise ApiError('Send a request with a known size.', 400)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise ApiError('The request size is invalid.', 400) from None
                if not 0 < length <= 65536:
                    raise ApiError('The request is empty or exceeds 64 KB.', 413)
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    raise ApiError('The request could not be read. Reload the page and try again.') from None
                if not isinstance(body, dict):
                    raise ApiError('The request must contain named values.')
                path = urlsplit(self.path).path
                actions = {'/api/jobs': control.enqueue, '/api/folders': control.create_folder,
                           '/api/repos': control.save_repo, '/api/repos/remove': control.remove_repo,
                           '/api/settings': control.save_settings, '/api/watches': control.add_watch,
                           '/api/service': control.service_action, '/api/provider-check': control.provider_check,
                           '/api/credentials': control.credential, '/api/authority': control.set_authority}
                if path in actions:
                    return self.send(actions[path](body))
                if path == '/api/fleet':
                    return self.send(control.fleet_action(body))
                if path == '/api/learn':
                    return self.send(control.learning_action(body))
                if path == '/api/github/connect':
                    return self.send(control.connect_github())
                if path == '/api/watches/remove':
                    repo = control.validate_repo(body.get('repo'))
                    if body.get('mode') not in {'all', 'selected'}:
                        raise ApiError('Choose the selected or all-pull-request watch to remove.')
                    number = '*' if body.get('mode') == 'all' else target_number(body.get('number'))
                    control.watches().remove(repo, number)
                    return self.send({'ok': True})
                if path.startswith('/api/jobs/'):
                    parts = path.strip('/').split('/')
                    if len(parts) == 4 and parts[3] in {'stop', 'retry'}:
                        return self.send(control.job_action(parts[2], parts[3]))
                    if len(parts) == 4 and parts[3] == 'draft-action':
                        return self.send(control.draft_action(parts[2], body))
                raise ApiError('This operation was not found.', 404)
            except Exception as error:
                self.error(error)

        def error(self, error):
            if isinstance(error, ApiError):
                status, message = error.status, str(error)
            elif isinstance(error, GitHubError):
                status, message = 502, 'GitHub could not complete this request. Check your connection, repository access, and GitHub rate limits, then retry.'
            else:
                status, message = 503, 'The worker records or settings could not be read. Existing data has been preserved. Run Connection checks or contact the tool maintainer.'
                print(f'FatChin error {type(error).__name__}', file=sys.stderr)
            try:
                self.send({'error': redact_text(message),
                           'hint': 'Fix the problem named above and try again; '
                                   'Connection checks explain GitHub access issues.',
                           'actions_available': ['reload', 'diagnostics']}, status)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def make_server(control, host='127.0.0.1', port=18465, *, web_root=WEB_ROOT, hostnames=(), tls_cert=None, tls_key=None, token=''):
    aliases = [mesh_hostname(name) for name in hostnames]
    server = GuiServer((host, port), handler_factory(control, web_root=web_root),
                       tls_cert=tls_cert, tls_key=tls_key)
    server.daemon_threads = True
    bound = server.server_port
    server.allowed_hosts = {f'{host}:{bound}', f'localhost:{bound}'}
    server.allowed_hosts.update(f'{name}:{bound}' for name in aliases)
    server.admin_token = str(token or '')
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='FatChin private graphical interface for the existing GitHub worker.')
    parser.add_argument('--host', default='127.0.0.1', help='Loopback or the host mesh IP. Never bind to a public interface.')
    parser.add_argument('--port', type=int, default=18465)
    parser.add_argument('--hostname', action='append', type=mesh_hostname, default=[],
                        help='Allow this mesh DNS hostname for the same private listener. May be repeated.')
    parser.add_argument('--tls-cert', help='Certificate chain in PEM format for native HTTPS.')
    parser.add_argument('--tls-key', help='Private key matching --tls-cert. Both TLS paths are required together.')
    parser.add_argument('--config', default='~/.config/gha-worker/config.toml')
    parser.add_argument('--env-file', default='~/.config/gha-worker/env')
    parser.add_argument('--token', default=os.environ.get('FATCHIN_ADMIN_TOKEN', ''),
                        help='Admin token required for /api/*. Required when binding beyond loopback.')
    args = parser.parse_args(argv)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error('Supply both --tls-cert and --tls-key to enable HTTPS.')
    if args.host not in {'127.0.0.1', 'localhost'}:
        import ipaddress
        try:
            if ipaddress.ip_address(args.host) not in ipaddress.ip_network('100.64.0.0/10'):
                parser.error('Use loopback or this server’s mesh IP.')
        except ValueError:
            parser.error('Use a numeric mesh IP or 127.0.0.1.')
    if args.host not in {'127.0.0.1', 'localhost'} and not args.token:
        parser.error('Binding beyond loopback requires --token or FATCHIN_ADMIN_TOKEN. '
                     'Use a long random value and send it as a Bearer token.')
    os.umask(0o002)
    control = WebControl(args.config, args.env_file)
    control.config()
    try:
        server = make_server(control, args.host, args.port, hostnames=args.hostname,
                             tls_cert=args.tls_cert, tls_key=args.tls_key,
                             token=args.token)
    except OSError as error:
        print(f'Cannot start FatChin on {args.host}:{args.port}: {error}. Check the port and HTTPS certificate files; existing services were not changed.', file=sys.stderr)
        return 1
    address = args.hostname[0] if args.hostname else args.host
    scheme = 'https' if args.tls_cert else 'http'
    print(f'FatChin {VERSION}: {scheme}://{address}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
