import tempfile
import unittest
from pathlib import Path

from jinja2 import UndefinedError

from agent2.llm.prompts import PromptRenderer


class PromptRendererTests(unittest.TestCase):
    def test_renders_external_template(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "sample.jinja2").write_text("Hello {{ name }}", encoding="utf-8")
            self.assertEqual("Hello Agent2", PromptRenderer(Path(folder)).render(
                "sample.jinja2", name="Agent2"))

    def test_missing_variable_fails_fast(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "sample.jinja2").write_text("{{ required }}", encoding="utf-8")
            with self.assertRaises(UndefinedError):
                PromptRenderer(Path(folder)).render("sample.jinja2")
