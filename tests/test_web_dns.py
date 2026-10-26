"""The readable mesh address uses the existing private listener."""
import http.client
import threading
import unittest

from ghagent.web import make_server, mesh_hostname
from tests.test_web_control import GuiFixture


class MeshDnsTests(GuiFixture, unittest.TestCase):
    def setUp(self):
        self.build_gui()
        self.server = make_server(self.control, '127.0.0.1', 0, hostnames=['worker.example.ts.net'])
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, host, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Host': f'{host}:{self.server.server_port}'}
        if origin:
            headers['Origin'] = f'http://{origin}:{self.server.server_port}'
        try:
            connection.request('GET', '/api/state', headers=headers)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def test_explicit_mesh_name_works_without_accepting_unrelated_hosts_or_origins(self):
        self.assertEqual(self.request('worker.example.ts.net', 'worker.example.ts.net'), 200)
        self.assertEqual(self.request('WORKER.EXAMPLE.TS.NET'), 200)
        self.assertEqual(self.request('unrelated.example'), 403)
        self.assertEqual(self.request('worker.example.ts.net', 'unrelated.example'), 403)

    def test_hostname_option_rejects_urls_ports_wildcards_and_invalid_labels(self):
        import argparse
        for value in ('', '*.example.net', 'https://example.net', 'example.net:443', 'bad..example', '-bad.example', 'bad/example'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                mesh_hostname(value)
        self.assertEqual(mesh_hostname('Inference.Example.ts.net.'), 'inference.example.ts.net')
