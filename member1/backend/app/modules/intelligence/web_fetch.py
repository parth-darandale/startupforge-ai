"""Fetch a public source page; search snippets never become evidence here.

Every connection is pinned to a checked public IP, including robots and redirect
requests. HTTPX still verifies HTTPS against the original hostname using SNI.
This is deliberately a plain HTML/PDF reader: access challenges and login walls
are reported rather than bypassed, and scanned PDFs do not receive invented OCR.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import math
import re
import socket
import time
import weakref
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .contracts import ResearchDocument, Source
from .errors import IntelligenceError

USER_AGENT = "StartupForgeAI/1.0 (public-source research; respects robots.txt)"
ROBOT_AGENT = "StartupForgeAI"
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
MAX_FEED_BYTES = 256 * 1024
MAX_CONTENT_CHARS = 60000
MAX_PDF_PAGES = 20
MAX_REDIRECTS = 3
ROBOTS_CACHE_SECONDS = 300
MIN_REQUEST_INTERVAL = 1.0

Resolver = Callable[[str, int], Awaitable[list[str]]]


def _error(code: str, message: str, *, retryable: bool = False) -> IntelligenceError:
    return IntelligenceError(code, message, retryable=retryable)


@dataclass
class _HostState:
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    robots_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float | None = None
    interval: float = MIN_REQUEST_INTERVAL
    robots: dict[str, tuple[float, RobotFileParser]] = field(default_factory=dict)


# The server uses one event loop. Weak loop keys also avoid retaining old test
# loops or reusing asyncio locks in another loop. Instances share pacing/cache.
_LOOP_HOSTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _host_state(hostname: str) -> _HostState:
    loop = asyncio.get_running_loop()
    states = _LOOP_HOSTS.setdefault(loop, {})
    return states.setdefault(hostname, _HostState())


def _normal_url(url: str) -> str:
    try:
        if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f\\]", url):
            raise ValueError
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not hostname
                or parsed.username is not None or parsed.password is not None
                or "%" in hostname):
            raise ValueError
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            raise ValueError
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        raise _error("PAGE_UNSAFE_URL", "The source URL must identify a public HTTP(S) page on its normal port.") from None


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
        checked = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        if not checked.is_global or checked.is_multicast:
            raise ValueError
        return str(address)
    except ValueError:
        raise _error("PAGE_UNSAFE_URL", "The source resolved to a non-public network address.") from None


async def _resolve(hostname: str, port: int) -> list[str]:
    results = await asyncio.get_running_loop().getaddrinfo(
        hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return list(dict.fromkeys(result[4][0] for result in results))


class PublicPageFetcher:
    """Return extracted, source-linked content from a publicly accessible URL.

    ``resolver`` and ``transport`` are injectable for tests. Production resolves
    DNS normally, validates *all* answers, then connects only to a validated IP.
    ``settings.web_fetch_timeout`` is accepted when present; otherwise timeout
    is 15 seconds for the complete fetch (robots, pacing and redirects included).
    """

    def __init__(self, settings=None, transport: httpx.AsyncBaseTransport | None = None,
                 *, timeout: float = 15.0, resolver: Resolver | None = None):
        self.timeout = float(getattr(settings, "web_fetch_timeout", timeout))
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.timeout > 60:
            raise _error("PROVIDER_CONFIGURATION", "Web page fetch timeout must be greater than zero and at most 60 seconds.")
        self.transport = transport
        self.resolver = resolver or _resolve

    async def fetch(self, url: str, title_hint: str = "") -> ResearchDocument:
        try:
            return await asyncio.wait_for(self._fetch(url, title_hint), timeout=self.timeout)
        except IntelligenceError as error:
            error.details.setdefault("stage", "page")
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise _error("PAGE_TIMEOUT", "The source page timed out.", retryable=True) from None
        except (httpx.RequestError, socket.gaierror, OSError):
            raise _error("PAGE_UNAVAILABLE", "The source page could not be reached.", retryable=True) from None

    async def fetch_xml_feed(self, url: str) -> tuple[str, bytes]:
        """Read bounded public XML for discovery, never a research document.

        Uses the same address pinning, robots policy and host pacing as page
        fetches. XML bytes are returned separately so feed snippets cannot be
        confused with downloaded publication evidence.
        """
        try:
            target, _headers, body = await asyncio.wait_for(self._download(url, feed=True), timeout=self.timeout)
            return target, body
        except IntelligenceError as error:
            error.details.setdefault("stage", "discovery_feed")
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise _error("SEARCH_TIMEOUT", "The discovery feed timed out.", retryable=True) from None
        except (httpx.RequestError, socket.gaierror, OSError):
            raise _error("SEARCH_UNAVAILABLE", "The discovery feed could not be reached.", retryable=True) from None

    async def _target(self, url: str) -> tuple[str, str]:
        normalized = _normal_url(url)
        parsed = urlsplit(normalized)
        hostname = parsed.hostname or ""
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            answers = await self.resolver(hostname, 443 if parsed.scheme == "https" else 80)
            if not answers:
                raise _error("PAGE_UNAVAILABLE", "The source hostname could not be resolved.", retryable=True)
            # A mixed public/private answer is rejected, not silently filtered.
            checked = [_public_ip(answer) for answer in answers]
            pinned = checked[0]
        else:
            pinned = _public_ip(str(address))
        return normalized, pinned

    @staticmethod
    async def _pace(hostname: str) -> None:
        state = _host_state(hostname)
        async with state.request_lock:
            if state.last_request is not None:
                remaining = state.interval - (time.monotonic() - state.last_request)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            state.last_request = time.monotonic()

    async def _read(self, client: httpx.AsyncClient, url: str, ip: str,
                    *, robots: bool = False, feed: bool = False) -> tuple[int, httpx.Headers, bytes]:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        await self._pace(hostname)
        host = f"[{hostname}]" if ":" in hostname else hostname
        pinned_host = f"[{ip}]" if ":" in ip else ip
        pinned_url = urlunsplit((parsed.scheme, pinned_host, parsed.path, parsed.query, ""))
        async with client.stream(
            "GET", pinned_url, headers={"Host": host},
            extensions={"sni_hostname": hostname},
        ) as response:
            status = response.status_code
            headers = response.headers
            if status in {301, 302, 303, 307, 308}:
                return status, headers, b""
            if status in {401, 403}:
                raise _error("PAGE_ACCESS_DENIED", "The source denies automated access or requires authentication.")
            if status == 429:
                raise _error("PAGE_RATE_LIMIT", "The source rate limit was reached.", retryable=True)
            if status >= 500:
                raise _error("PAGE_UNAVAILABLE", "The source is temporarily unavailable.", retryable=True)
            if robots and status in {404, 410}:
                return status, headers, b""
            if status < 200 or status >= 300:
                raise _error("PAGE_REQUEST_REJECTED", "The source rejected the page request.")
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if feed and content_type not in {"application/xml", "text/xml", "application/rss+xml", "application/atom+xml"}:
                raise _error("SEARCH_INVALID_RESPONSE", "The discovery service returned an access page or unsupported feed format.")
            if not robots and not feed and content_type not in {"text/html", "application/xhtml+xml", "application/pdf"}:
                raise _error("PAGE_UNSUPPORTED_CONTENT", "The source is not an HTML page or PDF report.")
            limit = MAX_ROBOTS_BYTES if robots else MAX_FEED_BYTES if feed else MAX_PDF_BYTES if content_type == "application/pdf" else MAX_HTML_BYTES
            try:
                declared_size = int(headers.get("content-length", "0"))
            except ValueError:
                declared_size = 0
            if declared_size > limit:
                raise _error("PAGE_TOO_LARGE", "The source exceeds the bounded download size.")
            encoding = headers.get("content-encoding", "identity").strip().lower()
            decoder = None
            if encoding == "gzip":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif encoding == "deflate":
                decoder = zlib.decompressobj(zlib.MAX_WBITS)
            elif encoding not in {"", "identity"}:
                raise _error("PAGE_CONTENT_ENCODING", "The source uses an unsupported content encoding.")
            # MockTransport responses are commonly already consumed by HTTPX.
            # Their cached content is already decoded; real streaming sockets
            # take the bounded raw/decompression path below.
            if response.is_stream_consumed:
                if len(response.content) > limit:
                    raise _error("PAGE_TOO_LARGE", "The expanded source exceeds the bounded download size.")
                return status, headers, response.content
            chunks: list[bytes] = []
            raw_size = expanded_size = 0
            # aiter_raw avoids HTTPX allocating an unbounded decompressed chunk.
            try:
                async for chunk in response.aiter_raw(chunk_size=65536):
                    raw_size += len(chunk)
                    if raw_size > limit:
                        raise _error("PAGE_TOO_LARGE", "The source exceeds the bounded download size.")
                    decoded = decoder.decompress(chunk, limit - expanded_size + 1) if decoder else chunk
                    expanded_size += len(decoded)
                    if expanded_size > limit or (decoder and decoder.unconsumed_tail):
                        raise _error("PAGE_TOO_LARGE", "The expanded source exceeds the bounded download size.")
                    chunks.append(decoded)
                if decoder:
                    # A valid complete stream is already fully decoded by
                    # decompress(). Do not use flush(), whose size argument is
                    # an allocation hint rather than a hard output limit.
                    if not decoder.eof or decoder.unused_data:
                        raise _error("PAGE_INVALID_CONTENT", "The source has an invalid compressed response.")
            except zlib.error:
                raise _error("PAGE_INVALID_CONTENT", "The source has an invalid compressed response.") from None
            return status, headers, b"".join(chunks)

    async def _robots(self, client: httpx.AsyncClient, url: str) -> None:
        try:
            await self._robots_policy(client, url)
        except IntelligenceError as error:
            error.details.setdefault("stage", "robots")
            if error.code == "PAGE_ACCESS_DENIED":
                error.message = "The source robots.txt endpoint denies automated access; the page was not fetched."
                error.args = (error.message,)
            raise

    async def _robots_policy(self, client: httpx.AsyncClient, url: str) -> None:
        parsed = urlsplit(url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        state = _host_state(parsed.hostname or "")
        async with state.robots_lock:
            cached = state.robots.get(origin)
            if cached is None or cached[0] <= time.monotonic():
                target = origin + "/robots.txt"
                body = b""
                for redirect in range(MAX_REDIRECTS + 1):
                    target, ip = await self._target(target)
                    status, headers, body = await self._read(client, target, ip, robots=True)
                    if status not in {301, 302, 303, 307, 308}:
                        break
                    if redirect == MAX_REDIRECTS or not headers.get("location"):
                        raise _error("PAGE_REDIRECT_LIMIT", "The source robots file has too many or invalid redirects.")
                    target = urljoin(target, headers["location"])
                parser = RobotFileParser(origin + "/robots.txt")
                robots_text = body.decode("utf-8-sig", errors="replace")
                if robots_text.lstrip().startswith("<"):
                    raise _error("PAGE_ACCESS_CHALLENGE", "The source robots endpoint returned an HTML access page instead of crawling rules.")
                parser.parse(robots_text.splitlines())
                delay = parser.crawl_delay(ROBOT_AGENT)
                rate = parser.request_rate(ROBOT_AGENT)
                state.interval = max(MIN_REQUEST_INTERVAL, float(delay or 0),
                                     rate.seconds / rate.requests if rate and rate.requests > 0 else 0)
                state.robots[origin] = (time.monotonic() + ROBOTS_CACHE_SECONDS, parser)
            else:
                parser = cached[1]
            if not parser.can_fetch(ROBOT_AGENT, url):
                raise _error("PAGE_ROBOTS_DENIED", "The source robots.txt disallows this page for StartupForgeAI.")

    async def _download(self, url: str, *, feed: bool = False) -> tuple[str, httpx.Headers, bytes]:
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=False,
            trust_env=False, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                     "Accept": ("application/rss+xml,application/xml,text/xml" if feed else
                                                "text/html,application/xhtml+xml,application/pdf")},
        ) as client:
            target = url
            for redirect in range(MAX_REDIRECTS + 1):
                target, ip = await self._target(target)
                await self._robots(client, target)
                # The page uses this already validated IP, never a fresh DNS lookup.
                status, headers, body = await self._read(client, target, ip, feed=feed)
                if status not in {301, 302, 303, 307, 308}:
                    break
                if redirect == MAX_REDIRECTS or not headers.get("location"):
                    raise _error("PAGE_REDIRECT_LIMIT", "The source has too many or invalid page redirects.")
                target = urljoin(target, headers["location"])
        return target, headers, body

    async def _fetch(self, url: str, title_hint: str) -> ResearchDocument:
        target, headers, body = await self._download(url)
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/pdf":
            title, content = await asyncio.to_thread(self._pdf, body, title_hint)
        else:
            title, content = await asyncio.to_thread(self._html, body, target, title_hint)
        # Keep the actual fetched path: /report and /report/ can serve different
        # pages. Remove fragments/default ports in _normal_url, then retain this
        # final URL rather than a publisher canonical link we never downloaded.
        parsed = urlsplit(target)
        canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        return ResearchDocument(
            source=Source(source_id="src_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
                          title=title.strip()[:500], url=canonical,
                          publisher=parsed.hostname, kind="web"),
            content=content,
        )

    @staticmethod
    def _html(body: bytes, url: str, title_hint: str) -> tuple[str, str]:
        try:
            import trafilatura
        except ImportError:
            raise _error("PROVIDER_CONFIGURATION", "Install the web extraction dependencies before fetching source pages.") from None

        soup = BeautifulSoup(body, "html.parser")
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        title_lower = page_title.lower()
        phrases = soup.get_text(" ", strip=True).lower()[:12000]
        blocked_title = re.search(r"(?:access denied|just a moment|security check|captcha|^log[ -]?in|^sign[ -]?in)", title_lower)
        blocked_body = any(phrase in phrases for phrase in (
            "verify you are human", "checking your browser", "enable javascript and cookies to continue",
            "sign in to continue", "log in to continue", "subscribe to read this article",
        ))
        if blocked_title or blocked_body or soup.select_one("#challenge-form, .cf-challenge, #cf-challenge-running"):
            raise _error("PAGE_ACCESS_CHALLENGE", "The source presents an access challenge, login or subscription wall.")
        if soup.select_one('input[type="password"]') and not soup.select_one("article, main"):
            raise _error("PAGE_ACCESS_CHALLENGE", "The source requires authentication.")
        try:
            text = trafilatura.extract(
                body, url=url, output_format="txt", include_tables=True,
                include_comments=False, include_links=False, include_images=False,
                favor_precision=True, fast=True,
            )
        except Exception:
            raise _error("PAGE_EXTRACTION_FAILED", "The source page could not be extracted.") from None
        if not text or len(text.strip()) < 100:
            raise _error("PAGE_NO_READABLE_CONTENT", "The source has too little readable main content; a JavaScript renderer or accessible report may be needed.")
        title = page_title or title_hint or urlsplit(url).hostname or "Public source"
        # A search-result hint can label the source, but is not downloaded
        # evidence. Only a title present in this page may enter quoted content.
        metadata = [f"Source page title: {page_title[:500]}"] if page_title else []
        for element in soup.find_all("meta"):
            name = (element.get("property") or element.get("name") or "").lower()
            if name in {"article:published_time", "datepublished", "dc.date.issued"}:
                value = element.get("content", "").strip()
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                metadata.append(f"Page-declared publication date: {value[:100]} (publisher metadata; not independently verified)")
                break
        content = ("\n".join(metadata) + "\n\n" if metadata else "") + text.strip()
        return title, _bounded_content(content)

    @staticmethod
    def _pdf(body: bytes, title_hint: str) -> tuple[str, str]:
        try:
            from pypdf import apply_configuration
        except ImportError:
            raise _error("PROVIDER_CONFIGURATION", "Install the PDF extraction dependency before fetching reports.") from None

        # pypdf 6.19 provides execution-context limits, so concurrent PDF reads
        # cannot overwrite one another's decoder limits. These also cover PDF
        # compression, separate from the HTTP response expansion cap above.
        with apply_configuration(
            maximum_declared_stream_length=MAX_PDF_BYTES,
            array_based_stream_maximum_output_length=MAX_PDF_BYTES,
            zlib_maximum_output_length=MAX_PDF_BYTES,
            zlib_maximum_recovery_input_length=MAX_ROBOTS_BYTES,
            lzw_maximum_output_length=MAX_PDF_BYTES,
            run_length_maximum_output_length=MAX_PDF_BYTES,
            jbig2_maximum_output_length=MAX_PDF_BYTES,
            image_maximum_buffer_size=MAX_PDF_BYTES,
            xmp_maximum_input_length=MAX_ROBOTS_BYTES,
            page_tree_maximum_entries=10000,
            page_tree_maximum_depth=50,
            xform_maximum_invocations_per_extraction=500,
        ):
            return PublicPageFetcher._read_pdf(body, title_hint)

    @staticmethod
    def _read_pdf(body: bytes, title_hint: str) -> tuple[str, str]:
        from pypdf import PdfReader
        from pypdf.errors import LimitReachedError

        try:
            reader = PdfReader(io.BytesIO(body), strict=True)
            if reader.is_encrypted:
                raise _error("PAGE_ACCESS_DENIED", "The source PDF is encrypted and cannot be read.")
            downloaded_title = str((reader.metadata or {}).get("/Title") or "").strip()
            title = downloaded_title or title_hint or "Public PDF report"
            pages = len(reader.pages)
            parts = [""]  # The actual extraction-scope description is set below.
            if downloaded_title:
                parts.append(f"Source PDF title: {downloaded_title[:500]}")
            read_count = min(pages, MAX_PDF_PAGES)
            creation = (reader.metadata or {}).get("/CreationDate")
            if creation and re.fullmatch(r"D:\d{4,14}(?:[Zz]|[+-]\d{2}'?\d{2}'?)?", str(creation)):
                parts.append(f"PDF-declared file creation date: {creation} (not a verified publication date)")
            readable = []
            processed = 0
            for index in range(read_count):
                text = reader.pages[index].extract_text() or ""
                processed = index + 1
                if text.strip():
                    readable.append(text.strip())
                    parts.append(f"\n[PDF page {index + 1}]\n{text.strip()}")
                if sum(len(part) for part in parts) > MAX_CONTENT_CHARS:
                    break
            if len("\n".join(readable)) < 100:
                raise _error("PAGE_NO_READABLE_CONTENT", "The PDF has too little extractable text; scanned pages require OCR that is not enabled.")
            parts[0] = f"PDF extraction scope: pages 1-{processed} of {pages}; text extraction only, no OCR."
            if pages > processed:
                parts.append(f"\n[Partial report: pages after {processed} were not extracted.]")
            return title, _bounded_content("\n".join(parts))
        except IntelligenceError:
            raise
        except LimitReachedError:
            raise _error("PAGE_TOO_LARGE", "The PDF exceeds bounded decompression or document processing limits.") from None
        except Exception:
            raise _error("PAGE_EXTRACTION_FAILED", "The PDF could not be parsed or its text extracted.") from None


def _bounded_content(content: str) -> str:
    if len(content) <= MAX_CONTENT_CHARS:
        return content
    note = "\n[Partial source: extracted text was limited to 60,000 characters.]"
    return content[:MAX_CONTENT_CHARS - len(note)] + note
