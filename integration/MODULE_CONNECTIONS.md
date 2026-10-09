# Member 2 and Member 3 connection points

The app lives in `member1/backend` and `member1/frontend`. Root `member2/` and `member3/` are teammate placeholders with independent backend, frontend, template and test folders. They contain no generator implementations. Shared contracts and fixtures remain in root `integration/`.

## Prepared API endpoints

All endpoints use the same app at `http://127.0.0.1:8000`. These seven routes are installed now.

| Method | Route | Behavior |
| --- | --- | --- |
| GET | `/api/v1/integration/modules` | Reports each destination's installed adapter, member, folder and routes. Availability means an adapter was injected; this is not an external service probe. |
| GET | `/api/v1/projects/{project_id}/contexts/{version}/handoff` | Returns the entire exact saved snapshot, its reference, generation eligibility and destination availability. |
| GET | `/api/v1/studio/templates?kind=pitch_deck` | Member 2's installed templates, supported formats and option bounds. Optional kind is `pitch_deck` or `poster`. |
| POST | `/api/v1/studio/pitch-decks` | Accepts a full saved context and deck options; submits an async deck job when the adapter is present. |
| POST | `/api/v1/studio/posters` | Accepts a full saved context and poster options; submits an independent async poster job when the adapter is present. |
| GET | `/api/v1/reports/templates` | Member 3's installed report templates and formats. |
| POST | `/api/v1/reports/generations` | Accepts a full saved context and report options; submits an independent async report job when the adapter is present. |

Every generation POST requires `Idempotency-Key`. Poll its returned `/api/v1/jobs/{job_id}` URL. Same project, operation, key and payload return the original job; changing input with that key returns `409 IDEMPOTENCY_CONFLICT`. Deck, poster and report operations have separate scopes and can run independently with the same committed snapshot. The current local runner executes at most two jobs concurrently; additional jobs queue.

While the teammate adapters are absent, their template and generation routes return **503 MODULE_NOT_AVAILABLE**, with `member`, `module`, `required_adapter` and `expected_folder` in the standard error envelope. No job, fake template, artifact or file is created. The API does not claim that a generator is complete.

## Handoff and request examples

After saving a reviewed context, retrieve the explicit version:

```http
GET /api/v1/projects/p_example/contexts/1/handoff
```

The response contains `context` (the complete saved `ProjectContext`), `context_ref` (`project_id`, `context_version`, `context_hash`), `generation_eligible`, and `destinations`. Use that **whole `context` object** in the generation requests, not the wrapper and not only its reference. IDs and templates below are illustrative; actual template IDs come from the installed module's template endpoint.

```http
POST /api/v1/studio/pitch-decks
Idempotency-Key: p_example-v1-deck-01
Content-Type: application/json

{
  "context": "<replace with the complete context JSON object>",
  "options": {"template_id": "<installed deck template>", "slide_count": 10, "tone": "professional"}
}
```

```http
POST /api/v1/studio/posters
Idempotency-Key: p_example-v1-poster-01
Content-Type: application/json

{
  "context": "<replace with the complete context JSON object>",
  "options": {"template_id": "<installed poster template>", "audience": "potential customers"}
}
```

```http
POST /api/v1/reports/generations
Idempotency-Key: p_example-v1-report-01
Content-Type: application/json

{
  "context": "<replace with the complete context JSON object>",
  "options": {"template_id": "<installed report template>", "formats": ["docx"]}
}
```

The quoted context placeholders are explanatory, not valid requests. API documentation at `/docs` shows the exact object schema. Report formats are `docx` and optional `pdf`; the selected template must explicitly support every requested format. Initial deck output requires `pptx`, and initial poster output requires `txt`. Unsupported exports return `422 UNSUPPORTED_FORMAT` with `supported_formats`. Adapter-defined template bounds are enforced by `validate_options`; the outer request schema also rejects malformed input.

## Immutable context and provenance rules

Before any output job is accepted, the connector recomputes the SHA-256 hash over canonical `ContextDraft` JSON, loads the requested project's exact saved version, and compares the entire supplied snapshot with storage. Edits to analysis, figures, warnings, identity or timestamp are rejected. Even recomputing the hash for an edited snapshot does not make it a saved version. Newer context versions never replace the submitted version. `blocked` reviews return `422 CONTEXT_BLOCKED`; `ready_with_gaps` reviews retain their qualifications and gaps for the generator to include.

Each accepted job stores `context_ref` with the exact project ID, version and hash. A successful adapter result must have matching `kind` and `context_ref`, and nonempty unique `artifact_ids` and `file_ids`. A wrong reference, malformed result, edited input context, or a failed adapter leaves a failed job rather than a successful-looking response. The connector verifies these structural/provenance guarantees; content consistency and file/manifest registration belong to the generating module and its shared repositories.

The handoff GET remains available for auditing a blocked snapshot and marks `generation_eligible: false`. Missing or altered generation contexts return typed errors such as `CONTEXT_NOT_FOUND`, `CONTEXT_HASH_MISMATCH` or `CONTEXT_SNAPSHOT_MISMATCH` before any job is created.

## Adapter implementation and final assembly

See [interfaces.py](../member1/backend/app/integration/interfaces.py) and [contracts.py](../member1/backend/app/integration/contracts.py).

Member 2 implements `VisualStudioAdapter`; Member 3 implements `ReportsAdapter`. Each publishes typed template metadata, validates its own supported options, and implements `async generate(kind, context, options, progress)`. It receives a deep copy of the verified snapshot and must treat it as immutable. It must register its actual structured artifacts and rendered files through the final shared infrastructure **before** returning `GenerationResult`. Use `IntelligenceError` for typed option/generation errors, and call the supplied async progress callback with an honest stage and percentage.

In the assembled Python host, pass concrete adapter instances:

```python
from app.main import create_app
from app.integration import IntegrationAdapters

# studio_adapter and report_adapter are the finished teammates' instances.
app = create_app(integration_adapters=IntegrationAdapters(
    visual_studio=studio_adapter,
    reports=report_adapter,
))
```

This calls Python services directly; generators do not call the intelligence HTTP API. For a different final host, mount `build_integration_router(IntegrationDependencies(...))` with the shared context repository, job runner and a job-context pin function, plus the standard error/request-ID middleware. The local harness pins synchronously before its asyncio job executes. Member 3's final durable job adapter must persist the context reference atomically with submission and retain the same idempotency behavior.

The shared frontend can route its saved-context callback to these APIs once teammate screens exist. No placeholder generation buttons were added to the current intelligence screen.

Combined `/projects/{id}/runs`, shared artifact preview/edit/render endpoints and file download APIs remain Member 3's implementation work; they are not reserved or faked by this connector scaffold. Module services need their actual artifacts/files registered and served through those shared interfaces before final end-to-end integration can pass.

Portable request/response schemas are in [schemas/](schemas/). Regenerate the eight additive schemas with `member1/backend/scripts/export_integration_contracts.py`; this does not replace the existing context schema 1.0 or the synthetic teammate fixture. Standalone teammate development may use [project_context.demo.json](fixtures/project_context.demo.json), clearly labelled synthetic, through a fixture-backed repository. Runtime live mode never loads fake generator adapters.

Validation: `member1/backend/tests/test_integration.py` uses the real local job runner with explicitly injected test generators to check unavailable modules, concurrent outputs, saved-version pins, tampering, blocked contexts, idempotency, options and invalid adapter results. These tests demonstrate the connection contract, not the unfinished output generators.
