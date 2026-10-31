"""JinjaNotificationRenderer — NotificationRendererPort from text templates.

One template per kind and language, ``templates/{lang}/{kind}.txt``, with two
blocks: ``subject`` and ``body``. Missing languages fall back to the first
platform language. Plain text only, so no autoescaping; undefined variables
fail loudly (the dispatcher logs and skips that mail).

Links into UMP-X are built in the templates from ``frontend_base_url``, so
adapting them to the frontend's routes is a template edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from ump.core.interfaces.notification_renderer import (
    NotificationRendererPort,
    RenderedMail,
)

TEMPLATES = Path(__file__).parent / "templates"


class JinjaNotificationRenderer(NotificationRendererPort):
    def __init__(
        self, languages: Sequence[str], template_dir: Path = TEMPLATES
    ) -> None:
        self._default = languages[0] if languages else "en"
        self._env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def render(self, kind: str, lang: str, params: dict[str, Any]) -> RenderedMail:
        try:
            template = self._env.get_template(f"{lang}/{kind}.txt")
        except TemplateNotFound:
            template = self._env.get_template(f"{self._default}/{kind}.txt")
        context = template.new_context(params)
        # a full render runs the top-level {% set %} statements the blocks use
        for _ in template.root_render_func(context):
            pass
        subject = "".join(template.blocks["subject"](context)).strip()
        body = "".join(template.blocks["body"](context)).strip() + "\n"
        return RenderedMail(subject=" ".join(subject.split()), text=body)
