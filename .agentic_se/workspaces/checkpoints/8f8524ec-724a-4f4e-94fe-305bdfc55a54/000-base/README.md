# Agentic Software Engineering System

A dependency-free prototype for the Agentic Software Engineering System assignment. It combines a runnable URL shortener with a governed SDLC orchestrator that turns a requirement into a reviewed, validated artifact bundle.

## Requirements

- Python 3.11 or newer
- No third-party packages for the offline stub provider, HTTP service, or tests
- Optional: an OpenAI-compatible API key and endpoint for remote agent output

## Run

In PowerShell, from the repository root:

```powershell
$env:PYTHONPATH = "src"
python -m agentic_se demo --scenario greenfield --provider stub
```

The CLI asks for human approval at the plan, review, and release-readiness gates. Type `APPROVE` only after reviewing the displayed material. Runs, event history, and artifacts are written under `.agentic_se/`.

Run the other assignment scenarios:

```powershell
python -m agentic_se demo --scenario brownfield --provider stub
python -m agentic_se demo --scenario ambiguous --provider stub
python -m agentic_se demo --scenario ambiguous --provider stub --answer "Track total and daily click counts; no personal identifiers; protect analytics with an API key."
```

The brownfield scenario injects one deterministic expired-link regression signal, records a failed test event, rolls back to the approved-plan checkpoint, re-runs implementation, and validates again. The ambiguous scenario safe-stops without an answer; the clarified run records a new requirement revision and proceeds through approval gates.

Use the remote provider only when you intend to send requirement text and the repository path inventory to the configured endpoint:

```powershell
$env:OPENAI_API_KEY = "your-key"
$env:OPENAI_MODEL = "gpt-4o-mini"
$env:PYTHONPATH = "src"
python -m agentic_se demo --scenario greenfield --provider openai-compatible
```

The CLI requires a separate `ALLOW NETWORK` confirmation. Secrets are not included in workflow prompts or audit snapshots. Generated responses are untrusted and remain in the isolated workspace for review; the tool never merges or deploys them.

## URL Service

Start the service in a separate terminal:

```powershell
$env:PYTHONPATH = "src"
$env:SHORTENER_ANALYTICS_API_KEY = "replace-with-a-local-development-key"
python -m agentic_se serve
```

The service listens on `http://127.0.0.1:8000` by default. Configure `SHORTENER_HOST`, `SHORTENER_PORT`, and `SHORTENER_DATABASE` to change the bind address, port, and SQLite location.

Create a link:

```powershell
$body = @{ url = "https://example.com/article" } | ConvertTo-Json
$link = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/links" -ContentType "application/json" -Body $body
$link
```

Visit `$link.short_url` to receive a `302` redirect. `POST /api/v1/links` accepts `url`, optional `custom_code`, and optional timezone-aware ISO-8601 `expires_at`. An optional `Idempotency-Key` header makes repeated create requests return the original link. Duplicate custom codes return `409`; invalid URLs return `422`.

Read analytics:

```powershell
$headers = @{ Authorization = "Bearer $env:SHORTENER_ANALYTICS_API_KEY" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/links/$($link.code)/analytics" -Headers $headers
```

Analytics returns total and daily clicks. If no analytics key is configured, that endpoint returns `503` instead of becoming public. Click storage excludes IP addresses and full user-agent strings. Link creation is limited to 60 requests/minute per client and redirects to 120 requests/minute per client in this single-process prototype; these transient counters are not persisted.

## Tests and Run History

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m agentic_se runs
python -m agentic_se show <run-id>
```

The suite covers dependency validation, parallel DAG synchronization, repair/retry/fallback behavior, approval enforcement, re-planning, secret redaction, workspace path containment and rollback, URL creation/redirect/expiry/idempotency/analytics, and all three SDLC scenarios. Scenario validation runs the focused engine and shortener integration suites inside the run sandbox.

## Design and Controls

See [DESIGN.md](DESIGN.md) for the full architecture, graph and state model, lifecycle gates, risks, assumptions, metrics, and scenario rationale.

- Normal dependencies are acyclic; repair and re-plan are explicit bounded control transitions.
- Independent checks run concurrently and synchronize before integration validation.
- Write actions require an approved plan and declared in-scope paths. Elevated actions require their own human approval.
- Approval is revision- and scope-bound. Re-planning invalidates prior outputs and requires approval.
- The workspace is a per-run copy. Path traversal and symlink escapes are rejected; rollback affects only that sandbox.
- Commands use argument arrays and bounded timeouts. The remote provider is opt-in and requires explicit data-egress consent.
- Audit events and sanitized context snapshots persist in SQLite. Metrics include success rate, retries, rollbacks, mean time to recovery, and end-to-end latency.

## Limitations

The stub provider produces a deterministic code artifact for repeatable demos, not a complete product implementation. The optional remote provider returns a bounded file manifest; the orchestrator validates each path and applies it only inside the isolated sandbox after plan approval. The model has no direct filesystem or shell access. A human must inspect and manually promote acceptable changes. Run storage and rate controls are single-machine prototype components; there is no distributed scheduler, automatic merge, or deployment integration. The brownfield failure is a deterministic demonstration signal rather than a mutation of the base repository.
