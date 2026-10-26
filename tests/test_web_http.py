"""Real loopback HTTP requests through the GUI server; no external services."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading
import unittest

from ghagent.github_client import GitHubError
from ghagent.web import make_server
from tests.test_web_control import GuiFixture


class GuiHttpTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.web_root = self.root / 'web'
        self.web_root.mkdir()
        (self.web_root / 'index.html').write_text('<!doctype html><title>Worker fixture</title>')
        (self.web_root / 'app.js').write_text('"use strict";')
        (self.web_root / '.private').write_text('not an asset')
        (self.root / 'private.txt').write_text('must not escape static root')
        (self.web_root / 'escape.txt').symlink_to(self.root / 'private.txt')
        self.server = make_server(self.control, '127.0.0.1', 0, web_root=self.web_root)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.host = f'127.0.0.1:{self.server.server_port}'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        values = {'Host': self.host, **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body)
            values.setdefault('Content-Type', 'application/json')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_api_requires_the_admin_token_when_configured(self):
        self.stop_server()
        self.server = make_server(self.control, '127.0.0.1', 0,
                                  web_root=self.web_root, token='secret')
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.request('GET', '/api/state')[0], 401)
        self.assertEqual(self.request('GET', '/api/state', headers={
            'Authorization': 'Bearer secret'})[0], 200)

    def test_page_assets_and_state_work_with_standard_browser_security_headers(self):
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'Worker fixture', body)
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, _, body = self.request('GET', '/static/app.js')
        self.assertEqual((status, body), (200, b'"use strict";'))
        status, _, body = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state['github']['login'], 'test-member')
        self.assertEqual(state['repos'][0]['full_name'], 'owner/repo')
        self.assertNotIn(b'sk-existing-test-credential', body)

    def test_head_has_headers_without_body(self):
        status, headers, body = self.request('HEAD', '/')
        self.assertEqual(status, 200)
        self.assertGreater(int(headers['Content-Length']), 0)
        self.assertEqual(body, b'')

    def test_cross_origin_posts_and_unrecognized_hosts_are_rejected_before_mutation(self):
        cases = ({'Origin': 'https://attacker.example'}, {'Host': 'attacker.example'},
                 {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'})
        for headers in cases:
            with self.subTest(headers=headers):
                status, _, body = self.request('POST', '/api/folders',
                    {'account': 'alice', 'parent': str(self.home), 'name': 'Injected'}, headers)
                self.assertEqual(status, 403)
                self.assertIn('error', json.loads(body))
        self.assertFalse((self.home / 'Injected').exists())

    def test_toolbench_cross_site_document_navigation_opens_the_workspace(self):
        headers = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
                   'Sec-Fetch-Dest': 'document', 'Sec-Fetch-User': '?1'}
        for path in ['/', '/index.html', '/?from=toolbench']:
            with self.subTest(path=path):
                status, _, body = self.request('GET', path, headers=headers)
                self.assertEqual(status, 200)
                self.assertIn(b'Worker fixture', body)

    def test_document_navigation_exception_does_not_allow_cross_site_api_or_embeds(self):
        headers = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
                   'Sec-Fetch-Dest': 'document'}
        for method, path, overrides in [
            ('GET', '/api/state', {}),
            ('GET', '/api/github/connect', {}),
            ('GET', '/static/app.js', {}),
            ('GET', '/', {'Sec-Fetch-Dest': 'iframe'}),
            ('GET', '/', {'Sec-Fetch-Mode': 'cors'}),
            ('GET', '/', {'Host': 'attacker.example'}),
            ('GET', '/', {'Origin': 'https://attacker.example'}),
            ('HEAD', '/', {}),
            ('POST', '/api/folders', {}),
        ]:
            with self.subTest(method=method, path=path, overrides=overrides):
                body = {'account': 'alice', 'parent': str(self.home), 'name': 'Injected'} if method == 'POST' else None
                status, _, _ = self.request(method, path, body, {**headers, **overrides})
                self.assertEqual(status, 403)
        self.assertFalse((self.home / 'Injected').exists())

    def test_same_origin_json_folder_creation_and_task_submission(self):
        origin = {'Origin': 'http://' + self.host, 'Sec-Fetch-Site': 'same-origin'}
        status, _, body = self.request('POST', '/api/folders',
            {'account': 'alice', 'parent': str(self.home), 'name': 'New work'}, origin)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['path'], str(self.home / 'New work'))
        status, _, body = self.request('POST', '/api/jobs', self.task_data(), origin)
        self.assertEqual(status, 200)
        job_id = json.loads(body)['job']['job_id']
        status, _, body = self.request('POST', f'/api/jobs/{job_id}/stop', {}, origin)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['job']['state'], 'stopped')

    def test_json_validation_has_readable_errors_and_no_side_effects(self):
        cases = [('text', {'Content-Type': 'text/plain'}, 415),
                 ('{invalid', {'Content-Type': 'application/json'}, 400),
                 ('[]', {'Content-Type': 'application/json'}, 400),
                 (b'\xff', {'Content-Type': 'application/json'}, 400),
                 ('{}', {'Content-Type': 'application/json', 'Content-Length': '65537'}, 413),
                 ('{}', {'Content-Type': 'application/json', 'Content-Length': '-1'}, 413),
                 ('{}', {'Content-Type': 'application/json', 'Content-Length': 'bad'}, 400)]
        for payload, headers, expected in cases:
            with self.subTest(payload=payload, headers=headers):
                status, _, body = self.request('POST', '/api/jobs', payload, headers)
                self.assertEqual(status, expected)
                self.assertTrue(json.loads(body)['error'])
        self.assertEqual(self.control.queue().all(), [])

    def test_static_traversal_hidden_files_and_symlink_escapes_are_not_served(self):
        for path in ('/static/../private.txt', '/static/%2e%2e/private.txt',
                     '/static/.private', '/static/escape.txt', '/docs/not-approved.md'):
            with self.subTest(path=path):
                status, _, body = self.request('GET', path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'must not escape', body)
                self.assertNotIn(b'not an asset', body)

    def test_report_download_contains_this_job_with_attachment_name(self):
        self.persisted_job()
        status, headers, body = self.request('GET', '/api/jobs/job_test/report')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['job_id'], 'job_test')
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertIn('gha-worker-job_test.json', headers['Content-Disposition'])

    def test_failed_github_request_never_echoes_upstream_credentials(self):
        self.gh.error = GitHubError('upstream token ghp_123456789012345678901234567890123456')
        status, _, body = self.request('GET', '/api/github/repositories')
        self.assertEqual(status, 502)
        self.assertNotIn(b'ghp_', body)
        self.assertIn('GitHub', json.loads(body)['error'])

    def test_corrupt_worker_state_is_reported_and_preserved(self):
        records = self.state / 'watched.json'
        records.write_text('{broken record')
        with contextlib.redirect_stderr(io.StringIO()):
            status, _, body = self.request('GET', '/api/state')
        self.assertEqual(status, 503)
        self.assertIn('preserved', json.loads(body)['error'])
        self.assertEqual(records.read_text(), '{broken record')

    def test_unknown_api_operations_are_json_errors(self):
        for method, path, body in (('GET', '/api/missing', None), ('POST', '/api/missing', {})):
            with self.subTest(method=method):
                status, headers, payload = self.request(method, path, body)
                self.assertEqual(status, 404)
                self.assertIn('application/json', headers['Content-Type'])
                self.assertTrue(json.loads(payload)['error'])

    def test_binary_result_download_is_scoped_and_is_an_attachment(self):
        job = self.persisted_job(notes={'output_dir': str(self.output), 'member': 'alice'})
        artifact = self.output / ('gha-worker-' + job.job_id)
        artifact.mkdir()
        (artifact / 'result.bin').write_bytes(b'\x00\x01binary output')
        status, headers, body = self.request('GET', f'/api/jobs/{job.job_id}/download?path=result.bin')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'\x00\x01binary output')
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertIn('result.bin', headers['Content-Disposition'])
        status, _, _ = self.request('GET', f'/api/jobs/{job.job_id}/download?path=../private.txt')
        self.assertEqual(status, 403)
