"""Jina web search + reader plugin — bundled layout, auto-loaded."""
from __future__ import annotations

from plugins.web.jina.provider import JinaWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(JinaWebSearchProvider())
