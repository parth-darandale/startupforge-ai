# StartupForge AI — Integrated Application

This repository contains the complete StartupForge AI implementation. Run the commands in this file from the repository root, which corresponds to the `implementation` folder in the original capstone workspace. Original synopsis and review documents are kept separately from this repository.

The integrated app structures an idea, retrieves evidence, produces a reviewed analysis, and opens a debate room. It also creates a source-linked pitch deck, poster text and formal report from an immutable saved context. You can argue for or against an idea, or watch Pro and Skeptic agents respond to each other. Statistics stay linked to quoted evidence; unsupported demand, market size and pricing remain open gaps.

The app supports **live local AI without an API key**, using Qwen3-4B-Instruct-2507 through Ollama. Its model shortlist contains only **Qwen3 4B Instruct 2507** (default) and **Fin-R1** (claim-revision alternative). Read the [two-model comparison](integration/model_trials/RESULTS.md). Web research fetches full public HTML pages and bounded text-based PDF passages. These are small workflow checks, not a guarantee of business accuracy. A fresh checkout defaults to offline demo mode; demo sources and statistics are labelled synthetic. Local environments, model weights, credentials, project databases and generated frontend assets are excluded from Git.

A [one-page comparison PDF](output/pdf/StartupForge_AI_Top_Two_Models.pdf) is ready to share. It presents the observed strengths, failures and timings without treating synthetic test figures as market evidence.

## Set up a fresh checkout

