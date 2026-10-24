# Member 2 — Pitch and Visual Studio

Member 2 owns context-grounded pitch deck and poster output. The module has a FastAPI adapter in `backend/startupforge_member2/visual_studio.py` and an editable React workspace in `frontend/src/modules/visualStudio/`.

The deck generator creates a structured slide outline and a real `.pptx` file. The poster generator creates an editable one-page brief and a `.txt` export. Both read the exact saved `ProjectContext`; they preserve review labels, source and evidence IDs, exact quoted statistics, and open gaps. They do not invent market figures. Generated content is editable in the app. Saving edits creates an immutable content revision; rendering the revision creates a new downloadable file.

The shared host is `member1/backend/app/main.py`, with shared context and file storage owned by Member 3. The API routes are:

```text
GET  /api/v1/studio/templates?kind=pitch_deck|poster
POST /api/v1/studio/pitch-decks
POST /api/v1/studio/posters
```

Generation requires `Idempotency-Key` and a complete committed context. Template IDs and option bounds are returned by the template route. Pitch decks default to 10 slides and support 5–12 slides, three tones and PPTX. The themed deck uses larger type and concise points covering the customer problem, product, market, business model, pilot, scorecard and decisions. Proposed actions carry a PLAN label, while reviewed findings keep their original wording and references. Dense lists use two columns, all saved body blocks render, and PowerPoint speaker notes retain source URLs and supplied notes. The source slide uses compact clickable references. Open market questions remain explicit rather than receiving invented figures. Poster output supports TXT. Both return a normal job receipt; poll its `status_url`, then read the artifact manifest and download the registered file.

The module uses the shared Python environment and dependencies from `member1/backend/requirements.txt`. It needs `python-pptx` for PowerPoint rendering. The frontend is mounted in the integrated app at **Pitch and poster**.

An optional **Slide generator** selector also offers the locally installed AutoPresent Llama 3.1 8B slide model. It generates layout programs that are statically translated to editable objects; saved project text is bound afterwards, so generated wording cannot replace evidence or introduce figures. The standard generator remains the default. Setup, model provenance and runtime details are in [SLIDE_GENERATION.md](../integration/SLIDE_GENERATION.md).

Run the integrated app from the implementation root after setup:

```powershell
.\member1\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir member1\backend --host 127.0.0.1 --port 8000
```

The source context fixture is `../integration/fixtures/project_context.demo.json`. Shared schemas, requests, version rules and integration routes are described in `../integration/MODULE_CONNECTIONS.md` and `../IMPLEMENTATION_PLAN.md`.
