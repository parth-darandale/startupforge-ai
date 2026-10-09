# Local web research checks

The web research implementation and its limitations are described in
[WEB_RESEARCH.md](../WEB_RESEARCH.md). The scripts in
[member1/backend/scripts](../../member1/backend/scripts) can reproduce provider
and complete workflow checks against your own local installation.

A Qwen workflow was verified on 9 October 2026 with the SunQuote example:
automatic news discovery, fetched publication text, critic/revision, a saved
context, teammate handoff and a completed user debate. It took 99.84 seconds and
retained one publication, two passages and thirteen quoted figures. The result
was `ready_with_gaps`: sector financing and capacity figures do not establish
the proposed service's demand or market size.

The public repository includes source, tests, shared contracts, synthetic demo
fixtures and the two-model benchmark. Raw fetched publication passages, local
project exports, runtime snapshots and screenshots are excluded from Git.
They are local verification material, rather than required application assets.
