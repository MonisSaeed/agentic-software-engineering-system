# Operational Runbook

This runbook covers local setup, test verification, scenario execution, run inspection, audit export, and common safe-stop recovery. Commands assume PowerShell and the repository root.

## Prerequisites and Environment

- Python 3.11 or newer on `PATH`.
- No third-party dependencies are required for the stub scenarios, HTTP service, or tests.
- Run each command from the repository root so `.agentic_se/` and scenario source paths resolve consistently.

Set the source import path in the active PowerShell session:

```powershell
$env:PYTHONPATH = "src"
```

This environment setting is session-local. Set it again in every new terminal.

## Verify the Test Suite

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The suite covers workflow graph validation and synchronization, retries/fallbacks/rollback, approvals and replanning, secret redaction, sandbox path restrictions, scenario behavior, and URL creation, redirects, expiry, idempotency, analytics, and rate limiting.

### Windows SQLite Temporary-Directory Cleanup

On Windows, SQLite or HTTP response handles can temporarily keep a test database file locked during temporary-directory cleanup. `tests/test_shortener.py` currently shuts down and closes the HTTP server, joins its worker thread, forces garbage collection, and retries `TemporaryDirectory.cleanup()` after a `PermissionError`. The test's direct SQLite connection uses a context manager; in Python that commits/rolls back but does not itself guarantee connection closure. The current teardown therefore relies on server shutdown, garbage collection, and cleanup retry rather than explicitly closing all database connections in `tearDown()`.

If cleanup still fails, close any process using the test database, rerun the test suite, and remove a leftover temporary directory only after confirming no test process is active. Do not delete the workflow state directory as a test-cleanup workaround; it contains audit evidence.

## Run Scenarios

Each demo runs interactively for human approval. Keep stdin attached to a real terminal; do not run these commands from a task/output pane or pipe input into them.

### Greenfield

```powershell
$env:PYTHONPATH = "src"
python -m agentic_se demo --scenario greenfield --provider stub
```

Review the plan, generated proposal, and review material. At each approval prompt, enter the exact uppercase string `APPROVE`, then provide a brief rationale. A non-approval response rejects the gate and safe-stops the run.

### Brownfield

```powershell
$env:PYTHONPATH = "src"
python -m agentic_se demo --scenario brownfield --provider stub
```

This scenario records a deterministic first-round expired-link failure signal, rolls the sandbox back to the approved-plan checkpoint, and reruns implementation before real shortener integration tests. See the brownfield fidelity note in [ARCHITECTURE_AND_SUMMARY.md](ARCHITECTURE_AND_SUMMARY.md): the current implementation does not mutate `service.py` to inject the regression.

### Ambiguous Requirement: Safe-Stop

```powershell
$env:PYTHONPATH = "src"
python -m agentic_se demo --scenario ambiguous --provider stub
```

At the clarification prompt, press Enter without an answer to exercise the safe-stop path. The system records the unresolved requirements and stops before planning/implementation. Then approve or reject any subsequent prompt only after it appears.

### Ambiguous Requirement: Resume with Clarification

```powershell
$env:PYTHONPATH = "src"
python -m agentic_se demo --scenario ambiguous --provider stub --answer "Track total and daily click counts; no personal identifiers; protect analytics with an API key."
```

The answer establishes click-count scope, excludes personal identifiers, and requires an analytics API key. The run requests approval for the replan, advances to graph revision 2, and then proceeds through the normal lifecycle approvals.

## Interactive Input and Terminal Buffering

Approval is deliberately terminal-driven and fail-closed. `_approval_prompt` checks that stdin is a TTY. If the process is non-interactive, it records no human reviewer and refuses to proceed. Piped input, output panes, task runners, or terminal-buffer timing can cause an approval response to be consumed before the approval prompt is waiting for it.

Operationally:

1. Launch the command directly in an integrated or external PowerShell terminal.
2. Wait until the full `Review the displayed plan/diff, then type APPROVE to continue:` prompt is visible.
3. Only then type `APPROVE` and press Enter. Any other spelling, casing, or response is not approval.
4. Enter a rationale when asked. Do not pre-type approval text or press Enter speculatively while output is still arriving.
5. For ambiguous intake, supply `--answer` explicitly for the clarified path; without it, answer at the clarification prompt or press Enter to safe-stop.

Use `--provider stub` for offline operation. For the remote provider, set the required configuration and understand that requirement text and the repository path inventory are sent to the endpoint:

```powershell
$env:OPENAI_API_KEY = "<set securely in this shell>"
$env:OPENAI_MODEL = "gpt-4o-mini"
$env:PYTHONPATH = "src"
python -m agentic_se demo --scenario greenfield --provider openai-compatible
```

The CLI separately requires the exact confirmation `ALLOW NETWORK`. Do not put actual credentials in shell history or documentation. The remote provider is optional and unnecessary for tests or stub scenarios.

## Inspect Runs and Artifacts

List runs and current metrics:

```powershell
$env:PYTHONPATH = "src"
python -m agentic_se runs
```

Show the persisted event trace for one run:

```powershell
python -m agentic_se show <run-id>
```

