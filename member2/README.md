# Member 2 — Pitch and Visual Studio

This is a placeholder for independent implementation. Own pitch narrative, editable slide/poster content, templates, PPTX rendering and poster TXT export in this folder. `backend/`, `frontend/`, `templates/` and `tests/` are reserved for your work; they do not contain working generators yet.

Implement `VisualStudioAdapter` from [the shared adapter interfaces](../member1/backend/app/integration/interfaces.py). Return actual registered outputs from `generate`; publish template IDs, formats and option bounds through `templates`. Develop against [the synthetic context fixture](../integration/fixtures/project_context.demo.json) without requiring Member 1 to run.

At integration, obtain the full saved context with:

```http
GET http://127.0.0.1:8000/api/v1/projects/{project_id}/contexts/{version}/handoff
```

Send the response's complete `context` object to `POST /api/v1/studio/pitch-decks` or `POST /api/v1/studio/posters`, with options and an `Idempotency-Key` header. Discover templates through `GET /api/v1/studio/templates`. These prepared endpoints currently return `503 MODULE_NOT_AVAILABLE` until your adapter is injected.

See [MODULE_CONNECTIONS.md](../integration/MODULE_CONNECTIONS.md) for exact request examples, context integrity, job/result provenance and final host registration. Shared artifact storage, jobs and downloads come from Member 3 at final assembly.
