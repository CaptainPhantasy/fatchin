"""The version lives in exactly one place and is always visible."""
import re
import unittest

from ghagent import __version__
from ghagent.web_control import BUILD, VERSION


class VersionTests(unittest.TestCase):
    def test_single_source_of_truth(self):
        # __init__ and web_control must both read VERSION, never their own copy.
        self.assertEqual(VERSION, __version__)

    def test_version_is_semver(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_build_stamp_is_reported(self):
        # A version nobody can trace to a build is just a label.
        self.assertTrue(BUILD)
        self.assertNotEqual(BUILD, "")

    def test_version_file_matches_code(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), VERSION)


if __name__ == "__main__":
    unittest.main()
