# Member 1 integration contract — schema 1.0

Member 1 owns idea extraction, research/retrieval, reviewed analysis, sourced statistics and debates. It can be developed and tested independently. All HTTP routes below use `/api/v1`. The committed `ProjectContext` is the boundary consumed by presentation/poster and report modules.

## Required endpoints

| Method | Route | Request | Response / owner |
|---|---|---|---|
| GET | `/health` | — | Mode, configured providers, capabilities and optional live `model`. This reports configuration, not a probe of provider availability. Shared host; local implementation supplied. |
| POST | `/projects` | `display_name`, `idea_text` | 201 project metadata. Member 3; local harness supplied. |
| GET | `/projects?limit=20` | Optional limit 1–100 | `{items, next_cursor}`. Member 3. |
| GET | `/projects/{project_id}` | — | Project metadata including `latest_context_version`. Member 3. |
| POST | `/intelligence/extractions` | `{project_id, idea_text}` | 202 `JobReceipt`; job result is `IdeaSpec`. Member 1. |
| POST | `/intelligence/analyses` | `{project_id, idea, research_options}` | 202 `JobReceipt`; job `result_url` identifies the analysis. Member 1. |
| GET | `/intelligence/analyses/{analysis_id}` | — | `AnalysisResult` containing `context_draft`, statistics and execution details. Member 1. |
| POST | `/intelligence/debates` | `{project_id, analysis_id, mode, user_stance, max_rounds}` | 202 `JobReceipt`; transcript URL is published during generation. Member 1. |
| GET | `/intelligence/debates/{debate_id}` | — | `DebateSession`, including turns, citations, figures and summary. Member 1. |
| POST | `/intelligence/debates/{debate_id}/turns` | `{message}` | 202 `JobReceipt`; user debate only. Member 1. |
| GET | `/jobs/{job_id}` | — | `Job` status, progress, result, result URL or structured error. Member 3. |
| GET | `/projects/{project_id}/jobs` | — | `{jobs, next_cursor}`; latest first. Member 3. |
| POST | `/projects/{project_id}/contexts` | `{expected_latest_version, context}` | 201 immutable `ProjectContext`; 409 if another version was saved. Member 3. |
| GET | `/projects/{project_id}/context` | — | Latest committed `ProjectContext`. Member 3. |
| GET | `/projects/{project_id}/contexts/{version}` | — | Exact immutable snapshot. Member 3. |

Each of the four feature generation POSTs requires `Idempotency-Key`. Replay the same key and payload to obtain the same job; changed payload returns 409. Keys are scoped by project and operation, and additionally by debate ID for turns. Context commits use optimistic version comparison, not job idempotency. All errors use `{error:{code,message,details,retryable,request_id}}`; failed jobs contain the same inner error object. `X-Request-ID` is echoed for diagnostics.

`JobReceipt` contains `job_id`, `status_url`, `operation`, `project_id`. Poll `status_url`; queued/running/succeeded/failed are the implemented statuses. Extracted ideas are in `Job.result`. Analysis/debate jobs identify their resource with `Job.result_url`; only use completed analysis jobs for downstream generation. A running debate may expose partial turns. Requests can fail inside an accepted job, so a 202 alone is not success.

`research_options` has `max_sources` (1–8), `language` (two-letter code), optional `region`, and optional `source_urls` (default empty, at most eight HTTP(S) URLs). This additive request option leaves existing requests and committed context schema 1.0 compatible. With `SEARCH_PROVIDER=web`, supplied URLs bypass discovery and fetch only those publications. Sources count toward `max_sources`; failed/denied sources are omitted with warnings and gaps. With another provider, supplied URLs fail explicitly with `USE_WEB_PROVIDER`; they are never silently ignored. Without supplied URLs, the default `WEB_DISCOVERY_PROVIDER=ddgs` searches general web results through the configured DDGS engines (Yahoo and DuckDuckGo by default) before fetching publication text. `WEB_DISCOVERY_PROVIDER=bing_news` remains an optional news-only mode. No extra endpoint or teammate dependency is needed. Publication text becomes referenced evidence and exact figure quotes using the existing output contracts; teammates continue consuming committed `ProjectContext`.

## Debate and statistics rules

`mode` is `user_vs_agent` or `agent_vs_agent`. `user_stance` is `for` or `against`; `max_rounds` is 1–5 (current UI offers 1–3). User mode starts with an opposing opening argument at round 0, then one user argument and one opposing response per round. Agent mode alternates Pro then Skeptic per round and runs automatically. Completed debates include a qualitative moderator summary.

