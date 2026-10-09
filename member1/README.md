# Member 1 — Idea Intelligence

The working application is in `backend/` and `frontend/` here. It owns idea
extraction, research, evidence selection, reviewed analysis, sourced statistics
and debates. The local backend also provides a development workspace/job harness.

Follow the [repository setup instructions](../README.md) to create the backend
environment, copy `.env.example`, install dependencies and build the frontend.
A fresh checkout defaults to a credential-free demo. Project databases, model
weights and local runtime files are created separately and are excluded from Git.

After setup and installation of the portable Ollama runtime, double-click
`../StartupForge.exe` from Windows Explorer to start the app. The launcher stays
beside all three member folders. Live local AI additionally requires downloading
the selected model weights and configuring `backend/.env`.

Shared schemas, sample contexts and connection instructions live in
`../integration/`. Members 2 and 3 develop independently against these contracts.
Their placeholder folders contain no implemented generators yet.

For backend checks, run `.venv\Scripts\python.exe -m pytest -q` from `backend/`.
For the frontend, run `npm run build` from `frontend/`.
