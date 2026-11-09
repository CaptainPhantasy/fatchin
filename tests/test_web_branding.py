"""Commercial icon and document contracts; no image library needed at runtime."""

import json
import struct
import unittest
from pathlib import Path

from ghagent.web import WEB_ROOT
from tests import test_web_http as http_fixture
from tests.test_web_control import GuiFixture


class BrandAssetsTests(unittest.TestCase):
    def test_png_pyramid_has_rgba_at_each_required_size(self):
        for name, size in [
            ("favicon-16x16.png", 16),
            ("favicon-32x32.png", 32),
            ("apple-touch-icon.png", 180),
            ("icon-192.png", 192),
            ("icon-512.png", 512),
        ]:
            with self.subTest(name=name):
                data = (WEB_ROOT / name).read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(
                    struct.unpack(">IIBB", data[16:26]), (size, size, 8, 6)
                )

    def test_svg_is_the_pig_not_a_background_tile(self):
        import xml.etree.ElementTree as ET

        root = ET.parse(WEB_ROOT / "favicon.svg").getroot()
        self.assertEqual(root.attrib["viewBox"], "0 0 64 64")
        self.assertEqual(root.findall(".//{http://www.w3.org/2000/svg}rect"), [])
        self.assertIn("pig", (WEB_ROOT / "favicon.svg").read_text().lower())

    def test_ico_contains_required_frames(self):
        data = (WEB_ROOT / "favicon.ico").read_bytes()
        self.assertEqual(struct.unpack("<HHH", data[:6]), (0, 1, 3))
        self.assertEqual(
            {(data[6 + i * 16], data[7 + i * 16]) for i in range(3)},
            {(16, 16), (32, 32), (48, 48)},
        )

    def test_document_and_manifest_link_all_icon_levels(self):
        page = (WEB_ROOT / "index.html").read_text()
        for name in [
            "favicon.ico",
            "favicon.svg",
            "favicon-16x16.png",
            "favicon-32x32.png",
            "apple-touch-icon.png",
            "manifest.webmanifest",
        ]:
            self.assertIn('href="/' + name + '"', page)
        self.assertIn('name="apple-mobile-web-app-title" content="FatChin"', page)
        manifest = json.loads((WEB_ROOT / "manifest.webmanifest").read_text())
        icons = {Path(i["src"]).name: i for i in manifest["icons"]}
        for name, size in [
            ("favicon.svg", "any"),
            ("icon-192.png", "192x192"),
            ("icon-512.png", "512x512"),
        ]:
            self.assertEqual(icons[name]["sizes"], size)
            self.assertEqual(icons[name]["purpose"], "any")


class BrandHttpTests(GuiFixture, unittest.TestCase):
    setUp = http_fixture.GuiHttpTests.setUp
    stop_server = http_fixture.GuiHttpTests.stop_server
    request = http_fixture.GuiHttpTests.request

    def test_root_icon_alias_rejects_symlink_escape(self):
        (self.web_root / "favicon.ico").symlink_to(self.root / "private.txt")
        self.assertEqual(self.request("GET", "/favicon.ico")[0], 404)

    def test_root_icon_aliases_keep_security_and_explicit_mimes(self):
        # Serve actual release assets through the same host/origin guard as the API.
        from ghagent.web import handler_factory

        self.server.RequestHandlerClass = handler_factory(
            self.control, web_root=WEB_ROOT
        )
        for name, mime in [
            ("favicon.ico", "image/vnd.microsoft.icon"),
            ("favicon.svg", "image/svg+xml"),
            ("favicon-16x16.png", "image/png"),
            ("favicon-32x32.png", "image/png"),
            ("apple-touch-icon.png", "image/png"),
            ("icon-192.png", "image/png"),
            ("icon-512.png", "image/png"),
            ("manifest.webmanifest", "application/manifest+json"),
            ("brand/fatchin-logo.png", "image/png"),
        ]:
            for prefix in ["/", "/static/"]:
                with self.subTest(name=name, prefix=prefix):
                    status, headers, data = self.request("GET", prefix + name)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Type"], mime)
                    self.assertTrue(data)
                    self.assertIn(
                        "manifest-src 'self'", headers["Content-Security-Policy"]
                    )
                    self.assertEqual(
                        self.request(
                            "GET", prefix + name, headers={"Host": "attacker.example"}
                        )[0],
                        403,
                    )
        self.assertEqual(self.request("GET", "/private.txt")[0], 404)
