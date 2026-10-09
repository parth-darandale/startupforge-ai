# Member 3 — Report and Project Workspace

This is a placeholder for independent implementation. Own formal report generation/DOCX export and final shared project, context, job, artifact, file and navigation infrastructure. `backend/`, `frontend/`, `templates/` and `tests/` are reserved for your work; they do not contain a report generator yet.

Implement `ReportsAdapter` from [the shared adapter interfaces](../member1/backend/app/integration/interfaces.py). Publish installed report templates, register actual report content/files and return their IDs with the exact context reference. Develop against [the synthetic context fixture](../integration/fixtures/project_context.demo.json) without requiring Member 1 to run.

At integration, obtain the full saved context with:

```http
GET http://127.0.0.1:8000/api/v1/projects/{project_id}/contexts/{version}/handoff
```

Send the response's complete `context` object to `POST /api/v1/reports/generations`, with `options: {template_id, formats: ["docx"]}` and an `Idempotency-Key` header. Discover templates through `GET /api/v1/reports/templates`. These prepared endpoints currently return `503 MODULE_NOT_AVAILABLE` until your adapter is injected.

See [MODULE_CONNECTIONS.md](../integration/MODULE_CONNECTIONS.md) and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) for final registration, immutable versions, atomic job provenance and the complete shared API scope. Replace Member 1's local harness rather than mounting duplicate project/job routes. Combined runs, artifact editing/rendering and downloads remain your work.
