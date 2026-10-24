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

Debates accept a numeric length of 1–100 whole rounds (default 10). Each user
round is a user argument plus an AI reply, following an opening argument; each
automatic round is a Pro argument plus a Skeptic reply. The moderator summarizes
after the requested final round. Long live debates retain the full transcript
and use labelled older excerpts for model inputs while keeping recent exchanges
and source evidence complete. The app discloses this compaction. These debates
can take considerably longer than a research request; local context-limit
errors remain explicit. See `../integration/MEMBER1_CONTRACT.md` for the
endpoint, progress, failure and transcript details.

The model receives a citation allowlist for each debate turn. A citation or
response-validation error gets at most two correction retries, while invalid
arguments never enter the transcript. Interrupted debates offer **Resume debate**
and continue from the first missing argument with their original round count.
If only the final summary failed, resuming regenerates the summary alone.
