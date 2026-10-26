"""Real TLS, browser origins, renewal, and slow-handshake isolation."""
import contextlib
import hashlib
import http.client
import io
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from ghagent.web import main, make_server
from tests.test_web_control import GuiFixture


class HttpsTests(GuiFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which('openssl')
        if not openssl:
            raise unittest.SkipTest('openssl is required for temporary TLS fixtures')
        cls.cert_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.cert_temp.cleanup)
        cls.cert_root = Path(cls.cert_temp.name)
        for name in ['first', 'renewed']:
            subprocess.run([openssl, 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                'ec_paramgen_curve:prime256v1', '-noenc', '-days', '1',
                '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
                '-keyout', str(cls.cert_root / (name + '.key')),
                '-out', str(cls.cert_root / (name + '.crt'))],
                check=True, capture_output=True, timeout=10)
        cls.ca_bundle = cls.cert_root / 'trusted-fixture-certs.pem'
        cls.ca_bundle.write_text(''.join((cls.cert_root / (n + '.crt')).read_text()
                                       for n in ['first', 'renewed']))

    def setUp(self):
        self.build_gui()
        self.cert = self.root / 'active.crt'
        self.key = self.root / 'active.key'
        self.activate('first')
        self.context = ssl.create_default_context(cafile=str(self.ca_bundle))
        self.server = make_server(self.control, '127.0.0.1', 0, tls_cert=self.cert, tls_key=self.key)
        self.host = f'127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def activate(self, name):
        shutil.copyfile(self.cert_root / (name + '.crt'), self.cert)
        shutil.copyfile(self.cert_root / (name + '.key'), self.key)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, *, origin=None, extra=None, create=False):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_port,
                                                  timeout=3, context=self.context)
        headers = {'Host': self.host, **(extra or {})}
        if origin:
            headers['Origin'] = origin
        try:
            if create:
                headers.update({'Content-Type': 'application/json', 'Sec-Fetch-Site': 'same-origin'})
                connection.request('POST', '/api/folders',
                    json.dumps({'account': 'alice', 'parent': str(self.home), 'name': 'Secure work'}), headers)
            else:
                connection.request('GET', '/api/state', headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def certificate_digest(self):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as raw:
            with self.context.wrap_socket(raw, server_hostname='localhost') as connection:
                return hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest()

    def expected_digest(self, name):
        der = ssl.PEM_cert_to_DER_cert((self.cert_root / (name + '.crt')).read_text())
        return hashlib.sha256(der).hexdigest()

    def test_tls_and_same_origin_form_work_with_certificate_verification(self):
        self.assertEqual(self.request()[0], 200)
        status, result = self.request(origin='https://' + self.host, create=True,
                                      extra={'X-Forwarded-Proto': 'http'})
        self.assertEqual(status, 200)
        self.assertEqual(result['path'], str(self.home / 'Secure work'))
        self.assertTrue((self.home / 'Secure work').is_dir())

    def test_origin_guards_use_actual_tls_not_forwarded_headers(self):
        for origin in ['http://' + self.host, 'https://attacker.example', 'null']:
            with self.subTest(origin=origin):
                self.assertEqual(self.request(origin=origin, create=True,
                    extra={'X-Forwarded-Proto': 'https'})[0], 403)
        self.assertFalse((self.home / 'Secure work').exists())

    def test_pending_handshake_does_not_block_other_clients(self):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3):
            self.assertEqual(self.request()[0], 200)

    def test_plaintext_http_is_not_served_on_the_https_listener(self):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as connection:
            connection.sendall(b'GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n')
            try:
                received = connection.recv(1024)
            except ConnectionResetError:
                received = b''
        self.assertNotIn(b'200 OK', received)
        self.assertEqual(self.request()[0], 200)

    def test_renewed_certificate_is_loaded_without_restarting_the_server(self):
        self.assertEqual(self.certificate_digest(), self.expected_digest('first'))
        self.activate('renewed')
        self.assertEqual(self.certificate_digest(), self.expected_digest('renewed'))
        self.assertEqual(self.request()[0], 200)

    def test_incomplete_update_keeps_previous_certificate_then_recovers(self):
        self.cert.write_text('not a certificate')
        self.assertEqual(self.certificate_digest(), self.expected_digest('first'))
        self.assertEqual(self.request()[0], 200)
        self.activate('renewed')
        self.assertEqual(self.certificate_digest(), self.expected_digest('renewed'))


class HttpsConfigurationTests(unittest.TestCase):
    def test_certificate_and_key_must_be_supplied_together(self):
        for option in ['--tls-cert', '--tls-key']:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                main([option, '/unconfigured-fixture-path'])
            self.assertEqual(error.exception.code, 2)
            self.assertIn('both --tls-cert and --tls-key', stderr.getvalue())