Use Python 3.10+ and Node 22.12+ (or Node 24). The app runs through [Member 1's intelligence host](member1/README.md), [Member 2's pitch and visual studio](member2/README.md), and [Member 3's report and workspace module](member3/README.md). Read the [folder layout](integration/FOLDER_STRUCTURE.md).

From the repository root, create the Python environment, install dependencies, copy the example configuration and build the frontend. The credential-free demo runs directly through Uvicorn and requires no Ollama runtime or model downloads:

```powershell
python -m venv member1\backend\.venv
.\member1\backend\.venv\Scripts\python.exe -m pip install -r member1\backend\requirements.txt
Copy-Item member1\backend\.env.example member1\backend\.env
cd member1\frontend
npm ci
npm run build
cd ..\..
.\member1\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir member1\backend --host 127.0.0.1 --port 8000
```

For frontend development, run `npm run dev` from `member1/frontend` while the backend runs on port 8000. The Vite URL is `http://127.0.0.1:5173`.

Open [the app](http://127.0.0.1:8000/) and [interactive API documentation](http://127.0.0.1:8000/docs). SQLite creates and keeps local projects, analyses, jobs, debates and immutable context versions in `member1/backend/data/intelligence.sqlite3`.

## Launch from Windows

After the setup above, **StartupForge.exe** starts the local services and opens the app. This executable is a workspace launcher; it does not bundle Python, Node, the frontend build or model weights. The launcher also requires the pinned portable Ollama runtime, even when the backend is configured for demo mode. Install that runtime once:

```powershell
.\member1\backend\.venv\Scripts\python.exe member1\backend\scripts\download_model_assets.py runtime
```

Then double-click **StartupForge.exe** in the repository root, or run `.\Start-Member1.ps1` in PowerShell. See [launcher instructions](launcher/README.md), including how to rebuild the executable from its C# source. Model weights are optional for the demo and must be downloaded separately for live local AI.

The launcher starts the workspace's Ollama runtime and one API worker in the background. On Windows, a hidden WMI-created worker owns each service independently of the terminal/tool process tree. Logs and service PID records are in `member1/backend/.runtime`. It reuses services already running on the local ports. Changes to `member1/backend/.env` require restarting the API worker; restarting the launcher alone does not reload an existing worker.

## Try the complete workflow

1. Enter an idea, structure it, and correct the extracted fields. Original input is preserved.
2. Research the idea. Optionally enter public report/article URLs to fetch those pages directly. Inspect the reviewed analysis, source excerpts, statistics, critic and initial analysis.
3. Choose **You vs. AI** and your stance, or **Pro vs. Skeptic**. The AI takes the opposite stance in a user debate. A bounded number of rounds ends in a moderator summary.
4. Save the reviewed context. Debate arguments remain a separate transcript; they do not silently change the reviewed findings.
5. Generate a pitch deck, poster brief or report from that saved context. Edit content, save a new revision, render it and download the file.
6. Open Project workspace to reopen projects and find earlier context versions and generated files.

## Configure real models and research

Copy `member1/backend/.env.example` to `member1/backend/.env`, edit the server-side settings, and restart the backend. Credentials stay on the backend.

For an OpenAI-compatible model service:

```dotenv
APP_MODE=live
LLM_PROVIDER=openai_compatible
LLM_API_KEY=your-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=your-model-id
LLM_RESPONSE_FORMAT=json_schema
SEARCH_PROVIDER=wikipedia
```

The adapter uses Chat Completions with a JSON schema. Set `LLM_RESPONSE_FORMAT=json_object` only if your chosen service requires that format. It does not automatically retry a billable request in another format. See the official [structured output documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

The configuration used for the verified local Qwen workflow is:

```dotenv
APP_MODE=live
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M
OLLAMA_NUM_CTX=16384
OLLAMA_NUM_PREDICT=2048
OLLAMA_TEMPERATURE=0
OLLAMA_KEEP_ALIVE=5m
OLLAMA_THINK=default
OLLAMA_CONTEXT_BYTES_PER_TOKEN=3
SEARCH_PROVIDER=web
WEB_DISCOVERY_PROVIDER=bing_news
WEB_SEARCH_ENGINES=brave
WEB_SEARCH_REGION=in-en
WEB_REQUEST_TIMEOUT=20
RETRIEVAL_PROVIDER=keyword
PROVIDER_TIMEOUT=180
```

Ollama must be running and the chosen model must already be available. This implementation follows its [chat API](https://docs.ollama.com/api/chat). The optional portable setup uses Ollama 0.40.1 and a repository-local model cache under `member1/backend/models/ollama`; it does not require changing a global Ollama installation. The launcher limits it to one resident model and one inference request at a time. The verified workflow uses a 16,384-token context, increased after a real revision exceeded the earlier 12,288-token budget. Its workspace daemon sets `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`, following the [documented memory-saving cache configuration](https://docs.ollama.com/faq#how-can-i-set-the-quantization-type-for-the-kv-cache). These process settings apply to both local models and do not change Windows user/system environment variables. The frozen model comparison used 4,096 tokens and predates these runtime changes. Longer evidence or conversations can still fail with `MODEL_CONTEXT_LIMIT`; inputs are never deliberately clipped to force a request through. The UTF-8 preflight estimate is conservative, not an exact tokenizer count.

The alternate local model is `startupforge-fin-r1`. After installing it, change `LLM_MODEL` in `member1/backend/.env` and restart the API worker. Qwen remains the default because it handled the full comparison workflow more consistently; Fin-R1's observed advantage was rewriting unsupported claims. Fin-R1 has baseline trial evidence, while the complete app workflow was checked with Qwen. The repository includes the curated synthetic trial outputs for these two models. The broader trial archive and machine-specific diagnostics remain local and are excluded from publication.

For a fresh local-model setup, `member1/backend/scripts/download_model_assets.py runtime` downloads and verifies the pinned official Windows runtime. Run the launcher, then use `member1/backend/.runtime/ollama-v0.40.1/ollama.exe pull qwen3:4b-instruct-2507-q4_K_M` against the local server. For Fin-R1, install the optional client in `member1/backend/requirements-models.txt`, run `member1/backend/scripts/download_hub_asset.py fin_r1`, then `member1/backend/scripts/import_finance_models.py fin_r1` to verify and import its weights. These downloads are several gigabytes each and are separate from the app dependencies.

`SEARCH_PROVIDER=web` now discovers publisher URLs through [Bing News RSS](https://blogs.bing.com/search/April-2008/News-Search-%E2%80%93-now-with-RSS), then uses [Trafilatura](https://github.com/adbar/trafilatura) and [pypdf](https://github.com/py-pdf/pypdf) to fetch public HTML/PDF text. Set `WEB_DISCOVERY_PROVIDER=bing_news` (the default). Discovery requires no API key, but covers news publications rather than the whole web. Up to three concise topic/business queries supply at most sixteen fetched candidates and eight retained sources. Feed descriptions and external title hints never become numerical evidence. Live automatic fetching succeeded for rooftop solar and grocery delivery; missing reports, competitor/pricing pages, customer studies and numbers remain explicit gaps. `WEB_DISCOVERY_PROVIDER=ddgs` keeps [DDGS](https://github.com/deedy5/ddgs) selectable with explicit engines, although its earlier local probes failed. See [current sourcing checks and limitations](integration/WEB_RESEARCH.md).

The optional **Report or article URLs** field accepts up to eight public URLs. Entering URLs skips discovery and fetches only those pages, bounded by the requested source count. This path successfully downloaded full pages from Our World in Data and the World Bank during live checks. Robots exclusions/access denial, inaccessible pages and unreadable PDFs produce visible gaps rather than substituted snippets or synthetic numbers. The fetcher checks public network addresses and redirects, pins DNS addresses, preserves the actual fetched path, observes robots rules and spaces requests to a host. HTML downloads are capped at 4 MiB, PDFs at 8 MiB, PDF text at the first twenty pages, extracted text at 60,000 characters and model evidence at six selected passages. Partial extraction is labelled. JavaScript-only content, scanned PDFs, spreadsheets and paywalled content need a different accessible publication or future adapter. Publication metadata, when available, remains publisher-declared; retrieval time is not publication time.

Crossref remains selectable for scholarly abstracts using its [public API](https://github.com/CrossRef/rest-api-doc); it searches only the primary topic query and does not fetch full publications. Wikipedia supplies background introductions and DuckDuckGo Lite supplies snippets. For configured paid search use `SEARCH_PROVIDER=tavily` and `TAVILY_API_KEY`; that adapter follows the [Tavily API](https://docs.tavily.com/documentation/api-reference/endpoint/search) and has only mocked validation here. No suitable source means a research gap. Quotes alone do not establish current market size, regional applicability, demand or willingness to pay.

Default retrieval is a transparent keyword baseline. Live web sources and passages must match the reviewed solution’s topic; region, audience or generic business words alone cannot qualify them. Figure cards preserve literal ranges and include measurement-period, geography, forecast and source caveats when available. The UI also accepts a market region. For semantic embeddings install `member1/backend/requirements-semantic.txt` and set `RETRIEVAL_PROVIDER=fastembed`. The first run may download the configured embedding model; see [FastEmbed](https://qdrant.github.io/fastembed/). Provider errors are returned as failed jobs. Live mode never silently switches to demonstration data.

## The three modules

All three modules now run in the same FastAPI app and React workspace. Save a reviewed context in Idea Intelligence, then open **Pitch and poster** or **Reports** to generate, edit and download outputs. **Project workspace** lists saved versions and files and can start a combined run that researches one idea and launches the requested outputs together.

Member 2 creates a cited PPTX and a TXT poster brief. Member 3 creates DOCX/PDF reports and owns the project, job, artifact revision and download APIs. The outputs preserve the selected context version and hash; editing content creates a new revision and rendering creates a separate file.

See [the integrated API and data flow](integration/MODULE_CONNECTIONS.md), [Member 2's module](member2/README.md), [Member 3's workspace](member3/README.md), and the original [three-member implementation plan](IMPLEMENTATION_PLAN.md).

## Checks and practical limits

Latest verification on 9 October 2026: **484 backend tests passed in 28.02 seconds**, and the frontend production build passed. Earlier on 9 October, 475 backend tests passed before the review-handling regression coverage was added, and the compiled `StartupForge.exe --headless` started the configured local workspace successfully. Backend verification includes teammate connector contracts, transient Windows status-file locks, required extraction proof fields, pronoun-only solution fallbacks, numerical assertions in revised claims, bounded model output instructions, public-page extraction, quotation provenance, URL/redirect/DNS checks, bounded HTML/PDF downloads, robots rules and web research diagnostics. Read the [live workflow summary](integration/web_trials/README.md); raw project exports and machine-specific diagnostics are kept local. The two-model comparison retains its synthetic trial outputs. An earlier direct live two-page fetch/retrieval check produced five exactly quoted figures; they are education background, not validation of the textbook market.

The verified automatic Qwen workflow completed in **99.84 seconds**, with no supplied URLs, manual extraction corrections or fixture evidence. It retained one news publication, two passages and thirteen quoted statistic cards, saved context version 1 with `ready_with_gaps`, and completed a one-round user-versus-AI debate with a moderator summary. At that earlier check, both output modules were still placeholders; the current app now generates pitch, poster and report files through the integrated routes. The solar figures do not establish the factory-owner quote-comparison service's market size or demand. See the [verification summary](integration/qa/MEMBER1_UPGRADE_VERIFICATION.md). Raw workflow exports and screenshots remain in the local development workspace.

An earlier supplied-URL live workflow fetched two public pages, selected six passages, produced eighteen quoted figures and saved context version 1 after real Qwen analysis, criticism and revision. This historical run completed in about 146 seconds and retained `ready_with_gaps`; Indian textbook-market demand and pricing remain unvalidated. See the [live workflow summary](integration/web_trials/README.md) and [web research tools and data flow](integration/WEB_RESEARCH.md).

```powershell
cd member1\backend
.\.venv\Scripts\python.exe -m pytest -q
cd ..\frontend
npm run build
```

Tests cover model/search adapter contracts with mocked HTTP, pipeline review/citation checks, figure quotation integrity, idempotent jobs, immutable contexts, debate stance/round limits, concurrency and provider failures. Live local model calls and public Crossref requests have also been made. An earlier model-selection workflow completed extraction, research, critic/revision, a user debate, an agent debate and saving context version 1. Its extracted idea needed a correction at the existing review/edit step. The final filtered run retained zero sources, excerpts or figures for that Indian used-textbook idea. Its demand, market size and feasibility remain unvalidated. The raw application exports and failed research attempts remain local. The published two-model synthetic comparison records are separate from later app safeguards and prompt improvements.

The harness is a local, single-process prototype. Final deployment needs Member 3's authentication, access control, shared durable job execution and worker-safe leases. Live extraction requires internal proof lists and reconstructs provided fields from exact submitted spans; missing fields remain unknown and generated names/categories remain labelled inferred. Live supported analysis claims must be complete literal source quotations. Critic-rejected claims left unchanged become explicit unknowns, with the original analysis preserved. Detected numerical assertions in live assumptions or unknown claims are also replaced with explicit unknowns, directing readers to quoted figure cards and evidence rather than accepting unvalidated numbers under a different badge. Evidence IDs and exact quotations establish traceability; they do not prove that a source is accurate or that a model's interpretation is correct. A separate critic helps expose gaps, but is not an independent factual guarantee. Numerical debate claims are presented in validated quoted cards; a conservative prose filter reduces unsupported numbers but is not a general semantic fact checker.