Scenario review bundles are under `.agentic_se/artifacts/<run-id>/`. They may contain `summary.md`, `run.json`, `events.json`, `metrics.json`, reviews, and copied proposed changes. Sandboxed working trees and checkpoint snapshots are under `.agentic_se/workspaces/` and `.agentic_se/checkpoints/` respectively. The default workflow audit database is `.agentic_se/runs.sqlite3`.

To export the CLI's run/metrics JSON to the requested audit file:

```powershell
$env:PYTHONPATH = "src"
python -m agentic_se runs *>&1 > final_audit_summary.txt
```

The checked-in audit snapshot records 7 total runs, 3 completed, 3 safe-stopped, 1 rollback, MTTR `10.63317875` seconds (reported as approximately 10.63 seconds), success rate `0.5`, zero retries, and mean end-to-end latency `122.7480115` seconds. One run is still `running`; success rate is computed over six terminal runs, so the running entry is excluded from its denominator.

| Scenario | Run ID | Recorded outcome |
| --- | --- | --- |
| Greenfield | `e27a0476-c9ef-4e50-8ec8-6d68d4240c10` | Completed |
| Brownfield | `362ec5ec-03db-40bf-ab01-4e5e749ccd3f` | Completed; rollback count 1 |
| Ambiguous, clarified | `8f8524ec-724a-4f4e-94fe-305bdfc55a54` | Completed; graph revision 2 |
| Safe-stop example | `85fa36d9-44c6-41c3-9cfd-3d06e2c405d7` | Safe-stopped |
| Safe-stop example | `afac1748-fb9b-4237-a155-d2fc91e063d5` | Safe-stopped |

The full audit also includes safe-stopped run `f5d51106-4d9a-4316-8f18-90fe47bc1f63` and non-terminal run `1dda1830-4c67-4f8a-adee-e5f40280d8d6`.

## Run the URL Shortener

Start the service in its own terminal:

```powershell
$env:PYTHONPATH = "src"
$env:SHORTENER_ANALYTICS_API_KEY = "<local-development-key>"
python -m agentic_se serve
```

Defaults are host `127.0.0.1`, port `8000`, and database `.agentic_se/shortener.sqlite3`. Override with `SHORTENER_HOST`, `SHORTENER_PORT`, and `SHORTENER_DATABASE`. Analytics is unavailable with HTTP 503 if no key is configured.

Create a link:

```powershell
$body = @{ url = "https://example.com/article" } | ConvertTo-Json
$link = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/links" -ContentType "application/json" -Body $body
$link
```

Read click totals and daily counts:

```powershell
$headers = @{ Authorization = "Bearer $env:SHORTENER_ANALYTICS_API_KEY" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/links/$($link.code)/analytics" -Headers $headers
```

## Troubleshooting Cycle

| Symptom | Check | Recovery |
| --- | --- | --- |
| `No interactive terminal; failing closed` or safe-stop at approval | Confirm the command is running in a terminal with interactive stdin (`sys.stdin.isatty()` must be true). | Relaunch directly in PowerShell, wait for the prompt, and enter the exact approval token only when requested. |
| Approval gate rejects the decision | The response must equal uppercase `APPROVE` exactly; inspect the displayed stage/revision and artifact before deciding. | Start a new run after resolving the decision; do not treat a rejected approval as a transient error. |
| Ambiguous run stops during intake | Check whether `--answer` was supplied or a clarification was entered at the prompt. | Rerun with the documented `--answer` value, then review and approve the new revision. |
| Provider request fails | For stub mode, verify `--provider stub`; for remote mode, verify key/model/endpoint and the network-consent prompt. | Prefer the stub for deterministic offline workflow checks. Never expose the API key in artifacts or prompts. |
| Generated paths are denied | Inspect the proposed manifest and approved scope; generated implementation is constrained to `src/url_shortener/`. | Correct the scoped proposal and rerun only after reviewing the plan; do not widen path policy casually. |
| Brownfield rollback occurs | Inspect the run's ordered events and checkpoint location; one rollback is expected in the audited demonstration. | Confirm the sandbox was restored and later integration checks completed. The source repository should remain untouched. |
| Test temporary directory cannot be removed on Windows | Look for processes retaining SQLite/HTTP handles and check the teardown output. | Allow teardown cleanup/retry to complete, stop stale test processes, then rerun the suite. Avoid deleting active run state. |
| Run appears as `running` after interruption | Inspect `show <run-id>` and the artifact bundle; a process may have exited before a terminal event. | Preserve evidence, determine whether the process is still active, and start a new run rather than editing the audit row manually. |

## Data and Safety Notes

- `.agentic_se/` contains local run state, snapshots, logs, and generated artifacts; protect and retain it according to the sensitivity of the repository and requirements.
- Workflow audit SQLite currently has physical `runs` and `events` tables. Checkpoint snapshots are filesystem copies; approval lineage is recorded in context/events, not standalone SQLite tables.
- The tool does not deploy or merge. Inspect the artifact bundle and promote approved changes through the team's normal review and release controls.
- Do not place credentials or personal data in prompts. Remote provider use sends requirement content and path inventory outside the local environment after explicit consent.