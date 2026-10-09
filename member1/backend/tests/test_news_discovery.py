"""RSS is URL discovery only; descriptions cannot become evidence or numbers."""
from urllib.parse import parse_qs, urlsplit

import pytest

from app.modules.intelligence.contracts import ResearchOptions
from app.modules.intelligence.errors import IntelligenceError
from app.modules.intelligence.news_discovery import BingNewsDiscovery, parse_news_feed, publisher_url
from app.modules.intelligence.web_fetch import MAX_FEED_BYTES


def feed(items):
    return ("<rss><channel>" + items + "</channel></rss>").encode()


def test_feed_unwraps_publisher_links_preserves_paths_and_discards_snippet_numbers():
    data = feed('''<item><title>Real report</title><link>https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fexample.org%2Freport%2F%23section&amp;c=123</link><description>999 billion invented customers</description></item>
    <item><title>Duplicate report</title><link>https://example.org/report/</link></item>
    <item><title>Different report</title><link>https://example.org/report</link></item>''')
    rows = parse_news_feed(data)
    assert rows == [{"href": "https://example.org/report/", "title": "Real report"},
                    {"href": "https://example.org/report", "title": "Different report"}]
    assert "999" not in str(rows)


@pytest.mark.parametrize("url", [
    "file:///C:/secret", "https://localhost/data", "http://127.0.0.1/data", "http://169.254.169.254/data",
    "https://user:pass@example.org/report", "https://example.org:8080/report", "javascript:alert(1)",
    "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fexample.org%2Fa&url=https%3A%2F%2Fother.org%2Fb",
    "https://www.bing.com:8080/news/apiclick.aspx?url=https%3A%2F%2Fexample.org%2Fa",
    "https://www.bing.com/news/apiclick.aspx?url=http%3A%2F%2F127.0.0.1%2Fsecret",
    "https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.bing.com%2Fsearch",
    "https://www.bing.com/unrelated?url=https%3A%2F%2Fexample.org%2Fa",
])
def test_invalid_and_non_public_publisher_urls_are_omitted(url):
    assert publisher_url(url) is None


@pytest.mark.parametrize("body", [
    b'<html><body>Access challenge</body></html>', b'<rss><channel>', b'<rss/>', b'<rss><channel/><channel/></rss>',
    b'<!DOCTYPE rss [<!ENTITY bomb "expanded">]><rss><channel/></rss>',
    '<!DOCTYPE rss [<!ENTITY bomb "expanded">]><rss><channel/></rss>'.encode('utf-16'),
    b'x' * (MAX_FEED_BYTES + 1),
], ids=['html', 'malformed', 'no-channel', 'two-channels', 'entity', 'utf16-entity', 'oversized'])
def test_invalid_oversized_and_entity_xml_is_rejected(body):
    with pytest.raises(IntelligenceError) as error:
        parse_news_feed(body)
    assert error.value.code == "SEARCH_INVALID_RESPONSE"


def test_empty_results_are_distinct_from_access_failure_and_feed_is_bounded():
    assert parse_news_feed(feed("")) == []
    items = ''.join(f'<item><title>Report {i}</title><link>https://example.org/{i}</link></item>' for i in range(20))
    assert len(parse_news_feed(feed(items))) == 8


@pytest.mark.asyncio
async def test_provider_queries_configured_market_through_feed_fetcher_only():
    class Reader:
        async def fetch_xml_feed(self, url):
            self.url = url
            return url, feed('<item><title>Solar report</title><link>https://example.org/solar</link></item>')
    reader = Reader()
    result = await BingNewsDiscovery(reader, 'in-en')('rooftop solar India', ResearchOptions())
    params = parse_qs(urlsplit(reader.url).query)
    assert params == {'q': ['rooftop solar India'], 'format': ['rss'], 'mkt': ['en-IN']}
    assert result[0]['href'] == 'https://example.org/solar'


@pytest.mark.asyncio
async def test_provider_rejects_redirected_other_service_feed():
    class Reader:
        async def fetch_xml_feed(self, url):
            return 'https://other.example.org/feed', feed('')
    with pytest.raises(IntelligenceError):
        await BingNewsDiscovery(Reader())('solar', ResearchOptions())
