# Member 1 — Idea Intelligence

The working application is in `backend/` and `frontend/` here. It owns idea
extraction, research, evidence selection, reviewed analysis, sourced statistics
and debates. The integrated host also mounts Member 2's pitch/poster generators
and Member 3's report, project, job, artifact and download services.

Follow the [repository setup instructions](../README.md) to create the backend
environment, copy `.env.example`, install dependencies and build the frontend.
A fresh checkout defaults to a credential-free demo. Project databases, model
weights and local runtime files are created separately and are excluded from Git.

After setup and installation of the portable Ollama runtime, double-click
`../StartupForge.exe` from Windows Explorer to start the app. The launcher stays
beside all three member folders. Live local AI additionally requires downloading
the selected model weights and configuring `backend/.env`.

Shared schemas and connection instructions live in `../integration/`. The main
frontend shell lives in
`../member3/frontend/src/app/` and mounts this module's exported workspace.

For backend checks, run `.venv\Scripts\python.exe -m pytest -q` from `backend/`.
For the frontend, run `npm run build` from `frontend/`.
