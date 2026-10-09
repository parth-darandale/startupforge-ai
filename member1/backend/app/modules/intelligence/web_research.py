"""Discover public web URLs and fetch publication text; snippets are never evidence.

Diagnostics belong to a request batch, never mutable provider-global state.
Explicit source URLs skip discovery, useful when keyless search is unavailable.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import IdeaSpec, ResearchDocument, ResearchOptions
from .business_evidence import filter_business_documents
from .errors import IntelligenceError
from .providers import _canonical_source_url, _queries
from .settings import Settings
from .web_fetch import PublicPageFetcher


@dataclass
class ResearchBatch:
    documents: list[ResearchDocument] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def _requested_url(value: Any) -> str | None:
    """Normalize host/fragments without changing a path's resource identity."""
    validated = _canonical_source_url(value)
    if not validated:
        return None
    original = urlsplit(value)
    normalized = urlsplit(validated)
    return urlunsplit((normalized.scheme, normalized.netloc, original.path or "/", original.query, ""))


class WebResearchProvider:
    name = "web"
    max_candidates = 16

    def __init__(self, settings: Settings, *, discovery: Any = None, fetcher: Any = None):
        timeout = settings.web_request_timeout
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 1 <= timeout <= 60):
            raise IntelligenceError("PROVIDER_CONFIGURATION", "WEB_REQUEST_TIMEOUT must be between 1 and 60 seconds.", 503)
        if settings.web_discovery_provider not in {"bing_news", "ddgs"}:
            raise IntelligenceError("PROVIDER_CONFIGURATION", "WEB_DISCOVERY_PROVIDER must be bing_news or ddgs.", 503)
        engines = [engine.strip() for engine in settings.web_search_engines.split(",")]
        # Validate against the installed version, preventing DDGS's implicit auto
        # fallback when a typo/disabled engine leaves no explicit backends.
        from ddgs.engines import ENGINES
        allowed = set(ENGINES["text"]) - {"wikipedia", "grokipedia"}
        if settings.web_discovery_provider == "ddgs" and (not engines or any(engine not in allowed for engine in engines)):
            raise IntelligenceError("PROVIDER_CONFIGURATION", "WEB_SEARCH_ENGINES must name enabled DDGS web engines; auto/encyclopedia backends are excluded.", 503)
        if not re.fullmatch(r"[a-z]{2}-[a-z]{2}", settings.web_search_region):
            raise IntelligenceError("PROVIDER_CONFIGURATION", "WEB_SEARCH_REGION must use a DDGS locale such as in-en.", 503)
        self.settings = settings
        self.engines = ",".join(dict.fromkeys(engines))
        self.fetcher = fetcher or PublicPageFetcher(timeout=timeout)
        self.discovery = discovery
        if self.discovery is None and settings.web_discovery_provider == "bing_news":
            from .news_discovery import BingNewsDiscovery
            self.discovery = BingNewsDiscovery(self.fetcher, settings.web_search_region)
        self._discovery_lock = asyncio.Lock()

    def _discover_sync(self, query: str) -> list[dict]:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
        try:
            return DDGS(timeout=math.ceil(self.settings.web_request_timeout), verify=True).text(
                query, backend=self.engines, region=self.settings.web_search_region,
                safesearch="moderate", max_results=6,
            )
        except RatelimitException:
            raise IntelligenceError("SEARCH_RATE_LIMIT", "Keyless web search rate limit reached.", retryable=True) from None
        except TimeoutException:
            raise IntelligenceError("SEARCH_TIMEOUT", "Keyless web search timed out.", retryable=True) from None
        except DDGSException:
            # DDGS also conflates empty/challenged/changed markup with no results.
            # Do not conclude that no relevant publications exist.
            raise IntelligenceError("SEARCH_UNAVAILABLE", "Keyless web search returned no usable results or was unavailable. Enter public report URLs to fetch sources directly.", retryable=True) from None

    async def _discover(self, query: str, options: ResearchOptions) -> list[dict]:
        if self.discovery is not None:
            return await self.discovery(query, options)
        async with self._discovery_lock:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._discover_sync, query),
                    timeout=min(90, self.settings.web_request_timeout * len(self.engines.split(",")) + 5),
                )
            except asyncio.TimeoutError:
                raise IntelligenceError("SEARCH_TIMEOUT", "Keyless web search exceeded its discovery budget.", retryable=True) from None

    async def search(self, queries: list[str], options: ResearchOptions) -> list[ResearchDocument]:
        return (await self.search_with_diagnostics(queries, options)).documents

    async def search_with_diagnostics(self, queries: list[str], options: ResearchOptions) -> ResearchBatch:
        return await self._search_with_diagnostics(queries, options)

    async def search_for_idea(self, queries: list[str], options: ResearchOptions, idea: IdeaSpec) -> ResearchBatch:
        """Reject off-topic pages before they consume the retained source budget."""
        return await self._search_with_diagnostics(queries, options, idea)

    async def _search_with_diagnostics(self, queries: list[str], options: ResearchOptions,
                                       idea: IdeaSpec | None = None) -> ResearchBatch:
        batch = ResearchBatch()
        candidates: dict[str, str] = {}
        if options.source_urls:
            for raw_url in options.source_urls:
                url = _requested_url(raw_url)
                if url:
                    candidates.setdefault(url, "")
            batch.gaps.append("Only supplied source URLs were fetched; broader market, competitor and counterevidence discovery was not performed.")
            if len(candidates) > options.max_sources:
                batch.warnings.append("The supplied URL count exceeds max_sources; only the first requested number of usable sources can be retained.")
        else:
            if self.settings.web_discovery_provider == "bing_news":
                batch.gaps.append("Automatic discovery searched public news RSS results only, not the whole web. Evergreen reports, competitor product/pricing pages and customer studies may be missing; supply public report URLs for those sources.")
            query_results: list[list[dict]] = []
            for index, query in enumerate(_queries(queries, options)[:4], start=1):
                try:
                    rows = await self._discover(query, options)
                    if not isinstance(rows, list):
                        raise IntelligenceError("SEARCH_INVALID_RESPONSE", "Web search returned an invalid result list.")
                    query_results.append(rows[:8])
                    if not rows:
                        batch.gaps.append(f"Web query {index} returned no usable publisher URLs in the configured discovery index; this does not show that relevant publications do not exist.")
                except IntelligenceError as error:
                    batch.warnings.append(f"Web query {index}: {error.code}. {error.message}")
                    batch.gaps.append(f"Web query {index} did not produce usable discovery results; coverage is incomplete.")
                    # Stop on a discovery failure, rather than retrying other
                    # queries against a potentially denied or limited service.
                    break
            # Round robin preserves chances for statistics, alternatives and
            # counterevidence before one query fills the candidate budget.
            for rank in range(8):
                for rows in query_results:
                    row = rows[rank] if len(rows) > rank else None
                    if not isinstance(row, dict):
                        continue
                    url = _requested_url(row.get("href"))
                    title = row.get("title")
                    if url and isinstance(title, str):
                        candidates.setdefault(url, title[:500])
            if not candidates:
                batch.gaps.append("No full source pages could be discovered. Supply report URLs or configure an accessible search engine; market statistics remain unknown.")
        seen: set[str] = set()
        blocked_hosts: set[str] = set()
        for url, title in list(candidates.items())[:self.max_candidates]:
            if len(batch.documents) >= options.max_sources:
                break
            host = urlsplit(url).hostname or "source"
            if host in blocked_hosts:
                continue
            try:
                document = await self.fetcher.fetch(url, title_hint=title)
                if document.source.url in seen:
                    continue
                seen.add(document.source.url)
                if idea is not None:
                    relevant, warnings, gaps = filter_business_documents([document], idea, options)
                    batch.warnings.extend(warnings)
                    batch.gaps.extend(gaps)
                    if not relevant:
                        continue
                batch.documents.append(document)
                if "[Partial source:" in document.content or "[Partial report:" in document.content:
                    batch.warnings.append(f"Source {document.source.source_id} was partially extracted; inspect the original report for full coverage.")
            except IntelligenceError as error:
                # Show a host and typed reason, never a URL query or raw HTTP
                # exception that may carry tokens or credentials.
                if error.code in {"PAGE_ACCESS_DENIED", "PAGE_ROBOTS_DENIED", "PAGE_ACCESS_CHALLENGE", "PAGE_RATE_LIMIT"}:
                    blocked_hosts.add(host)
                batch.warnings.append(f"Skipped {host}: {error.code}. {error.message}")
                batch.gaps.append(f"A candidate publication from {host} could not be fetched; it contributes no evidence or figures.")
        if len(batch.documents) < options.max_sources:
            batch.gaps.append(f"Retained {len(batch.documents)} of up to {options.max_sources} full-page sources; this bounded crawl is not exhaustive market research.")
        if options.language != "en":
            batch.gaps.append("Requested language coverage is not guaranteed by the configured search engine; fetched publications are not translated.")
        batch.warnings = list(dict.fromkeys(batch.warnings))
        batch.gaps = list(dict.fromkeys(batch.gaps))
        return batch
