"""Run small real projects through dependency preparation and isolated checks."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tests import sandbox_available

from ghagent.fleet_checks import _copy_manifests, run_repository_checks
from ghagent.interfaces import Config


@unittest.skipUnless(sandbox_available(), 'isolated checks require bubblewrap')
class RepositoryCheckTests(unittest.TestCase):
    def check_project(self, files, command, timeout=120):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            result = run_repository_checks(Config(run_command_timeout_seconds=timeout), root, [command])
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0]['passed'], result)
            for name in ('Cargo.lock', 'go.sum', 'go.work.sum'):
                if name not in files:
                    self.assertFalse((root / name).exists(), name)

    def test_python_nested_requirements_and_read_only_git(self):
        self.check_project({'requirements.txt': '-r requirements/base.txt\n',
                            'requirements/base.txt': '# No outside packages needed.\n',
                            '.git/config': '[core]\n bare = false\n',
                            'tests/test_case.py': 'import unittest\nclass Test(unittest.TestCase):\n def test_value(self): self.assertEqual(2+2,4)\n'},
                           'python3 -m unittest discover -s tests')

    @unittest.skipUnless(shutil.which('npm'), 'npm is required')
    def test_npm_workspace_and_saved_install_then_test(self):
        self.check_project({'package.json': json.dumps({'name': 'fixture-root', 'private': True,
                'workspaces': ['packages/*'], 'scripts': {'test': 'node test.cjs'}}),
            'packages/math/package.json': json.dumps({'name': 'fixture-math', 'version': '1.0.0', 'main': 'index.cjs'}),
            'packages/math/index.cjs': 'module.exports = 4;\n',
            'test.cjs': 'require("node:assert").strictEqual(require("fixture-math"),4);\n'},
            "bash -c 'npm install --no-audit --no-fund && npm test'", timeout=300)

    @unittest.skipUnless(shutil.which('cargo'), 'cargo is required')
    def test_rust_workspace_path_dependency_and_ephemeral_lock(self):
        self.check_project({'Cargo.toml': '[workspace]\nmembers=["library"]\nresolver="2"\n',
            'library/Cargo.toml': '[package]\nname="fixture-library"\nversion="0.1.0"\nedition="2021"\n',
            'library/src/lib.rs': '#[test]\nfn works(){assert_eq!(2+2,4);}\n'}, 'cargo test', timeout=300)

    @unittest.skipUnless(shutil.which('go'), 'Go is required')
    def test_go_local_module_replacement(self):
        self.check_project({'go.mod': 'module fixture.test/root\n\ngo 1.23\nrequire fixture.test/local v0.0.0\nreplace fixture.test/local => ./local\n',
            'local/go.mod': 'module fixture.test/local\n\ngo 1.23\n',
            'local/value.go': 'package local\nfunc Value() int{return 4}\n',
            'value_test.go': 'package root\nimport("testing";"fixture.test/local")\nfunc TestValue(t *testing.T){if local.Value()!=4{t.Fatal("wrong value")}}\n'}, 'go test ./...')

    def test_manifest_includes_are_bounded_and_cannot_copy_source_or_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, stage = Path(tmp) / 'root', Path(tmp) / 'stage'
            root.mkdir(); stage.mkdir()
            (root / 'requirements.txt').write_text('-r nested.txt\n')
            (root / 'nested.txt').write_text('-r requirements.txt\n')
            (root / 'source.py').write_text('private_source = True\n')
            _copy_manifests(root, stage)
            self.assertTrue((stage / 'nested.txt').is_file())
            self.assertFalse((stage / 'source.py').exists())
            (root / 'nested.txt').unlink(); (root / 'nested.txt').symlink_to(root / 'source.py')
            with self.assertRaisesRegex(ValueError, 'symbolic links'):
                _copy_manifests(root, stage)
