# LangGraph Issue-to-PR MVP

A one-repository development pipeline. A maintainer labels an Issue `ai-ready`. Four separate model calls analyze the request, propose a change, summarize actual tests, and review the tested diff. Python routes failures back to development, with at most two revisions. The publisher checks the tested and reviewed SHA and opens a PR for GitHub CI and human review.

The sample product is a Python calculator under `demo/`. The model can propose changes to `demo/calculator.py`. The worker controls the test command, Git operations and PR publication.

## Install and test

```sh
uv sync --locked
uv run pytest -q tests
```

Worker CI supplies a PostgreSQL service and runs the database tests. Set `MVP_TEST_DATABASE_URL` and `MVP_TEST_POSTGRES_DSN` to a disposable test database to run them locally.

## Webhook and worker

Use a recent PostgreSQL instance. Configure `MVP_REPO` as `owner/repository`, `MVP_ALLOWED_ACTOR` as the maintainer login, `MVP_DATABASE_URL` as the DSN and `MVP_WEBHOOK_SECRET` as the webhook secret. Subscribe the GitHub App to Issues and send webhooks to the HTTPS ingress forwarding to `/webhook`.

Run these in separate processes with the same database:

```sh
uv run python -m issue_to_pr.cli webhook
uv run python -m issue_to_pr.cli worker
```

Run one worker instance. Keep its data directory on a persistent volume. The worker uses `GH_TOKEN` for GitHub access. Supply a refreshed GitHub App installation token with Issues write for status comments, Contents write for the issue branch, and Pull requests write. Set `ROLE_BACKEND=openai`, `ROLE_MODEL` and `ROLE_OPENAI_API_KEY` for a compatible model, and optionally `ROLE_OPENAI_BASE_URL`. `ROLE_BACKEND=codex` uses an authenticated local Codex CLI. Give candidate tests a dedicated sandbox with a disposable checkout.

## Lab transport

The public sandbox repo includes an Issue intake workflow that records an actual GitHub issue event and an Actions bot publisher so the repository owner can review the resulting PR. For this repository the one-shot command uses the intake artifact and can exercise a controlled failing first candidate. It uses SQLite unless `MVP_DATABASE_URL` is set.

```sh
uv run python -m issue_to_pr.cli run --intake-run RUN_ID --lab-bot --repair-drill
```

The optional `directory` role backend writes request JSON to `ROLE_MAILBOX_DIR`. A separate model runner writes a matching response JSON. This lets the lab run with locally connected role agents. The `--repair-drill` switch deliberately adds a faulty override to the first candidate. Tests execute against the real candidate commit, and subsequent development attempts receive the failing output.

The role outputs, candidate SHA, test evidence and PR link are saved under `.local/`. Reuse the intake run ID to resume. For a new attempt on an existing issue branch, use `run --issue NUMBER --attempt NEW_ID` with the appropriate publisher. Leave PR approval and merge to the human reviewer in GitHub.
