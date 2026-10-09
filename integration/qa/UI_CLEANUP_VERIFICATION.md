# UI cleanup verification

Checked on 9 October 2026 in the running app at `http://127.0.0.1:8000/`.

- Strict TypeScript checking and the production build passed.
- The original layout and controls are retained: two debate mode cards (**You vs. AI** and **Pro vs. Skeptic**), for/against stance controls, four metric cards, and five research tabs (**Reviewed analysis**, **Sources & evidence**, **Statistics**, **Critic review**, **Original analysis**).
- Decorative slogans, promotional side panels, and repeated explanatory text were trimmed. Research and debate remain on the same page; the cleanup does not replace the original controls with a minimal navigation layout.
- Desktop at 1280 × 900 was checked in the browser: both debate modes, stance controls, all five research tabs and keyboard navigation worked. There was no horizontal page overflow.
- A saved SunQuote context's 13 statistics remain in 13 individual cards. Exact quotes, complete qualifications and source links are visible. A metric that repeats its quote is omitted; original supporting passages remain accessible through citation disclosures.
- Extracted idea fields and research inputs remain together in the idea editor. Source excerpts remain visible in **Sources & evidence**; reviewed findings, critic review and original findings keep their own tabs.
- Mobile at 390 × 844 passed: both mode cards stack with all controls visible, all five research tabs remain present, and expanded idea fields remain visible. The page scroll width was 375 pixels, within the 390-pixel viewport.
- An invalid source URL displayed the complete HTTP(S) URL validation error and disabled **Research again**. The input was restored to its original empty value without submitting a request.
- Temporary viewport overrides were reset after verification.
- Existing model generation and backend endpoints were unchanged. This check used saved research and did not rerun model generation.

Local screenshots: `ui-restored-desktop.png` and `ui-restored-mobile.png`. Screenshots are excluded from Git.