Every debate is pinned to an immutable `analysis_id` and its project. Cross-project input is rejected. Blocked critic results cannot start a debate. Concurrent turns are excluded; failed replies leave the prior transcript intact. Unknown model citation IDs fail the turn. Figures selected for a turn must cite that turn's evidence.

Each `SourcedFigure` stores `figure_id`, `display_value`, `metric`, exact `excerpt`, `evidence_id`, `source_id`, `qualification`. Values and excerpts must be literal substrings of the cited evidence, and the source must match the evidence's source. Figures are also stored inside `ContextDraft` so teammates receive them when contexts are saved. Numbers alone are not inferred market sizes or forecasts. A conservative filter removes detected numerical claims from debate prose; validated cards carry quoted statistics. Extraction recognizes common digits/currencies/percentages/population units and is intentionally bounded; it does not cover every numeric format.

Debate transcripts remain separate from reviewed analysis. Saving a context does not endorse user arguments, choose a winner or convert debate claims into findings. If a later feature applies debate feedback to research, it must explicitly run a new review and create a new context version.

## Final assembly

1. Freeze the schemas and use the supplied fixture in each teammate's standalone app. Regenerate artifacts with `member1/backend/scripts/export_contracts.py` if the agreed schema changes.
2. Member 3 supplies `IntelligenceRepository` and `JobRunner` implementations from `interfaces.py`. The repository must preserve analyses, atomically claim/release debates, and store typed analysis/session objects. The job adapter must persist scoped idempotency, run the supplied async work, provide progress callbacks, implement `publish_result_url`, and serve the shared job endpoints.
3. Build `IntelligenceService` with the selected adapters, build `DebateService` using the same repository and model, and call `app.include_router(build_router(IntelligenceDependencies(...)))`. Keep Member 3's project/context/job endpoints. The local `app/harness` is replaced rather than mounted twice.
4. Mount `IntelligenceWorkspace` in the shared frontend with `apiBase="/api/v1"`. Its `onContextSaved` callback receives the complete committed snapshot.
5. Submit extraction, correct the idea, submit analysis, inspect it, then commit `context_draft` using the current expected version. The returned server-assigned `project_id`, version, UTC timestamp and hash identify the immutable snapshot.
6. Pass that full snapshot into Member 2's pitch/poster and Member 3's report APIs. The prepared endpoints, adapter signatures and final mounting steps are in [MODULE_CONNECTIONS.md](MODULE_CONNECTIONS.md). Those generators use `final_analysis`, sources, evidence, figures and unresolved gaps; `initial_analysis` is audit material and can intentionally contain unsupported claims.
7. Run final tests with one shared project: commit v1, generate all outputs from v1, commit v2, and confirm old outputs still identify v1. Also check idempotent retries, failed provider calls, debate concurrency and context version conflicts.

The local harness uses SQLite plus in-process async jobs. Run one Uvicorn worker. Startup marks interrupted jobs failed. It does not resume partially completed jobs or debates. A shared production adapter must use durable queues and recoverable owner-safe leases across workers, rather than copying the prototype busy flag. Authentication and project access checks belong to the shared host.

## Research choices

The pipeline follows the retrieval-plus-generation separation described by [RAG](https://arxiv.org/abs/2005.11401): collected source documents become individually referenced evidence passages before analysis. A separate feedback/revision pass is motivated by [Self-Refine](https://arxiv.org/abs/2303.17651); this implementation caps revision at one pass and retains initial output, feedback and final output. These papers motivate the design, not a claim that this prototype reproduces their results.

Provider formats follow official documentation linked in the root README. Source pages, original ideas and debate history stay in untrusted input payloads. The installed configuration uses local Qwen, keyless general-web discovery through Yahoo and DuckDuckGo, and public-page HTML/PDF fetching through Trafilatura and pypdf. Live scrape records are in `integration/web_trials`; model comparisons remain in `integration/model_trials`. Keyless discovery can fail or omit sources; direct report URLs allow fetching independently of discovery. Optional Bing News RSS has narrower news coverage. Partial/unavailable publications produce research gaps. Schema validation, valid citation IDs and exact quotations establish structure and lineage; semantic entailment, freshness, geography, source quality and market applicability require review. Model failures are failed jobs. Individual discovery/page failures are preserved in successful research outputs as warnings/gaps with no fabricated replacement data. Live mode never invokes synthetic demo data.
