# Workspace layout

```text
startupforge-ai/                    Repository checkout (original implementation folder)
  README.md                        Setup and usage
  IMPLEMENTATION_PLAN.md           Three-member division and integration plan
  StartupForge.exe                 Windows workspace launcher
  Start-Member1.ps1                 PowerShell startup alternative
  launcher/                        Launcher source, rebuild script and checks
  member1/
    backend/                       Working FastAPI app
      app/modules/intelligence/    Member 1 feature services and API
      app/harness/                 Temporary project/job/SQLite adapters
      app/integration/             Connections for future teammate adapters
      scripts/                     Startup and optional model download tools
      tests/
      .env.example                 Credential-free demo configuration template
    frontend/
      src/modules/intelligence/    Mountable React feature
  member2/
    backend/                       Placeholder for Pitch and Visual Studio
    frontend/
    templates/
    tests/
  member3/
    backend/                       Placeholder for Report and Project Workspace
    frontend/
    templates/
    tests/
  integration/                     Shared schemas, fixtures, contracts and summaries
    model_trials/                  Curated synthetic two-model comparison
    web_trials/                    Public live-workflow summary
    qa/                            Verification summaries
  output/                          Implementation deliverable documents
```

Launch the app and run its setup commands from the repository root. This checkout
contains the implementation only; the original synopsis and review materials
remain separate in the local capstone workspace.

Create the Python virtual environment, install backend and frontend dependencies,
copy `.env.example` to `.env`, and build the frontend as described in the root
README. Local `.env`, `.venv`, `node_modules`, `dist`, runtime downloads, models,
SQLite databases, raw application exports and QA screenshots are excluded from
publication. A fresh checkout starts in demo mode. Live local AI requires an
Ollama runtime and separately downloaded model weights; the Windows launcher also
requires the portable runtime to be installed.

Each member owns their folder. Shared contracts stay outside the member folders.
At final assembly, Member 3 mounts the feature routers and frontend components
and supplies shared repository/job/file adapters. The current local harness is
replaced then. The placeholder folders are development locations, not pretend
implementations. No teammate server is required to run Member 1.

Setup commands use `python -m pip` and `python -m pytest` through the checkout's
local `.venv` Python so they use the intended environment.
