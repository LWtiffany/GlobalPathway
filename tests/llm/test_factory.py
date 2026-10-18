import unittest

from agent2.llm.factory import build_llm_components


class FactoryTests(unittest.TestCase):
    def test_disabled_has_no_llm_ports(self):
        result = build_llm_components({"AGENT2_LLM_ENABLED": "0"})
        self.assertIsNone(result.understanding)
        self.assertIsNone(result.writer)

    def test_enabled_requires_key(self):
        with self.assertRaisesRegex(RuntimeError, "API_KEY"):
            build_llm_components({"AGENT2_LLM_ENABLED": "1",
                                  "AGENT2_LLM_BASE_URL": "https://example.com/v1"})

    def test_builds_aliyun_compatible_ports(self):
        result = build_llm_components({
            "AGENT2_LLM_ENABLED": "1",
            "AGENT2_LLM_API_KEY": "test-key",
            "AGENT2_LLM_BASE_URL": "https://example.com/v1",
            "AGENT2_LLM_MODEL": "deepseek-v4.1-flash",
        })
        self.assertEqual("deepseek-v4.1-flash", result.understanding.model_version)
        self.assertFalse(result.understanding.model.extra_body["enable_thinking"])
        self.assertEqual("json_object", result.understanding.model.structured_mode)

    def test_default_uses_qwen_server_side_json_schema(self):
        result = build_llm_components({
            "AGENT2_LLM_ENABLED": "1",
            "AGENT2_LLM_API_KEY": "test-key",
            "AGENT2_LLM_BASE_URL": "https://example.com/v1",
        })
        self.assertEqual("qwen3.8-flash", result.understanding.model_version)
        self.assertEqual("json_schema", result.understanding.model.structured_mode)
        self.assertFalse(result.understanding.model.extra_body["enable_thinking"])
