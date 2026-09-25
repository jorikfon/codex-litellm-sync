import unittest

from codex_litellm_sync import build_catalog, with_catalog_key

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

    def test_config_key(self):
        cfg = 'model = "x"\n\n[features]\nfoo = true\n'
        out = with_catalog_key(cfg, "/h/cat.json")
        self.assertEqual(out, 'model = "x"\n\nmodel_catalog_json = "/h/cat.json"\n[features]\nfoo = true\n')
        self.assertIsNone(with_catalog_key(out, "/other.json"))
        self.assertEqual(with_catalog_key("", "/c.json"), 'model_catalog_json = "/c.json"\n')


if __name__ == "__main__":
    unittest.main()
