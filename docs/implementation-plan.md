# LangGraph Issue-to-PR implementation plan

## Goal

Run one authorized test-repository Issue through analysis, development, testing, review and GitHub PR publication. Verify the real test failure and repair loop and use GitHub as the authority for CI and human review.

## Components

1. Task graph. Build a Python LangGraph StateGraph with explicit structured state, two shared repair attempts, fail-closed gating and test/review evidence tied to the candidate commit. Exercise state transitions with injected services.
2. Role adapters. Support a completion-capable model through an OpenAI-compatible endpoint or the local Codex CLI, with distinct prompts and validated output. The developer proposes allowlisted file replacements. Trusted code writes and commits them. Test exit status remains authoritative.
3. Worker and GitHub. Use verified GitHub Issue data, an isolated Git checkout, actual repo tests, instance-level persistent checkpoints, and a publisher that discovers an existing issue branch PR before creating. Support a single-repo manual worker for acceptance and the signed webhook entry point backed by PostgreSQL.
4. Integration fixture. Seed one small Python product and a trusted GitHub CI workflow. Provide a controlled first-round fault for the repair drill, then run actual tests and model repair. Verify the GitHub PR, latest SHA, CI and review gate independently through GitHub. Human approval is an external acceptance step.

## Global constraints

Only the dedicated private demo repo is a publication target. GitHub write credentials belong to the deterministic publisher. No agent gets GitHub write tools. The fixture allowlist excludes the orchestration and workflow files. The graph finishes at PR handoff. GitHub owns CI and human approval.
