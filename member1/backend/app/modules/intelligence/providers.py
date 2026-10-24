"""Provider adapters. Demo needs no network; live adapters are explicitly configured.

HTTP errors are translated to safe, typed errors: response bodies, headers, URLs,
and exception messages can contain credentials and are never sent to the client.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .contracts import ResearchDocument, ResearchOptions, Source
from .errors import IntelligenceError
from .local_model_lock import local_model_lock
from .settings import BACKEND_ROOT, Settings

MAX_QUERIES = 6
MAX_CONTENT_CHARS = 60000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OLLAMA_CONTEXT_SAFETY_TOKENS = 128


class LanguageModel(Protocol):
    name: str

    async def generate_json(self, task: str, payload: dict, schema: dict) -> dict: ...


class SearchProvider(Protocol):
    name: str

    async def search(
        self, queries: list[str], options: ResearchOptions
    ) -> list[ResearchDocument]: ...


def _config_error(message: str) -> IntelligenceError:
    return IntelligenceError("PROVIDER_CONFIGURATION", message, status_code=503)


def _invalid_response(provider: str) -> IntelligenceError:
    return IntelligenceError(
        "PROVIDER_INVALID_RESPONSE", f"{provider} returned an invalid response."
    )


def _provider_url(base_url: str, suffix: str) -> str:
    """Validate configured endpoints; allow localhost for locally hosted models."""
    try:
        parsed = urlsplit(base_url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise _config_error("The provider base URL must be an HTTP(S) URL without credentials or query parameters.")
    return base_url.rstrip("/") + suffix


async def _request_json(
    client: httpx.AsyncClient, provider: str, method: str, url: str, **kwargs: Any
) -> dict:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException:
        raise IntelligenceError(
            "PROVIDER_TIMEOUT", f"{provider} timed out. Try again later.", retryable=True
        ) from None
    except httpx.RequestError:
        raise IntelligenceError(
            "PROVIDER_UNAVAILABLE", f"Cannot connect to {provider}.", retryable=True
        ) from None
    status = response.status_code
    if status == 401:
        raise IntelligenceError(
            "PROVIDER_AUTHENTICATION", f"{provider} rejected the configured credentials."
        )
    if status == 403:
        raise IntelligenceError(
            "PROVIDER_ACCESS_DENIED",
            f"{provider} denied the request. Check your network and provider access.",
        )
    if status in {429, 432, 433}:
        raise IntelligenceError(
            "PROVIDER_RATE_LIMIT", f"{provider} rate or usage limit reached.", retryable=True
        )
    if status >= 500:
        raise IntelligenceError(
            "PROVIDER_UNAVAILABLE", f"{provider} is temporarily unavailable.", retryable=True
        )
    if status < 200 or status >= 300:
        raise IntelligenceError(
            "PROVIDER_REQUEST_REJECTED",
            f"{provider} rejected the request. Check the provider model and configuration.",
        )
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise _invalid_response(provider)
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise _invalid_response(provider) from None
    if not isinstance(data, dict):
        raise _invalid_response(provider)
    return data


def _parse_model_content(content: Any, provider: str) -> dict:
    if not isinstance(content, str) or not content.strip():
        raise _invalid_response(provider)
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise IntelligenceError(
            "MODEL_INVALID_JSON", f"{provider} did not produce a valid JSON object."
        ) from None
    if not isinstance(value, dict):
        raise IntelligenceError(
            "MODEL_INVALID_JSON", f"{provider} did not produce a JSON object."
        )
    # The pipeline validates the object against its task-specific Pydantic model.
    return value


def _messages(task: str, payload: dict, schema: dict) -> list[dict[str, str]]:
    from .prompts import build_messages

    return build_messages(task, payload, schema)


def _generation_schema(task: str, schema: dict) -> dict:
    """Strengthen model extraction without changing the public API contract.

    Client-created IdeaSpecs can still use optional collection defaults. During
    extraction, however, an explicit value and provenance entry helps models
    preserve stated constraints instead of silently omitting optional fields.
    """
    prepared = copy.deepcopy(schema)
    origin_fields = ["project_name", "problem", "solution", "target_users",
                     "domain", "assumptions", "constraints"]
    properties = prepared.get("properties")
    expected = {"original_text", "field_origins", *origin_fields}
    if (task != "extract" or prepared.get("title") != "IdeaSpec" or prepared.get("type") != "object"
            or not isinstance(properties, dict) or set(properties) != expected):
        return prepared
    prepared["required"] = list(properties)
    properties["field_origins"] = {
        "type": "object", "title": "Field Origins",
        "properties": {field: {"type": "string", "enum": ["provided", "inferred", "unknown"]}
                       for field in origin_fields},
        "required": origin_fields, "additionalProperties": False,
    }
    return prepared


class OpenAICompatibleLanguageModel:
    name = "openai_compatible"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.llm_api_key.strip():
            raise _config_error("Set LLM_API_KEY before enabling the live OpenAI-compatible provider.")
        if not settings.llm_model.strip():
            raise _config_error("Set LLM_MODEL to the model available in your provider account.")
        self.url = _provider_url(settings.llm_base_url, "/chat/completions")
        self.settings = settings
        self.transport = transport
        self.response_format = getattr(settings, "llm_response_format", "json_schema")
        if self.response_format not in {"json_schema", "json_object"}:
            raise _config_error("LLM_RESPONSE_FORMAT must be json_schema or json_object.")

    async def generate_json(self, task: str, payload: dict, schema: dict) -> dict:
        schema = _generation_schema(task, schema)
        response_format: dict[str, Any] = {"type": "json_object"}
        if self.response_format == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "startupforge_" + re.sub(r"[^a-zA-Z0-9_-]", "_", task)[:40],
                    "schema": schema,
                    # These schemas contain defaults and optional fields. Strict
                    # provider mode requires a more restrictive schema subset.
                    "strict": False,
                },
            }
        body = {
            "model": self.settings.llm_model,
            "messages": _messages(task, payload, schema),
            "response_format": response_format,
        }
        async with httpx.AsyncClient(
            timeout=self.settings.provider_timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            data = await _request_json(
                client, self.name, "POST", self.url,
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"}, json=body,
            )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise IntelligenceError("MODEL_REFUSAL", "The model declined this request.")
            if choice.get("finish_reason") == "length":
                raise IntelligenceError("MODEL_TRUNCATED", "The model output was cut short. Reduce the input size.")
            return _parse_model_content(message["content"], self.name)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise _invalid_response(self.name) from None


class OllamaLanguageModel:
    name = "ollama"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.llm_model.strip():
            raise _config_error("Set LLM_MODEL to a model installed in Ollama.")
        if type(settings.ollama_num_ctx) is not int or not 512 <= settings.ollama_num_ctx <= 131072:
            raise _config_error("OLLAMA_NUM_CTX must be an integer between 512 and 131072.")
        if type(settings.ollama_num_predict) is not int or not 1 <= settings.ollama_num_predict <= 16384:
            raise _config_error("OLLAMA_NUM_PREDICT must be an integer between 1 and 16384.")
        temperature = settings.ollama_temperature
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise _config_error("OLLAMA_TEMPERATURE must be a finite number between 0 and 2.")
        if settings.ollama_think is not None and type(settings.ollama_think) is not bool:
            raise _config_error("OLLAMA_THINK must be default, true or false.")
        ratio = settings.ollama_context_bytes_per_token
        if (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                or not math.isfinite(ratio) or not 0 < ratio <= 4):
            raise _config_error("OLLAMA_CONTEXT_BYTES_PER_TOKEN must be a finite number greater than 0 and at most 4.")
        keep_alive = settings.ollama_keep_alive
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", keep_alive) if isinstance(keep_alive, str) else None
        if match:
            seconds = float(match[1]) * {None: 1, "ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]
        else:
            seconds = -1
        if not math.isfinite(seconds) or not 0 <= seconds <= 3600 or (match and not match[2] and seconds != 0):
            raise _config_error("OLLAMA_KEEP_ALIVE must be 0 or a duration up to 1h, such as 5m.")
        self.url = _provider_url(settings.ollama_base_url, "/api/chat")
        self.settings = settings
        self.transport = transport

    async def generate_json(self, task: str, payload: dict, schema: dict) -> dict:
        schema = _generation_schema(task, schema)
        messages = _messages(task, payload, schema)
        # The local HTTP API does not expose a tokenizer. This configurable
        # byte-based estimate errs toward a smaller usable prompt and leaves
        # room for template tokens plus the full requested generation. It is
        # not an exact token count and does not prove absence of truncation.
        prompt_bytes = sum(len(message["content"].encode("utf-8")) for message in messages)
        token_estimate = prompt_bytes / self.settings.ollama_context_bytes_per_token
        prompt_tokens = math.ceil(token_estimate) if math.isfinite(token_estimate) else None
        if prompt_tokens is None or prompt_tokens + OLLAMA_CONTEXT_SAFETY_TOKENS + self.settings.ollama_num_predict > self.settings.ollama_num_ctx:
            raise IntelligenceError(
                "MODEL_CONTEXT_LIMIT",
                "The idea, research or conversation exceeds this local model's estimated context budget. Increase the supported local context setting or reduce the input; no input was removed.",
                status_code=422,
                details={"task": task, "estimated": True, "prompt_bytes": prompt_bytes,
                         "estimated_prompt_tokens": prompt_tokens,
                         "output_reserved_tokens": self.settings.ollama_num_predict,
                         "safety_tokens": OLLAMA_CONTEXT_SAFETY_TOKENS,
                         "context_tokens": self.settings.ollama_num_ctx,
                         "bytes_per_token": self.settings.ollama_context_bytes_per_token},
            )
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "options": {
                "num_ctx": self.settings.ollama_num_ctx,
                "num_predict": self.settings.ollama_num_predict,
                "temperature": self.settings.ollama_temperature,
            },
            "keep_alive": self.settings.ollama_keep_alive,
        }
        # Older chat models may not support a think toggle. Omit it unless the
        # configuration explicitly selects a value supported by /api/show.
        if self.settings.ollama_think is not None:
            body["think"] = self.settings.ollama_think
        async with local_model_lock():
            async with httpx.AsyncClient(
                timeout=self.settings.provider_timeout, transport=self.transport, follow_redirects=False
            ) as client:
                data = await _request_json(client, self.name, "POST", self.url, json=body)
        if data.get("error"):
            raise IntelligenceError("PROVIDER_REQUEST_REJECTED", "Ollama could not run the configured model.")
        if data.get("done") is False:
            raise IntelligenceError("MODEL_TRUNCATED", "Ollama returned an unfinished response.")
        if data.get("done_reason") == "length":
            raise IntelligenceError(
                "MODEL_TRUNCATED",
                "Ollama reached the generation limit. Increase OLLAMA_NUM_PREDICT or reduce the requested output.",
            )
        prompt_count, output_count = data.get("prompt_eval_count"), data.get("eval_count")
        if (type(prompt_count) is int and type(output_count) is int
                and prompt_count >= 0 and output_count >= 0
                and prompt_count + output_count >= self.settings.ollama_num_ctx - OLLAMA_CONTEXT_SAFETY_TOKENS):
            raise IntelligenceError(
                "MODEL_CONTEXT_LIMIT",
                "The local model reported token usage near its context limit. Increase the supported local context setting or reduce the input before retrying.",
                status_code=422,
                details={"task": task, "estimated": False, "prompt_tokens": prompt_count,
                         "output_tokens": output_count, "context_tokens": self.settings.ollama_num_ctx,
                         "safety_tokens": OLLAMA_CONTEXT_SAFETY_TOKENS},
            )
        try:
            return _parse_model_content(data["message"]["content"], self.name)
        except (KeyError, TypeError):
            raise _invalid_response(self.name) from None


def _queries(queries: list[str], options: ResearchOptions) -> list[str]:
    result: list[str] = []
    for query in queries:
        if not isinstance(query, str):
            continue
        query = " ".join(query.split())[:500]
        if options.region and query:
            query += " " + options.region.strip()
        if query and query not in result:
            result.append(query)
        if len(result) >= MAX_QUERIES:
            break
    return result


def _canonical_source_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 3000:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port
        netloc = parsed.hostname.lower()
        if ":" in netloc:
            netloc = "[" + netloc + "]"
        if port is not None and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
            netloc += f":{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/") or "/", parsed.query, ""))
    except ValueError:
        return None


def _document(title: Any, url: Any, content: Any, publisher: str | None = None,
              kind: str = "web") -> ResearchDocument | None:
    url = _canonical_source_url(url)
    if not url or not isinstance(title, str) or not title.strip() or not isinstance(content, str) or not content.strip():
        return None
    source_id = "src_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return ResearchDocument(
        source=Source(source_id=source_id, title=title.strip()[:500], url=url,
                      publisher=publisher or urlsplit(url).hostname, kind=kind),
        content=content.strip()[:MAX_CONTENT_CHARS],
    )


class DemoSearchProvider:
    """Original teaching fixtures; these are never presented as live web research."""

    name = "fixture"

    async def search(self, queries: list[str], options: ResearchOptions) -> list[ResearchDocument]:
        if not _queries(queries, options):
            return []
        try:
            fixture = json.loads((BACKEND_ROOT / "fixtures" / "research_sources.json").read_text(encoding="utf-8"))
            rows = fixture["documents"]
            if not isinstance(rows, list):
                raise ValueError
            documents = [
                _document(row["title"], row["url"], row["content"], "StartupForge teaching fixture", "fixture")
                for row in rows
            ]
            if any(doc is None for doc in documents):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise IntelligenceError("DEMO_FIXTURE_INVALID", "The bundled teaching fixtures could not be loaded.", status_code=500) from None
        return documents[:options.max_sources]


class TavilySearchProvider:
    name = "tavily"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.tavily_api_key.strip():
            raise _config_error("Set TAVILY_API_KEY before selecting Tavily search.")
        self.settings = settings
        self.transport = transport

    async def search(self, queries: list[str], options: ResearchOptions) -> list[ResearchDocument]:
        documents: dict[str, ResearchDocument] = {}
        async with httpx.AsyncClient(
            timeout=self.settings.provider_timeout, transport=self.transport, follow_redirects=False
        ) as client:
            for query in _queries(queries, options):
                data = await _request_json(
                    client, self.name, "POST", "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {self.settings.tavily_api_key}"},
                    json={
                        "query": query, "search_depth": "basic", "topic": "general",
                        "max_results": min(3, options.max_sources), "include_answer": False,
                        "include_raw_content": "text", "language": options.language,
                    },
                )
                rows = data.get("results")
                if not isinstance(rows, list):
                    raise _invalid_response(self.name)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    # Search snippets remain usable when raw page extraction fails.
                    content = row.get("raw_content") or row.get("content")
                    doc = _document(row.get("title"), row.get("url"), content)
                    if doc and doc.source.url not in documents:
                        documents[doc.source.url] = doc
                    if len(documents) >= options.max_sources:
                        break
                if len(documents) >= options.max_sources:
                    break
        return list(documents.values())


class WikipediaSearchProvider:
    """Keyless background reading, with explicit Wikipedia attribution.

    Introductions are not sufficient evidence for market size, current pricing,
    customer demand, or current competitor features. The pipeline reports this gap.
    """

    name = "wikipedia"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def search(self, queries: list[str], options: ResearchOptions) -> list[ResearchDocument]:
        queries = _queries(queries, options)
        if not queries:
            return []
        api_url = f"https://{options.language}.wikipedia.org/w/api.php"
        ids: dict[int, str] = {}
        async with httpx.AsyncClient(
            timeout=self.settings.provider_timeout, transport=self.transport, follow_redirects=False,
            headers={"User-Agent": "StartupForgeAI/0.1 (educational research prototype)"},
        ) as client:
            for query in queries:
                data = await _request_json(
                    client, self.name, "GET", api_url,
                    params={"action": "query", "format": "json", "formatversion": "2", "list": "search",
                            "srsearch": query, "srlimit": min(3, options.max_sources), "srnamespace": 0},
                )
                search = data.get("query", {}).get("search") if isinstance(data.get("query"), dict) else None
                if not isinstance(search, list):
                    raise _invalid_response(self.name)
                for row in search:
                    if isinstance(row, dict) and isinstance(row.get("pageid"), int) and isinstance(row.get("title"), str):
                        ids[row["pageid"]] = row["title"]
                    if len(ids) >= options.max_sources:
                        break
                if len(ids) >= options.max_sources:
                    break
            if not ids:
                return []
            data = await _request_json(
                client, self.name, "GET", api_url,
                params={"action": "query", "format": "json", "formatversion": "2", "prop": "extracts",
                        "pageids": "|".join(str(item) for item in ids), "exintro": 1, "explaintext": 1,
                        "exlimit": options.max_sources, "exchars": 1200},
            )
        pages = data.get("query", {}).get("pages") if isinstance(data.get("query"), dict) else None
        if not isinstance(pages, list):
            raise _invalid_response(self.name)
        documents: dict[str, ResearchDocument] = {}
        for page in pages:
            if not isinstance(page, dict) or page.get("pageid") not in ids:
                continue
            title = page.get("title")
            if not isinstance(title, str):
                continue
            url = f"https://{options.language}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='')}"
            doc = _document(title, url, page.get("extract"), "Wikipedia")
            if doc:
                documents[doc.source.url] = doc
        return list(documents.values())[:options.max_sources]


class DuckDuckGoLiteSearchProvider:
    """Keyless HTML search snippets, not full publication text or a stable API.

    Only the fixed DuckDuckGo Lite endpoint is requested. Destination links are
    kept for user verification and never fetched by this adapter. Challenges
    and changes to the result markup fail explicitly rather than yielding
    invented sources or silently selecting another provider.
    """

    name = "duckduckgo"
    endpoint = "https://lite.duckduckgo.com/lite/"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def _page(self, client: httpx.AsyncClient, query: str) -> str:
        try:
            async with client.stream("GET", self.endpoint, params={"q": query}) as response:
                status = response.status_code
                if status in {401, 403}:
                    raise IntelligenceError(
                        "PROVIDER_ACCESS_DENIED", "DuckDuckGo denied this search request. Use another research provider."
                    )
                if status == 429:
                    raise IntelligenceError("PROVIDER_RATE_LIMIT", "DuckDuckGo search rate limit reached.", retryable=True)
                if status >= 500:
                    raise IntelligenceError("PROVIDER_UNAVAILABLE", "DuckDuckGo search is temporarily unavailable.", retryable=True)
                if not 200 <= status < 300:
                    raise IntelligenceError("PROVIDER_REQUEST_REJECTED", "DuckDuckGo rejected the search request.")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
                    raise _invalid_response(self.name)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise _invalid_response(self.name)
                    chunks.append(chunk)
                try:
                    return b"".join(chunks).decode("utf-8")
                except UnicodeError:
                    raise _invalid_response(self.name) from None
        except httpx.TimeoutException:
            raise IntelligenceError("PROVIDER_TIMEOUT", "DuckDuckGo search timed out.", retryable=True) from None
        except httpx.RequestError:
            raise IntelligenceError("PROVIDER_UNAVAILABLE", "Cannot connect to DuckDuckGo search.", retryable=True) from None

    @staticmethod
    def _destination(href: Any) -> str | None:
        if not isinstance(href, str):
            return None
        # Lite normally uses a DuckDuckGo /l/ wrapper; parse the destination
        # locally and never request the redirect URL or the destination page.
        absolute = urljoin(DuckDuckGoLiteSearchProvider.endpoint, href)
        try:
            parsed = urlsplit(absolute)
            host = parsed.hostname or ""
            if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
                if parsed.path != "/l/":
                    return None
                values = parse_qs(parsed.query).get("uddg", [])
                if len(values) != 1:
                    return None
                absolute = values[0]
            destination = _canonical_source_url(absolute)
            if not destination:
                return None
            host = urlsplit(destination).hostname or ""
            if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
                return None
            return destination
        except ValueError:
            return None

    @staticmethod
    def _documents(html: str) -> list[ResearchDocument]:
        soup = BeautifulSoup(html, "html.parser")
        text = " ".join(soup.get_text(" ", strip=True).split())
        challenge = soup.select_one(
            '#challenge-form, #anomaly-modal, form[action*="anomaly.js"], .anomaly-modal, input[name="anomaly"]'
        )
        if challenge or re.search(r"unfortunately,? bots use duckduckgo|please complete (?:the )?following challenge", text, re.I):
            raise IntelligenceError(
                "SEARCH_CHALLENGE", "DuckDuckGo returned a verification challenge. Use another research provider."
            )
        links = soup.select("a.result-link")
        if not links:
            if soup.select_one(".no-results, .no-results__message") or re.search(r"\bno (?:more )?results(?: found)?\b", text, re.I):
                return []
            raise _invalid_response(DuckDuckGoLiteSearchProvider.name)
        documents: dict[str, ResearchDocument] = {}
        for link in links:
            destination = DuckDuckGoLiteSearchProvider._destination(link.get("href"))
            title = " ".join(link.get_text(" ", strip=True).split())
            row = link.find_parent("tr")
            snippet = None
            if row:
                # Snippets live in a following table row. Stop before the next
                # result so a missing snippet cannot inherit another source's.
                following = row.find_next_sibling("tr")
                for _ in range(4):
                    if not following or following.select_one("a.result-link"):
                        break
                    snippet = following.select_one(".result-snippet")
                    if snippet:
                        break
                    following = following.find_next_sibling("tr")
            if not destination or not title or not snippet:
                continue
            excerpt = " ".join(snippet.get_text(" ", strip=True).split())
            document = _document(
                title[:480] + " (search snippet)", destination, excerpt, "DuckDuckGo search snippet"
            )
            if document:
                documents.setdefault(document.source.url, document)
        if not documents:
            raise _invalid_response(DuckDuckGoLiteSearchProvider.name)
        return list(documents.values())

    async def search(self, queries: list[str], options: ResearchOptions) -> list[ResearchDocument]:
        documents: dict[str, ResearchDocument] = {}
        async with httpx.AsyncClient(
            timeout=self.settings.provider_timeout, transport=self.transport, follow_redirects=False,
            headers={"User-Agent": "StartupForgeAI/0.1 (educational research prototype)", "Accept": "text/html"},
        ) as client:
            for query in _queries(queries, options)[:3]:
                html = await self._page(client, query)
                for document in self._documents(html):
                    documents.setdefault(document.source.url, document)
                    if len(documents) >= min(8, options.max_sources):
                        break
                if len(documents) >= min(8, options.max_sources):
                    break
        return list(documents.values())


class CrossrefSearchProvider:
    """Public scholarly abstract metadata, with no publication-page fetching.

    Abstracts are deposited by publishers and may be incomplete or outdated.
    They provide background evidence rather than general market-search coverage.
    The public API's single-request concurrency limit is respected within this
    shared adapter; HTTP limits and access denials are surfaced without retries.
    """

    name = "crossref"
    endpoint = "https://api.crossref.org/works"
    min_request_interval = 5.0
    _topic_stopwords = frozenset(
        "a an and are as at be been by can for from has have how in into is it its of on or that the their this to "
        "was were what when where which will with would used use using research evidence paper study market size "
        "statistic data demand customer user business startup feasibility competitor alternative validation current "
        "recent trend student college campus university school online digital platform application app service "
        "help need way proposed marketplace build create enable efficient efficiency reliable secure friendly "
        "problem solution general based indian india pilot test concept product".split()
    )
    _concept_aliases = {
        "textbook": frozenset({"textbook", "coursebook"}),
        "resale": frozenset({"resale", "resell", "reselling", "buyback", "buy", "buying", "sell", "selling",
                              "purchase", "purchasing", "secondhand", "exchange", "price", "pricing", "cost",
                              "affordability", "affordable", "rent", "rental"}),
    }

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self._request_lock = asyncio.Lock()
        self._last_request_started: float | None = None
        self._clock = time.monotonic
        self._pause = asyncio.sleep

    async def _items(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
        # Bibliographic search focuses on publication metadata. Crossref may
        # still return partial matches, so it is not itself a relevance proof.
        params = {"query.bibliographic": query, "rows": min(12, max(6, limit * 3)), "filter": "has-abstract:true",
                  "select": "DOI,title,publisher,abstract"}
        try:
            async with self._request_lock:
                if self._last_request_started is not None:
                    remaining = self.min_request_interval - (self._clock() - self._last_request_started)
                    if remaining > 0:
                        await self._pause(remaining)
                self._last_request_started = self._clock()
                async with client.stream("GET", self.endpoint, params=params) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise IntelligenceError("PROVIDER_ACCESS_DENIED", "Crossref denied this research request.")
                    if status == 429:
                        raise IntelligenceError("PROVIDER_RATE_LIMIT", "Crossref research rate limit reached.",
                                                retryable=True, details=self._rate_details(response.headers))
                    if status >= 500:
                        raise IntelligenceError("PROVIDER_UNAVAILABLE", "Crossref research is temporarily unavailable.", retryable=True)
                    if not 200 <= status < 300:
                        raise IntelligenceError("PROVIDER_REQUEST_REJECTED", "Crossref rejected the research request.")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type and content_type != "application/json" and not content_type.endswith("+json"):
                        raise _invalid_response(self.name)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise _invalid_response(self.name)
                        chunks.append(chunk)
                    try:
                        data = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError):
                        raise _invalid_response(self.name) from None
        except httpx.TimeoutException:
            raise IntelligenceError("PROVIDER_TIMEOUT", "Crossref research timed out.", retryable=True) from None
        except httpx.RequestError:
            raise IntelligenceError("PROVIDER_UNAVAILABLE", "Cannot connect to Crossref research.", retryable=True) from None
        if (not isinstance(data, dict) or data.get("status") != "ok"
                or not isinstance(data.get("message"), dict)
                or not isinstance(data["message"].get("items"), list)):
            raise _invalid_response(self.name)
        return data["message"]["items"]

    @staticmethod
    def _rate_details(headers: httpx.Headers) -> dict:
        """Expose only bounded numeric scheduling diagnostics, never raw headers."""
        details: dict[str, int | float] = {}
        try:
            retry_after = float(headers.get("retry-after", ""))
            if math.isfinite(retry_after) and 0 <= retry_after <= 3600:
                details["retry_after_seconds"] = retry_after
        except ValueError:
            pass
        try:
            limit = int(headers.get("x-rate-limit-limit", ""))
            if 0 <= limit <= 100000:
                details["rate_limit_requests"] = limit
        except ValueError:
            pass
        interval = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", headers.get("x-rate-limit-interval", "").strip())
        if interval:
            seconds = float(interval[1]) * {None: 1, "ms": 0.001, "s": 1, "m": 60, "h": 3600}[interval[2]]
            if math.isfinite(seconds) and 0 <= seconds <= 3600:
                details["rate_limit_interval_seconds"] = seconds
        return details

    @staticmethod
    def _plain_text(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        if len(value) > MAX_CONTENT_CHARS or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", value, re.I):
            raise _invalid_response(CrossrefSearchProvider.name)
        # html.parser does not resolve remote XML entities or fetch embedded
        # links/images. External entity declarations are rejected explicitly.
        soup = BeautifulSoup(value, "html.parser")
        for tag in soup.select("script, style, noscript"):
            tag.decompose()
        text = " ".join(soup.get_text(" ", strip=True).split())
        return text or None

    @classmethod
    def _document(cls, row: Any) -> ResearchDocument | None:
        if not isinstance(row, dict):
            return None
        doi = row.get("DOI")
        titles = row.get("title")
        if (not isinstance(doi, str) or len(doi) > 1000
                or not re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x20]+", doi, re.ASCII)
                or not isinstance(titles, list) or not titles):
            return None
        title, abstract = cls._plain_text(titles[0]), cls._plain_text(row.get("abstract"))
        if not title or not abstract:
            return None
        # Build a DOI resolver URL ourselves; do not trust a deposited arbitrary
        # URL or follow DOI redirects. DOI suffix punctuation is URL-encoded.
        url = "https://doi.org/" + quote(doi.lower(), safe="/")
        publisher = cls._plain_text(row.get("publisher"))
        attribution = (publisher[:400] + " (Crossref abstract metadata)" if publisher
                       else "Crossref abstract metadata")
        return _document(title[:480] + " (abstract)", url, abstract, attribution)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        text = re.sub(r"\btext[\s-]+books?\b", "textbook", text.lower())
        words = re.findall(r"\b[^\W\d_]{3,}\b", text, re.UNICODE)
        return {word[:-3] + "y" if word.endswith("ies") and len(word) > 4 else
                word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 3 else word
                for word in words}

    @classmethod
    def _relevance_score(cls, topic: str, document: ResearchDocument, ignored: str = "") -> float:
        terms = cls._tokens(topic) - cls._topic_stopwords - cls._tokens(ignored)
        # Terms in the same narrow concept family count once. These lexical
        # checks are deliberately conservative and do not establish support
        # for a claim, geographic relevance, or current market applicability.
        concepts: list[frozenset[str]] = []
        for term in sorted(terms):
            group = next((aliases for aliases in cls._concept_aliases.values() if term in aliases), frozenset({term}))
            if group not in concepts:
                concepts.append(group)
        if not concepts:
            return 0.0
        title = cls._tokens(document.source.title)
        combined = title | cls._tokens(document.content)
        title_matches = sum(bool(group & title) for group in concepts)
        matches = sum(bool(group & combined) for group in concepts)
        textbook_topic = cls._concept_aliases["textbook"] in concepts
        # Preserve the specific commodity anchor even when the query contains
        # general terms such as resale, college or pricing. Textbook background
        # is useful, but it is never promoted to evidence of resale demand.
        if textbook_topic and not (cls._concept_aliases["textbook"] & combined):
            return 0.0
        minimum_matches = 1 if textbook_topic else min(2, len(concepts))
        if not title_matches or matches < minimum_matches:
            return 0.0
        return (3 * title_matches + matches) / len(concepts)

    @classmethod
    def filter_for_idea(cls, documents: list[ResearchDocument], idea: Any, options: ResearchOptions) -> list[ResearchDocument]:
        # The reviewed solution, rather than a broad planner query, anchors the
        # research topic. Brand, audience, domain and region terms cannot alone
        # justify a paper as evidence for the specific proposed product.
        ignored = " ".join([idea.project_name, idea.domain, *idea.target_users, options.region or ""])
        ranked = [(cls._relevance_score(idea.solution, document, ignored), document) for document in documents]
        return [document for score, document in sorted(ranked, key=lambda item: (-item[0], item[1].source.source_id)) if score > 0]

    async def search(self, queries: list[str], options: ResearchOptions) -> list[ResearchDocument]:
        documents: dict[str, ResearchDocument] = {}
        async with httpx.AsyncClient(
            timeout=min(30.0, self.settings.provider_timeout), transport=self.transport, follow_redirects=False,
            headers={"User-Agent": "StartupForgeAI/0.1 (educational research prototype)", "Accept": "application/json"},
        ) as client:
            # Execute only the primary topic query. Do not issue a burst of
            # extra requests merely to fill the source budget after filtering.
            for query in _queries(queries, options)[:1]:
                rows = await self._items(client, query, options.max_sources)
                usable = 0
                ranked: list[tuple[float, ResearchDocument]] = []
                for row in rows:
                    document = self._document(row)
                    if not document:
                        continue
                    usable += 1
                    score = self._relevance_score(query, document, options.region or "")
                    if score > 0:
                        ranked.append((score, document))
                for _, document in sorted(ranked, key=lambda item: (-item[0], item[1].source.source_id)):
                    documents.setdefault(document.source.url, document)
                    if len(documents) >= min(8, options.max_sources):
                        break
                if rows and not usable:
                    raise _invalid_response(self.name)
                if len(documents) >= min(8, options.max_sources):
                    break
        return list(documents.values())


def make_language_model(settings: Settings) -> LanguageModel:
    if settings.app_mode == "demo":
        from .demo import DemoLLM

        return DemoLLM(delay=settings.demo_delay)
    if settings.app_mode != "live":
        raise _config_error("APP_MODE must be demo or live.")
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleLanguageModel(settings)
    if settings.llm_provider == "ollama":
        return OllamaLanguageModel(settings)
    raise _config_error("LLM_PROVIDER must be openai_compatible or ollama.")


def make_search_provider(settings: Settings) -> SearchProvider:
    if settings.app_mode == "demo":
        return DemoSearchProvider()
    if settings.app_mode != "live":
        raise _config_error("APP_MODE must be demo or live.")
    if settings.search_provider == "tavily":
        return TavilySearchProvider(settings)
    if settings.search_provider == "wikipedia":
        return WikipediaSearchProvider(settings)
    if settings.search_provider == "duckduckgo":
        return DuckDuckGoLiteSearchProvider(settings)
    if settings.search_provider == "crossref":
        return CrossrefSearchProvider(settings)
    if settings.search_provider == "web":
        from .web_research import WebResearchProvider

        return WebResearchProvider(settings)
    raise _config_error("SEARCH_PROVIDER must be web, tavily, wikipedia, duckduckgo or crossref.")
