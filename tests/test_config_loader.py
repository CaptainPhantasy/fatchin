"""Unit tests for config loading, env-file parsing (values never leak into
errors), and environment validation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ghagent import config_loader
from ghagent.config_loader import (
    ConfigError,
    load_config,
    load_env_file,
    model_auth_problems,
    validate_environment,
    write_example_config,
)
from ghagent.interfaces import Config, ModelConfig, RepoConfig


def write_file(tmp, name, text):
    path = Path(tmp) / name
    path.write_text(text, encoding="utf-8")
    return path


def toml_text(tmp, *, state=True, worktree=True, extra=""):
    lines = ['poll_interval_seconds = 30', 'allowed_command_users = [7]']
    if state:
        lines.append(f'state_dir = "{tmp}/state"')
    if worktree:
        lines.append(f'worktree_root = "{tmp}/wt"')
    if extra:
        lines.append(extra)
    lines += ["[[repos]]", 'owner = "o"', 'name = "r"',
              "[model]", 'auth_mode = "broker-lease"']
    return "\n".join(lines) + "\n"


def good_cfg(**over):
    values = dict(
        state_dir="/members/floyd/state",
        worktree_root="/members/floyd/wt",
        allowed_command_users=[7],
        repos=[RepoConfig(owner="o", name="r")],
    )
    values.update(over)
    return Config(**values)


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_load_config_ok(self):
        path = write_file(self.tmp, "config.toml", toml_text(self.tmp))
        cfg = load_config(path)
        self.assertEqual(cfg.repos[0].full_name, "o/r")
        self.assertEqual(cfg.model.auth_mode, "broker-lease")
        self.assertEqual(cfg.allowed_command_users, [7])

    def test_missing_state_dir_is_named(self):
        path = write_file(self.tmp, "config.toml",
                          toml_text(self.tmp, state=False))
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("state_dir", str(ctx.exception))
        self.assertNotIn("worktree_root is missing", str(ctx.exception))
        self.assertTrue(ctx.exception.missing_destination)

    def test_missing_destination_keys_both_named(self):
        path = write_file(self.tmp, "config.toml",
                          toml_text(self.tmp, state=False, worktree=False))
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("state_dir", str(ctx.exception))
        self.assertIn("worktree_root", str(ctx.exception))
        self.assertTrue(ctx.exception.missing_destination)

    def test_empty_state_dir_value_is_missing(self):
        text = toml_text(self.tmp).replace(
            f'state_dir = "{self.tmp}/state"', 'state_dir = ""')
        path = write_file(self.tmp, "config.toml", text)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertTrue(ctx.exception.missing_destination)
        self.assertIn("state_dir", str(ctx.exception))

    def test_unknown_top_level_field_friendly_error(self):
        path = write_file(self.tmp, "config.toml",
                          toml_text(self.tmp, extra='mystery_key = 1'))
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("does not recognize", str(ctx.exception))
        self.assertIn("mystery_key", str(ctx.exception))

    def test_unknown_repo_field_friendly_error(self):
        text = toml_text(self.tmp) + 'oops = true\n'
        path = write_file(self.tmp, "config.toml", text)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("does not recognize", str(ctx.exception))

    def test_invalid_toml_friendly_error(self):
        path = write_file(self.tmp, "config.toml", "not [valid toml [[[")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("not valid TOML", str(ctx.exception))

    def test_missing_file_message_suggests_init_config(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.tmp / "absent.toml")
        self.assertIn("init-config", str(ctx.exception))
        self.assertFalse(ctx.exception.missing_destination)


class LoadEnvFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_parses_quotes_comments_and_spacing(self):
        path = write_file(self.tmp, "env", "\n".join([
            "# full-line comment stays out",
            "PLAIN=1",
            'QUOTED="two words"',
            "SINGLE='single quoted'",
            "SPACED =   padded value  ",
            'HASH_IN_VALUE="a#b"',
            "",
        ]) + "\n")
        values = load_env_file(path)
        self.assertEqual(values, {
            "PLAIN": "1",
            "QUOTED": "two words",
            "SINGLE": "single quoted",
            "SPACED": "padded value",
            "HASH_IN_VALUE": "a#b",
        })

    def test_error_messages_never_contain_values(self):
        secret = "super-secret-broker-token-value"
        path = write_file(self.tmp, "env", f"BROKEN LINE NO EQUALS {secret}\n")
        with self.assertRaises(ConfigError) as ctx:
            load_env_file(path)
        message = str(ctx.exception)
        self.assertNotIn(secret, message)
        self.assertIn("line 1", message)

    def test_bad_key_error_names_line_only(self):
        path = write_file(self.tmp, "env", "1BAD=x\n")
        with self.assertRaises(ConfigError) as ctx:
            load_env_file(path)
        self.assertIn("line 1", str(ctx.exception))
        self.assertIn("invalid variable name", str(ctx.exception))

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(load_env_file(self.tmp / "absent"), {})


class ValidateEnvironmentTest(unittest.TestCase):
    def test_fully_valid_environment(self):
        env = {"MCP_SECRET_BROKER_ADMIN_TOKEN": "x"}
        self.assertEqual(validate_environment(good_cfg(), env), [])

    def test_each_problem_is_named(self):
        cases = [
            (good_cfg(state_dir=""), {},
             "state_dir"),
            (good_cfg(worktree_root=""), {},
             "worktree_root"),
            (good_cfg(allowed_command_users=[]), {},
             "allowed_command_users"),
            (good_cfg(repos=[]), {},
             "repos"),
            (good_cfg(), {},
             "MCP_SECRET_BROKER_ADMIN_TOKEN"),
            (good_cfg(model=ModelConfig(auth_mode="env")), {},
             "GHA_MODEL_API_KEY"),
            (good_cfg(model=ModelConfig(auth_mode="magic")), {},
             "auth_mode"),
        ]
        for cfg, env, expected in cases:
            with self.subTest(expected=expected):
                problems = validate_environment(cfg, env)
                self.assertTrue(problems)
                self.assertTrue(
                    any(expected in p for p in problems),
                    f"no problem mentions {expected!r}: {problems}")

    def test_model_auth_problems_are_the_auth_subset_only(self):
        env = {"MCP_SECRET_BROKER_ADMIN_TOKEN": "x"}
        broken_config = good_cfg(repos=[])  # config problem, not an auth one
        self.assertEqual(model_auth_problems(broken_config, env), [])
        self.assertTrue(validate_environment(broken_config, env))


class WriteExampleConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_example_loads_and_refuses_overwrite(self):
        dest = self.tmp / "config.example.toml"
        path = write_example_config(dest)
        self.assertEqual(path, dest)
        cfg = load_config(dest)  # placeholder values must still load
        self.assertEqual(cfg.model.model_id, "your-model")
        self.assertEqual(cfg.repos[0].owner, "example-owner")
        with self.assertRaises(ConfigError) as ctx:
            write_example_config(dest)
        self.assertIn("refusing to overwrite", str(ctx.exception))

    def test_example_matches_deployed_example_file(self):
        repo_root = Path(__file__).resolve().parents[1]
        deployed = repo_root / "deploy" / "config.example.toml"
        if deployed.exists():
            self.assertEqual(
                deployed.read_text(encoding="utf-8"),
                config_loader.EXAMPLE_CONFIG_TOML,
                "deploy/config.example.toml drifted from the constant",
            )


if __name__ == "__main__":
    unittest.main()
