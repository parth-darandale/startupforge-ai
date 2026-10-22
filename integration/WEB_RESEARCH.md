# Live web research for Member 1

The app now fetches publication pages rather than treating search snippets as market evidence. It keeps Qwen3 4B Instruct and Fin-R1 as the only installed analysis models. Web fetching requires software with internet access; model weights alone do not supply live reports.

## Existing open-source options

| Project | Job | Choice for this app |
|---|---|---|
| [DDGS](https://github.com/deedy5/ddgs) | Keyless URL discovery through public search sites | Current default with Yahoo and DuckDuckGo engines. A live campus-meals probe found and fetched five full pages; access still depends on network and provider availability. |
| [Bing News RSS](https://blogs.bing.com/search/April-2008/News-Search-%E2%80%93-now-with-RSS) | Keyless discovery of publisher URLs from a public news feed | Optional news-only mode. It returned no matches for the latest campus-meals research run and does not search the whole web. |
| [Trafilatura](https://github.com/adbar/trafilatura) | Main HTML text, tables and metadata extraction | Implemented with bounded public HTTP fetching. Live public pages downloaded successfully. |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | Browser rendering/crawling and extraction | Useful future adapter for JavaScript pages; heavier browser setup. Self-hosted crawling does not include its cloud search API. |
| [ScrapeGraphAI](https://github.com/ScrapeGraphAI/Scrapegraph-ai) | LLM-directed scraping graphs, including local Ollama | Current main requires Python 3.12; this workspace uses Python 3.10. Existing pipeline already supplies analysis/review. |
| [NuExtract](https://huggingface.co/numind/NuExtract-1.5-tiny) | Specialized extraction model for turning supplied text into structured data | A real fine-tuned extraction model. It still needs a fetch/search tool to obtain current text. No third analysis model was installed. |

## End-to-end data flow

1. Review the structured idea.
2. If `source_urls` is empty, generate up to three concise queries: a core topic, market/pricing, and competitors/feasibility. DDGS with Yahoo and DuckDuckGo discovers publisher URLs by default; `WEB_DISCOVERY_PROVIDER=bing_news` selects the news-only alternative. If URLs are supplied, skip discovery and fetch those pages only.
3. Download accessible public HTML/PDF sources with public-address and redirect validation, DNS pinning, robots rules, per-host pacing, timeouts and size limits. Inaccessible pages create visible warnings and gaps.
4. Trafilatura extracts main text and HTML tables. pypdf extracts text-based PDF pages; scanned PDFs require a future OCR adapter.
5. Before filling the retained-source budget, omit fetched pages without the reviewed solution's topic. Audience, region, domain or generic market terms alone cannot qualify a source. Keyword retrieval uses the reviewed solution and selects at most six passages; off-topic passages are omitted again. Every retained passage keeps its source ID, URL and locator. This conservative lexical selection does not prove semantic relevance or claim support.
6. Analysis, critic and revision consume the cited passages. Supported claims must be complete literal source quotations. Detected numerical assertions in assumptions or unknown claims are replaced with explicit unknowns even after revision; the original analysis remains available for audit. Exact currencies, percentages, population/business counts and energy-capacity quantities become quoted figure cards only when the quantity's own sentence mentions the reviewed topic. Pronouns such as “these” do not inherit relevance from a preceding sentence. Compatible numeric ranges remain complete ranges; bare “and” keeps independent quantities separate, and unsupported or incompatible ranges are omitted. Search descriptions and external title hints cannot become figures.
7. Saving commits sources, evidence, figures, final analysis and gaps into the existing schema 1.0 `ProjectContext`, consumed by the other two modules.

## Configuration and API

```dotenv
SEARCH_PROVIDER=web
WEB_DISCOVERY_PROVIDER=ddgs
WEB_SEARCH_ENGINES=yahoo,duckduckgo
WEB_SEARCH_REGION=in-en
WEB_REQUEST_TIMEOUT=20
```

`WEB_DISCOVERY_PROVIDER` accepts `ddgs` (default) or `bing_news`. `WEB_SEARCH_ENGINES` is used only for DDGS; the default is `yahoo,duckduckgo`, names must be enabled in the installed version, and `auto`/encyclopedia engines are excluded. `WEB_SEARCH_REGION` is a discovery locale; `research_options.region` also qualifies queries. Neither proves geographic applicability. No API key is required, but keyless search availability may vary by network/provider.

The existing `POST /api/v1/intelligence/analyses` accepts:

```json
{
  "project_id": "existing-project-id",
  "idea": "the existing complete IdeaSpec object",
  "research_options": {
    "max_sources": 2,
    "language": "en",
    "region": "India",
    "source_urls": ["https://publisher.example/report"]
  }
}
```

The illustrative `idea` placeholder must be replaced with an actual `IdeaSpec`. `source_urls` defaults to empty, accepts at most eight HTTP(S) URLs without credentials, and is supported only by the live `web` provider. Existing endpoint responses and the saved context boundary remain unchanged. Supplied URLs are exact resource paths; `/report` and `/report/` remain distinct.

## Limits and evidence

Downloads are limited to 4 MiB HTML/8 MiB PDF, twenty PDF pages and 60,000 extracted characters. Partial sources are labelled. The app does not render JavaScript, solve challenges, perform OCR, parse spreadsheets, access paywalls or automatically compute TAM/SAM/SOM. Publication dates are publisher-declared when available; `retrieved_at` is only the retrieval time.

Discovery XML is separately limited to 256 KiB, with public-address pinning, robots checks, per-host pacing, bounded redirects and XML entity declarations rejected. Only recognized Bing news click links are unwrapped, without following the tracking URL. Feed descriptions/dates are discarded. General Bing `/search` was disallowed by the live robots rules and is not used. Access denial, rate limiting or invalid feeds stop further discovery queries; denied publisher hosts are not retried within that research request.

Every Bing News run reports that evergreen reports, competitor product/pricing pages and customer studies may be missing. General web search can still omit sources or return pages that cannot be fetched; those become visible gaps. An empty index result does not mean relevant publications do not exist. Supplied public report URLs remain useful for deeper coverage.

Figure qualifications identify years and places explicitly mentioned in the quoted text, or state that the measurement period/geography is unknown. Opinion/interview, historical and prospective wording receive cautions. These annotations do not certify publication accuracy or make a quotation applicable to the requested market. A quoted statistic is not calculated TAM/SAM/SOM or evidence of startup demand.

## Current automatic discovery checks

`integration/web_trials/automatic_news_research.json` records two real checks with no supplied URLs, fixture data or model calls. The reproducible script is `member1/backend/scripts/probe_automatic_research.py`.

| Topic | Retained sources | Selected evidence | Topic-qualified figure cards | Interpretation |
|---|---:|---:|---:|---|
| Indian rooftop solar quotations for factory owners | 2 | 6 | 11 | Actual news coverage and an industry interview; includes residential-capacity context that does not establish the proposed factory-owner business's addressable market. |
| Grocery delivery for Indian urban households | 2 | 5 | 0 | Actual quick-commerce/logistics news fetched; conservative sentence/topic guards retained no qualifying quantities. Missing market figures remain unknown. |

The solar sources were The HinduBusinessLine and Power Peak Digest. Businessworld's robots endpoint denied access, so it supplied no evidence. The grocery sources were Urban Acres and Whalesbook. Publishers' reported figures, interview statements and forecasts were not independently verified. The bounded fetch/retrieval checks took approximately 6.4 and 6.0 seconds respectively, excluding language-model analysis. After numeric-range corrections and removal of pronoun relevance inheritance, figure cards were recomputed locally from unchanged fetched evidence. The separate `automatic_news_figures_reviewed.json` records the input file's SHA-256 hash, current figures and unchanged counts of eleven and zero. The earlier raw record remains preserved; no extra search, fetch or model call was credited.

## Complete local AI workflow

The newer complete API check is [the web research verification summary](web_trials/README.md). Real Qwen extraction, automatic discovery, analysis/critic/revision, context v1, immutable handoff, missing-module checks and a complete user debate passed in 99.84 seconds. No supplied URLs, fixture evidence or manual field corrections were used. One HinduBusinessLine publication supplied two passages and thirteen quoted cards about World Bank rooftop-solar financing and capacity. The requested budget of two sources was not filled. Sector funding, residential goals, historical figures and forecasts do not establish small factory owners' demand or the proposed service's addressable market. The final review correctly retains `ready_with_gaps` and five unknown startup-specific conclusions. Flash Attention and Q8 cache kept all 37 local Qwen layers on the GPU; earlier context-limit and critic-timeout attempts are retained separately rather than credited as successful runs.

## Historical source-access checks

Earlier probes remain unchanged in this directory's sibling `web_trials`: `search_probe.json`, `full_page_probe.json`, `public_access_probe.json`, and `live_workflow.json`. They predate the new default discovery and topic guards. The government-source attempt was denied, while full public Our World in Data and World Bank education pages were fetched. These were source-access and quotation checks. Broad education statistics do not establish Indian used-textbook demand, current market revenue or willingness to pay.

The complete live API run finished with two sources, six evidence passages, eighteen quoted figures, a `ready_with_gaps` review and saved context version 1. It used real Qwen analysis/critic/revision and supplied source URLs, completing in about 146 seconds. It does not credit automatic web discovery or validate the textbook business. A separate deterministic four-passage check yielded five figures; different retrieval budgets select different quotations.

Automatic discovery failures remain explicit. No failed search/page is replaced by a snippet, model memory or demonstration data.
