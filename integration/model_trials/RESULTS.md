# Top two local model options

8 October 2026. **Use Qwen3 4B Instruct 2507 as the default; keep Fin-R1 as the alternative for claim revision.** Both run locally through Ollama without an API key. This shortlist reflects suitability for our idea-intelligence workflow on the available laptop, rather than a general ranking of business intelligence models.

| Comparison | Qwen3 4B Instruct 2507 — default | Fin-R1 — alternative |
|---|---|---|
| Local model ID | `qwen3:4b-instruct-2507-q4_K_M` | `startupforge-fin-r1` |
| Completed / schema-valid responses | 7/7 / 7/7 | 7/7 / 7/7 |
| Idea extraction | Included students; added unsupported product characteristics and omitted provenance/constraints | Expanded users into 20 audiences and added unsupported features; omitted provenance/constraints |
| Analysis from supplied evidence | Used the interview and alternative facts; kept payment and commercial viability unverified | Cautious, but underused supported facts and described an unvalidated pilot as feasible |
| Criticism of seeded errors | Flagged both false claims and the nonexistent citation | Flagged both false claims; did not explicitly diagnose the nonexistent citation |
| Revision of seeded errors | Improved the summary, but retained the invented percentage and profit guarantee in claim text under weaker labels | Rewrote both false claims as uncertainty and preserved their claim IDs |
| Debate replies | Both replies obeyed the rule against numerical prose; Pro invented shortcomings of the free alternative | Pro repeated numerical interview results against that rule and invented a listing-quality shortcoming; Skeptic was cautious but generic |
| Cashbook calculation | Correct: closing cash INR 4,600; net change INR -400 | Named a formula without calculating either requested result |
| Citation quality | Included a valid but unrelated cashbook citation | Included valid but unrelated cashbook citations |
| First-call elapsed time | 18.47 seconds | 26.02 seconds |
| Median of the six later calls | 14.43 seconds | 17.35 seconds |
| Later-call elapsed range | 4.18–34.61 seconds | 4.58–38.62 seconds |
| Raw evidence | [Qwen trial](./qwen3_4b_instruct.json) | [Fin-R1 trial](./fin_r1.json) |

Qwen is the more balanced default in these observations: it calculated the requested figures and followed the debate numbers rule, with a lower measured median latency. Fin-R1's clearest advantage was actually rewriting unsupported claims. That advantage does not establish better extraction, arithmetic or overall research accuracy. Neither model consistently attached citations to claims their passages supported, so human review remains necessary.

## How to read this comparison

The seven independent cases used the same frozen [prompt protocol](./prompt_protocol_v1.json), synthetic interview/cashbook fixtures, Q4_K_M weights, a 4,096-token context, a 1,024-token output cap, temperature 0, seed 42 and a 180-second timeout. Raw model responses were evaluated before application guards or repair. **Schema-valid means the response fits the contract; it does not mean the task was answered correctly.**

Each case ran once on an i5-12450H / 16 GB RAM / RTX 3050 Laptop 6 GB machine. Background load and output length varied, and first-call times include model loading. These timings are observations, not a controlled speed benchmark or repeatability study. The running app uses newer prompts and a larger context, so this table does not measure that exact application configuration. Fin-R1 has baseline trial evidence here; the completed application workflow was checked with Qwen.

**Sources and market figures remain a separate limitation.** The filtered live Crossref check retained zero sources, evidence items or figures for the Indian used-textbook marketplace example. Its completed debates and saved context demonstrate integration, not validated demand, market size or commercial feasibility. Switching models cannot supply missing evidence by itself.

The verified local runtime contained only these two model options. Model weights and local runtime snapshots are excluded from the public repository. Qwen remains the configured local app's default. To try Fin-R1 after installing its weights, set `LLM_MODEL=startupforge-fin-r1` in `member1/backend/.env` and restart the API worker; the launcher reuses an existing worker, so running it alone does not apply this change. Fin-R1's larger weights may require CPU offloading at the app's larger context setting.

The public repository includes the two synthetic benchmark records and frozen prompt protocol linked above. The broader experimental audit and raw live research records remain local. The three other trial models were removed from the active Ollama list.
