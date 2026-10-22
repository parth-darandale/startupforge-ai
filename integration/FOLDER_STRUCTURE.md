# Workspace layout

```text
startupforge-ai/                    Repository checkout (original implementation folder)
  README.md                         Setup and integrated app guide
  IMPLEMENTATION_PLAN.md            Three-member division and integration plan
  StartupForge.exe                  Windows workspace launcher
  Start-Member1.ps1                  PowerShell startup alternative
  launcher/                         Launcher source and rebuild instructions
  member1/
    backend/app/modules/intelligence/ Idea extraction, research, analysis, debate and API
    backend/app/harness/             Compatibility imports for older callers
    backend/app/integration/         Shared generator contracts and routes
    frontend/src/modules/intelligence/ Idea Intelligence React workspace
  member2/
    backend/startupforge_member2/    Pitch deck and poster generation/rendering
    frontend/src/modules/visualStudio/ Deck and poster editor/preview
    templates/                       Reserved for additional deck/poster themes
    tests/                           Module test location
  member3/
    backend/startupforge_member3/    Reports, SQLite project/job store, artifact files and workspace routes
    backend/data/files/              Generated output bytes (ignored by Git)
    frontend/src/app/                Integrated navigation shell
    frontend/src/api.ts              Shared workspace API client
    frontend/src/modules/reports/    Report editor, preview and exports
    frontend/src/modules/workspace/  Project and artifact library
    templates/                       Reserved for additional report templates
    tests/                           Module test location
  integration/                      Shared schemas, fixtures, contracts and summaries
    model_trials/                    Curated two-model comparison
    web_trials/                      Live sourcing workflow notes
    qa/                              Verification and migration summaries
  output/                            Implementation deliverable documents
```

Run setup and launch commands from the repository root. The original synopsis and review files remain in the parent capstone folder. Local `.env`, `.venv`, `node_modules`, `dist`, runtime downloads, models, SQLite databases, generated files and raw QA evidence are excluded from publication. A fresh checkout starts in demo mode; live local AI uses Ollama and separately downloaded model weights.

The integrated app uses one FastAPI host and one React build. All modules consume the same immutable context versions. Member 3 registers common project, job, artifact and download endpoints once; Member 1 provides analysis and debate, Member 2 provides PPTX/TXT generation, and Member 3 provides DOCX/PDF generation and the workspace shell. See `MODULE_CONNECTIONS.md` for route and version details.
