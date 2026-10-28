"""Load versioned Jinja prompts from disk with strict variable checking."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

PROMPT_ROOT = Path(__file__).with_name("prompt_templates")


class PromptRenderer:
    def __init__(self, root: Path = PROMPT_ROOT):
        self.root = Path(root)
        self.environment = Environment(
            loader=FileSystemLoader(str(self.root)),
            undefined=StrictUndefined,
            autoescape=select_autoescape(default=False),
            keep_trailing_newline=True,
        )

    def render(self, template_name: str, **values) -> str:
        return self.environment.get_template(template_name).render(**values).strip()
