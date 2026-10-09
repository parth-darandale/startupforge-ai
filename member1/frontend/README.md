# Member 1 frontend

This is the independently runnable Idea Intelligence feature. It covers idea definition, evidence preparation, reviewed analysis, sourced statistics, and both debate modes. It generates no presentation or report files.

## Run

Use Node.js 22.12+ or 24 and start the backend on port 8000 first. See the
[repository setup instructions](../../README.md) for the Python environment and
demo configuration. From the repository root:

```powershell
cd member1\frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to `http://127.0.0.1:8000`.

```powershell
npm run build
```

The backend can serve the built `dist` folder at `http://127.0.0.1:8000/`. Generated assets and `node_modules` are excluded from Git, so a fresh checkout must run the installation and build commands. Start or restart the backend after the first build so it registers the static assets.

## Final integration

The exported component includes its styling and accepts a shared API base URL. The optional callback receives the complete immutable snapshot returned by the save endpoint.

```tsx
import { IntelligenceWorkspace } from './modules/intelligence'

<IntelligenceWorkspace
  apiBase="/api/v1"
  onContextSaved={(context) => workspace.openContext(context)}
/>
```

Member 3 can keep the standalone feature shell or adapt its navigation when mounting this component. The feature does not import teammate implementations and communicates through the agreed API contract. Public types and `IntelligenceApi` are also exported by the feature folder.

## Workflow

1. Enter an idea or choose a sample. Submit it to create a project and extract its structured fields.
2. Review and correct the project name, problem, solution, users, domain, assumptions, and constraints. The original input remains inspectable.
3. Choose a source limit of 1–8. The idea editor shows **Market region** and optional **Report or article URLs** alongside the extracted fields. Automatic discovery searches news publications. Up to eight supplied URLs replace discovery with direct page fetching. Start research. The UI polls bounded asynchronous jobs and exposes errors rather than inventing results.
4. Research results retain four metric cards: sources, traceable excerpts, sourced statistics, and assumptions/unknowns. Use the five research tabs: **Reviewed analysis**, **Sources & evidence**, **Statistics**, **Critic review**, and **Original analysis**. Analysis findings, source excerpts, and complete statistic qualifications remain visible. Each statistic has its own card; a metric that merely repeats the displayed quote is omitted. Supporting citations remain expandable.
5. The debate room stays on the same page as research. Choose the **You vs. AI** card and a for/against stance, or **Pro vs. Skeptic** to observe two agents. Choose 1–3 rounds. AI turns include evidence references and sourced statistics when available.
6. Save the reviewed context and copy or download its JSON for teammates. Context saving does not alter the research or automatically incorporate debate arguments into findings.
7. Reopen a recent project to retrieve its saved context and latest completed research. A failed latest research job is shown while a prior saved context stays available.

The original button cards, research tabs, and editing controls are retained. Decorative slogans, promotional side panels, and repeated explanatory text were removed. Demo mode labels fixture sources, synthetic statistics, and scripted debates. Live mode requires the backend provider configuration. Statistics are shown as cited numbers and excerpts with their full qualifications; no charts are generated.

## Verification

`npm run build` performs strict TypeScript checking and a production bundle build. Integration behavior should be checked against the real standalone backend: extraction, field corrections, analysis, both debate modes, citations, context commit, JSON download, project reopen, and a deliberately failed job. Keyboard users can move across the five research tabs with arrow keys, Home, and End. Modified shortcuts such as Ctrl+Home keep their normal browser behavior.

The restored layout passed the production build and browser checks against saved research at desktop and narrow widths; see [the verification record](../../integration/qa/UI_CLEANUP_VERIFICATION.md).
