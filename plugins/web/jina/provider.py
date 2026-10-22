"""Jina AI web search + page reader (``s.jina.ai`` / ``r.jina.ai``).

Config: ``web.backend`` / ``web.search_backend`` / ``web.extract_backend: jina``.
Env: ``JINA_API_KEY`` (https://jina.ai/reader/), optional ``JINA_BASE_URL`` for a proxy.
Keyed requests use ``Authorization: Bearer``. Jina has no keyless tier, so without a key
``is_available()`` is False and ``hermes tools`` offers to set it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from plugins.web._common import (
    SEARCH_LIMIT_CAP, BaseWebSearchProvider, document, http_status_detail,
    provider_env, run_extract, run_search, search_fail, search_ok, setup_schema, title_hit,
)

logger = logging.getLogger(__name__)

_SEARCH_BASE = "https://s.jina.ai"
_READER_BASE = "https://r.jina.ai"
_TIMEOUT = 60

# Jina renders search results as a markdown list of links, optionally followed by a
# description line. Only the link shape is structural; the description is best-effort.
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _api_key() -> str:
    return provider_env("JINA_API_KEY")


def _base(override_env: str, default: str) -> str:
    return (provider_env(override_env) or default).rstrip("/")


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Accept": "text/plain", "User-Agent": "hermes-agent"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _missing_key_error() -> str:
    return (
        "JINA_API_KEY is not set. s.jina.ai search needs a key (https://jina.ai/reader/); "
        "only r.jina.ai extraction works keyless."
    )


def _request(url: str, api_key: str) -> str:
    response = httpx.get(url, timeout=_TIMEOUT, headers=_headers(api_key), follow_redirects=True)
    if response.status_code >= 400:
        raise ValueError(http_status_detail(response))
    return response.text or ""


def _is_credit_error(message: str) -> bool:
    """True when Jina rejected the credential itself (401/402), so a keyless retry makes sense."""
    lowered = message.lower()
    return "402" in lowered or "401" in lowered or "insufficientbalance" in lowered or "authenticationrequired" in lowered


def _parse_search_hits(markdown: str, limit: int) -> list[dict[str, Any]]:
    """Turn Jina's markdown link list into normalized hits, de-duplicated by URL."""
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        match = _LINK_RE.search(line)
        if not match:
            continue
        title, url = match.group(1).strip(), match.group(2).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        description = ""
        for follow in lines[i + 1 : i + 3]:
            text = follow.strip()
            if not text or _LINK_RE.search(text) or text.startswith("#"):
                break
            description = text
            break
        hits.append(title_hit(title or url, url, description, len(hits) + 1))
        if len(hits) >= limit:
            break
    return hits


class JinaWebSearchProvider(BaseWebSearchProvider):
    """Jina search (``s.jina.ai``) + reader (``r.jina.ai``) provider."""

    NAME = "jina"
    DISPLAY_NAME = "Jina"
    KEY_ENV = "JINA_API_KEY"
    EXTRACT = True
    KEYLESS = False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        def _body() -> dict[str, Any]:
            api_key = _api_key()
            if not api_key:
                return search_fail(_missing_key_error())
            safe_limit = max(1, min(limit, SEARCH_LIMIT_CAP))
            logger.info("Jina search: %r (limit=%d)", query, safe_limit)
            markdown = _request(
                f"{_base('JINA_SEARCH_BASE_URL', _SEARCH_BASE)}/{quote(query)}", api_key
            )
            hits = _parse_search_hits(markdown, safe_limit)
            if not hits:
                return search_fail("Jina returned no parseable results for this query.")
            return search_ok(hits)

        return run_search("Jina", logger, _body)

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        def _body() -> list[dict[str, Any]]:
            # r.jina.ai is keyless-capable (verified: 200, ~6 s). A key that has run out of
            # credit returns 402 on the SAME endpoint, so keyed-first-then-keyless is the
            # only order that works for both funded and empty accounts.
            api_key = _api_key()
            base = _base("JINA_READER_BASE_URL", _READER_BASE)
            documents: list[dict[str, Any]] = []
            for url in urls:
                content = ""
                last_error = ""
                for candidate_key in ([api_key] if api_key else []) + [""]:
                    try:
                        content = _request(f"{base}/{url}", candidate_key)
                        if candidate_key:
                            logger.info("Jina extract keyed OK: %s", url)
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        if not _is_credit_error(last_error):
                            break  # a real page error: do not retry keyless
                        logger.warning("Jina keyed extract rejected (%s), retrying keyless", url)
                if content:
                    title = ""
                    for line in content.splitlines():
                        if line.startswith("Title:"):
                            title = line[6:].strip()
                            break
                    documents.append(document(url, title, content))
                else:
                    logger.warning("Jina extract failed for %s: %s", url, last_error)
                    documents.append(document(url, "", "", source_url=url) | {"error": last_error})
            return documents

        return run_extract("Jina", logger, urls, _body)

    def get_setup_schema(self) -> dict[str, Any]:
        return setup_schema(
            "Jina", "extract free · search needs key", "Search + page reader via Jina AI.",
            "JINA_API_KEY", "Jina API key (optional — page extraction works keyless)", "https://jina.ai/reader/",
        )
