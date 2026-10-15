import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_litellm_sync import (build_catalog, interactive_models, key_models, main,
                               fetch_key_models, parse_selection, persist_macos_env,
                               resolve_key, select_models, with_catalog_key)

GROUPS = [
    {"model_group": "gpt-6-luna", "mode": "chat", "max_input_tokens": 922000.0,
     "supports_vision": True, "supported_reasoning_efforts": ["none", "low", "medium", "high"]},
    {"model_group": "glm-5.3", "mode": "chat", "supported_reasoning_efforts": ["low", "high", "max"]},
    {"model_group": "qwen", "mode": None},
    {"model_group": "bge-m3", "mode": "embedding"},
]
BUNDLED = [
    {"slug": "gpt-hidden", "visibility": "hide", "base_instructions": "H"},
    {"slug": "gpt-6-luna", "visibility": "list", "base_instructions": "P",
     "model_messages": {"instructions_template": "T"}, "context_window": 1},
]


class SyncTest(unittest.TestCase):
    def test_catalog(self):
        models = {m["slug"]: m for m in build_catalog(GROUPS, BUNDLED)["models"]}
        self.assertEqual(list(models), ["gpt-6-luna", "glm-5.3", "qwen"])
        # a slug Codex ships keeps its bundled entry
        self.assertEqual(models["gpt-6-luna"]["context_window"], 1)
        self.assertEqual(models["gpt-6-luna"]["priority"], 0)
        # others get the first listed bundled model's prompt
        self.assertEqual(models["glm-5.3"]["base_instructions"], "P")
        self.assertEqual(models["glm-5.3"]["model_messages"], {"instructions_template": "T"})
        self.assertEqual(models["glm-5.3"]["default_reasoning_level"], "high")
        self.assertEqual(models["qwen"]["context_window"], 128000)
        self.assertEqual(models["qwen"]["priority"], 2)
        self.assertNotIn("default_reasoning_level", models["qwen"])

    def test_key_scope_and_no_reasoning(self):
        groups = GROUPS + [
            {"model_group": "deepseek-no-reasoning", "mode": "chat", "supports_reasoning": True,
             "supported_reasoning_efforts": ["none", "low", "high", "max"]},
            {"model_group": "flagged-off", "mode": "chat", "supports_reasoning": False,
             "supported_reasoning_efforts": ["low"]},
        ]
        allowed = key_models([
            {"model_name": "glm-5.3", "litellm_params": {}},
            {"model_name": "deepseek-no-reasoning", "litellm_params": {"reasoning_effort": "none"}},
            {"model_name": "flagged-off"},
            {"model_name": "glm-5.3", "litellm_params": {"reasoning_effort": "none"}},  # not all deployments
        ])
        models = {m["slug"]: m for m in build_catalog(groups, BUNDLED, allowed=allowed)["models"]}
        self.assertEqual(sorted(models), ["deepseek-no-reasoning", "flagged-off", "glm-5.3"])
        for slug in ("deepseek-no-reasoning", "flagged-off"):
            self.assertEqual(models[slug]["supported_reasoning_levels"], [], slug)
            self.assertNotIn("default_reasoning_level", models[slug])
        self.assertEqual(len(models["glm-5.3"]["supported_reasoning_levels"]), 3)
        # /model/info unavailable -> no filter, as before
        self.assertIn("qwen", {m["slug"] for m in build_catalog(groups, BUNDLED)["models"]})

    def test_config_key(self):
        cfg = 'model = "x"\n\n[features]\nfoo = true\n'
        out = with_catalog_key(cfg, "/h/cat.json")
        self.assertEqual(out, 'model = "x"\n\nmodel_catalog_json = "/h/cat.json"\n[features]\nfoo = true\n')
        self.assertIsNone(with_catalog_key(out, "/h/cat.json"))
        self.assertIn('model_catalog_json = "/other.json"\n', with_catalog_key(out, "/other.json"))
        self.assertEqual(with_catalog_key("", "/c.json"), 'model_catalog_json = "/c.json"\n')


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.models = build_catalog(GROUPS, BUNDLED)["models"]

    def test_selection_order_priorities_and_validation(self):
        selected = select_models(self.models, ["qwen", "glm-5.3", "qwen"])
        self.assertEqual([m["slug"] for m in selected], ["glm-5.3", "qwen"])
        self.assertEqual([m["priority"] for m in selected], [0, 1])
        self.assertEqual(self.models[1]["priority"], 1)
        for names in ([], ["missing"], ["bge-m3"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                select_models(self.models, names)

    def test_numbers_ranges_and_invalid_input(self):
        self.assertEqual(parse_selection("1, 3-5 3", 5), {0, 2, 3, 4})
        self.assertEqual(parse_selection(" ALL ", 3), {0, 1, 2})
        for text in ("", "0", "4", "3-1", "1-4", "-1", "one", "1-2-3", "all 1"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_selection(text, 3)

    def test_interactive_defaults_and_retry(self):
        with patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(interactive_models(self.models), self.models)
            selected = interactive_models(self.models, ["qwen", "removed"])
        self.assertEqual([m["slug"] for m in selected], ["qwen"])
        with patch("builtins.input", side_effect=["", "0", "2-3"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            selected = interactive_models(self.models, ["removed"])
        self.assertEqual([m["slug"] for m in selected], ["glm-5.3", "qwen"])

    def test_cancel_and_empty_choices(self):
        for exc in (EOFError, KeyboardInterrupt):
            with patch("builtins.input", side_effect=exc), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "cancelled"):
                interactive_models(self.models)
        with self.assertRaisesRegex(ValueError, "no available"):
            interactive_models([])

    def run_cli(self, args, allowed=None):
        with patch("sys.argv", ["sync"] + args), \
                patch.dict("os.environ", {"LITELLM_API_KEY": "test-placeholder"}), \
                patch("codex_litellm_sync.fetch_groups", return_value=GROUPS), \
                patch("codex_litellm_sync.bundled_models", return_value=BUNDLED), \
                patch("codex_litellm_sync.fetch_key_models", return_value=allowed), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main()

    def test_cli_preview_write_and_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            out, config = Path(temp) / "models.json", Path(temp) / "config.toml"
            args = ["--out", str(out), "--config", str(config), "--models", "glm-5.3"]
            self.run_cli(args, {"glm-5.3": False})
            self.assertFalse(out.exists())
            self.assertFalse(config.exists())
            self.run_cli(args + ["--write"], {"glm-5.3": False})
            models = json.loads(out.read_text())["models"]
            self.assertEqual([m["slug"] for m in models], ["glm-5.3"])
            self.assertEqual(models[0]["priority"], 0)
            original = out.read_text()
            for allowed in (None, {"qwen": False}):
                with self.assertRaises(SystemExit) as error:
                    self.run_cli(args + ["--write"], allowed)
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(out.read_text(), original)
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=""):
                self.run_cli(["--out", str(out), "--config", str(config), "--select", "--write"],
                             {"glm-5.3": False, "qwen": False})
            self.assertEqual(out.read_text(), original)

    def test_cli_requires_terminal(self):
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(SystemExit) as error:
            self.run_cli(["--select"])
        self.assertEqual(error.exception.code, 2)


class EnvironmentTest(unittest.TestCase):
    def test_missing_env_loads_keychain_without_secret_arguments(self):
        with patch.dict("os.environ", {}, clear=True), patch("sys.platform", "darwin"), \
                patch("subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()):
            run.return_value.returncode = 0
            run.return_value.stdout = "test-placeholder\n"
            self.assertEqual(resolve_key("LITELLM_API_KEY"), ("test-placeholder", True))
            self.assertNotIn("test-placeholder", repr(run.call_args))
            self.assertEqual(resolve_key("LITELLM_API_KEY"), ("test-placeholder", False))
            self.assertEqual(run.call_count, 1)

    def test_loaders_are_idempotent_and_do_not_contain_secret(self):
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
                patch.dict("os.environ", {"SHELL": "/bin/zsh", "ZDOTDIR": temp}), \
                patch("subprocess.run"), contextlib.redirect_stdout(io.StringIO()):
            persist_macos_env("LITELLM_API_KEY")
            first = (Path(temp) / ".zshrc").read_text()
            persist_macos_env("LITELLM_API_KEY")
            self.assertEqual((Path(temp) / ".zshrc").read_text(), first)
            self.assertIn("System.keychain", first)
            self.assertNotIn("test-placeholder", first)

    def test_windows_path_is_toml_escaped(self):
        self.assertEqual(with_catalog_key("", r"C:\Users\test\models.json"),
                         'model_catalog_json = "C:\\\\Users\\\\test\\\\models.json"\n')

    def test_key_scoped_fallback(self):
        responses = []
        for data in ({"data": []}, {"data": [{"id": "qwen"}]}):
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = io.StringIO(json.dumps(data))
            responses.append(response)
        with patch("urllib.request.urlopen", side_effect=responses) as urlopen, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fetch_key_models("https://example.test", "test-placeholder"), {"qwen": False})
            self.assertEqual(urlopen.call_args.args[0].full_url, "https://example.test/v1/models")


if __name__ == "__main__":
    unittest.main()
