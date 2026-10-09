"""Keyless news URL discovery from Bing's public RSS feed.

The feed is an index, not evidence. Only destination pages fetched separately
may supply passages or figures. RSS coverage does not include the whole web.
"""
from __future__ import annotations

import re
import ipaddress
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlsplit

from .contracts import ResearchOptions
from .errors import IntelligenceError
from .web_fetch import MAX_FEED_BYTES, PublicPageFetcher, _normal_url, _public_ip


def publisher_url(value: str) -> str | None:
    """Unwrap only the documented feed's Bing click URLs; never follow them."""
    try:
        if not isinstance(value, str) or len(value) > 6000:
            return None
        parsed = urlsplit(value)
        if parsed.hostname in {"bing.com", "www.bing.com"}:
            if (parsed.scheme not in {"http", "https"} or parsed.path != "/news/apiclick.aspx"
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}):
                return None
            urls = parse_qs(parsed.query, max_num_fields=30).get("url", [])
            if len(urls) != 1:
                return None
            value = urls[0]
        result = _normal_url(value)
        hostname = urlsplit(result).hostname or ""
        if hostname in {"bing.com", "www.bing.com"}:
            return None
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass  # Fetcher validates every DNS answer before making requests.
        else:
            _public_ip(str(address))
        return result
    except (ValueError, IntelligenceError):
        return None


def parse_news_feed(body: bytes, limit: int = 8) -> list[dict[str, str]]:
    if not isinstance(body, bytes) or len(body) > MAX_FEED_BYTES:
        raise IntelligenceError("SEARCH_INVALID_RESPONSE", "The discovery feed exceeds the bounded XML size.")
    # Reject DTD/entity declarations before parsing, including UTF-16/32 byte
    # encodings. ElementTree then has no declared entity expansions to process.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body.replace(b"\x00", b""), re.I):
        raise IntelligenceError("SEARCH_INVALID_RESPONSE", "The discovery feed contains unsupported XML declarations.")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        raise IntelligenceError("SEARCH_INVALID_RESPONSE", "The discovery service returned invalid RSS XML.") from None
    local = lambda element: element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
    if local(root) != "rss":
        raise IntelligenceError("SEARCH_INVALID_RESPONSE", "The discovery service did not return an RSS feed.")
    channels = [element for element in root if local(element) == "channel"]
    if len(channels) != 1:
        raise IntelligenceError("SEARCH_INVALID_RESPONSE", "The discovery feed has an invalid channel.")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in channels[0]:
        if local(item) != "item":
            continue
        fields = {local(element): element.text or "" for element in item}
        url = publisher_url(fields.get("link", ""))
        title = fields.get("title", "").strip()
        if not url or not title or url in seen:
            continue
        seen.add(url)
        # Deliberately discard descriptions and timestamps. They describe a
        # search result and are neither quotations nor publication metadata.
        results.append({"href": url, "title": title[:500]})
        if len(results) >= min(8, max(1, limit)):
            break
    return results


class BingNewsDiscovery:
    scope = "news"
    endpoint = "https://www.bing.com/news/search"

    def __init__(self, fetcher: PublicPageFetcher, region: str = "in-en"):
        self.fetcher = fetcher
        self.region = region

    async def __call__(self, query: str, options: ResearchOptions) -> list[dict[str, str]]:
        country, language = self.region.split("-")
        url = self.endpoint + "?" + urlencode({"q": query, "format": "rss", "mkt": f"{language}-{country.upper()}"})
        final_url, body = await self.fetcher.fetch_xml_feed(url)
        # A redirected login/feed proxy cannot silently replace this provider.
        if urlsplit(final_url).hostname not in {"bing.com", "www.bing.com"}:
            raise IntelligenceError("SEARCH_INVALID_RESPONSE", "The discovery feed redirected to a different service.")
        return parse_news_feed(body)
