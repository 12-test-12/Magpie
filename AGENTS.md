# Magpie collaboration requirements

## Current agreed scope (2026-10-15)

- Mainline one was delivered locally in `c43ef79`. The user explicitly started mainline two on 2026-10-15.
- Implement mainline two in `Magpie-探索引擎优化方案与实施路线.md`, beginning with work package C: research workspace navigation, overview, grouped and paginated asset/API browsing, bounded details and evidence provenance. Current delivery and remaining E/F work are recorded in `Magpie-主线二实施与验收记录.md`.
- Defer Agent / Planner / Worker and autonomous active testing. Keep the existing read-only relationship canvas; do not build an autonomous exploration engine.
- Preserve the native parser contract, original evidence, Contribution lineage, normalization and Undo / Redo. Do not create a competing asset or evidence truth store.
- Unknown or partially understood data must be preserved and visibly reported. A successful HTTP response does not mean a fully understood import.
- User requires local browser testing before advancing to packaging or release. Do not rely on GitHub-built packages as the first verification step.
- Use `dev.py` for an isolated local browser workspace; document actual tests and remaining limitations. Never use real project data for destructive test setup.
- Keep the implementation plan and progress/verification record current so later sessions recover these decisions. Re-read them before continuing; do not promise infallible conversational memory.
- Re-read both mainline acceptance records before continuing. Local commits are allowed for this delivery; the user will push to the remote themselves.
- Python 3.13 is the preferred development interpreter per repository documentation. If unavailable, record the compatible interpreter actually used, and do not claim a 3.13 verification.
