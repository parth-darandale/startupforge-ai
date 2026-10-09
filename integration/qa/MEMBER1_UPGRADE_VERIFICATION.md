# Member 1 upgrade verification

Verified on 9 October 2026 against the installed Windows workspace.

| Check | Result |
| --- | --- |
| Backend | 475 tests passed in 15.23 seconds. |
| Frontend | Strict TypeScript and Vite production build passed. |
| Native launcher | Compiled `StartupForge.exe --headless` exited 0 and started the actual local Ollama/API services. |
| Folder move | Application in `member1/backend` and `member1/frontend`; saved projects/models preserved. `member2` and `member3` contain documented placeholders. |
| Connector API | All seven prepared routes are present in OpenAPI. Unfinished generators return typed 503 errors; no output is fabricated. |
| Live workflow | Real Qwen, automatic news discovery, fetched publication text, review, context commit, exact handoff and completed user debate passed in 99.84 seconds. |
| Live evidence | One publication, two passages, thirteen quoted statistic cards; each figure's value/excerpt/source linkage was checked. |
| Runtime | 16,384 context tokens, Flash Attention, Q8 cache; all 37 model layers loaded on the GPU. |
| UI | Reopened old and new saved projects, checked the market-region/news-scope controls, displayed statistic citations and qualifications, and confirmed context v1. |

The live input was SunQuote, a rooftop-solar quote comparison service for small factory owners in India. No source URLs, fixture evidence or manual field corrections were used. Extraction preserved the named customer group and pilot/validation-before-charging constraint. A pronoun-only extracted solution conservatively retained the submitted text so its core product topic was not lost.

The saved snapshot's version/hash and full contents matched the handoff. Member 2's pitch-deck and Member 3's report calls returned `503 MODULE_NOT_AVAILABLE`. The real debate completed an opening, user objection, opposing reply and moderator summary. This verifies the connection and debate workflow; teammate generators are still unfinished.

The source discusses World Bank sector financing and rooftop-solar capacity, including residential, historical and prospective figures. Thirteen cards are quotations from one publication, not thirteen independent measurements or studies. The review retained `ready_with_gaps` and five unknown startup-specific conclusions. Customer demand, competitor coverage, pricing and service market size remain unvalidated. Automatic discovery covers news; accessible public report URLs can supply deeper material.

See [the public workflow summary](../web_trials/README.md). Raw successful results and runtime snapshots remain local and are excluded from Git. Previous proof-map/topic/context-limit/timeout attempts are retained in separate records. They are not credited as successful verification of the current safeguards.

API request examples and final adapter wiring: [MODULE_CONNECTIONS.md](../MODULE_CONNECTIONS.md).

## Workspace relocation

The complete application now lives in `implementation/`, separate from the
original synopsis and review documents. After the move, 475 backend tests,
five launcher tests and the frontend production build passed. The compiled
Windows launcher started the local services from the new directory.

The database file and all 17 model-cache files were preserved. All 15 saved
projects and six committed contexts remain accessible, both installed models
are available, and the latest context handoff retains its original hash.
Python activation paths and 22 command wrappers were repaired for the new
location. The detailed `relocation_verification.json` is a local diagnostic excluded from Git.

The local `member1-upgrade.jpg` screenshot verified quote cards, source links and market qualifications; screenshots are excluded from Git.
