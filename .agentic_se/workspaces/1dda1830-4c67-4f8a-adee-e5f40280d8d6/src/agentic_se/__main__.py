from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import shutil
import sys
import uuid
from pathlib import Path

from agent_runtime import WorkspaceSandbox, create_provider
from agent_runtime.scenarios import build_scenario, revise_scenario
from url_shortener.http import serve_from_environment
from workflow_engine import (
    ApprovalDecision,
    ApprovalRequest,
    WorkflowBlocked,
    WorkflowEngine,
    WorkflowFailed,
    WorkflowRejected,
)
from workflow_engine.store import SQLiteRunStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-se")
    subcommands = parser.add_subparsers(dest="command", required=True)

    demo = subcommands.add_parser("demo", help="run one of the assignment SDLC scenarios")
    demo.add_argument("--scenario", required=True, choices=("greenfield", "brownfield", "ambiguous"))
    demo.add_argument("--provider", choices=("stub", "openai-compatible"), default="stub")
    demo.add_argument("--answer", help="clarification for the ambiguous scenario")
    demo.add_argument("--source", type=Path, default=Path.cwd())
    demo.add_argument("--state-dir", type=Path, default=Path(".agentic_se"))

    subcommands.add_parser("serve", help="run the URL shortener HTTP service")
    runs = subcommands.add_parser("runs", help="list persisted runs and reliability metrics")
    runs.add_argument("--database", type=Path, default=Path(".agentic_se/runs.sqlite3"))
    show = subcommands.add_parser("show", help="show the event trace for a run")
    show.add_argument("run_id")
    show.add_argument("--database", type=Path, default=Path(".agentic_se/runs.sqlite3"))
    return parser


def _approval_prompt(request: ApprovalRequest, _context) -> ApprovalDecision:
    print(
        f"\nApproval required: stage={request.stage}, action={request.step_name}, "
        f"revision={request.graph_revision}"
    )
    if request.affected_paths:
        print("Affected scope: " + ", ".join(request.affected_paths))
    if request.step_name == "__replan__":
        print("Requirement context: " + str(_context.state.get("requirement", "not specified")))
    artifact_key = {
        "plan_approval": "draft_plan",
        "review_approval": "review_prepare",
        "release_readiness": "review_prepare",
    }.get(request.step_name)
    artifact = _context.outputs.get(artifact_key) if artifact_key else None
    if artifact:
        print("\n--- Review Artifact ---")
        print(str(artifact)[:8000])
        print("--- End Review Artifact ---\n")
    if not sys.stdin.isatty():
        print("No interactive terminal; failing closed.")
        return ApprovalDecision(False, "non-interactive", "No human reviewer was available")
    actor = getpass.getuser()
    approved = input("Review the displayed plan/diff, then type APPROVE to continue: ").strip() == "APPROVE"
    rationale = input("Approval rationale (brief): ").strip() if approved else "Rejected by reviewer"
    return ApprovalDecision(approved, actor, rationale)


def _write_bundle(
    state_dir: Path,
    run_id: str,
    definition,
    sandbox: WorkspaceSandbox,
    store: SQLiteRunStore,
    result=None,
    error: Exception | None = None,
) -> Path:
    bundle = state_dir / "artifacts" / run_id
    bundle.mkdir(parents=True, exist_ok=True)
    review_dir = sandbox.root / "reviews"
    if review_dir.is_dir():
        shutil.copytree(review_dir, bundle / "reviews", dirs_exist_ok=True)

    generated_files = []
    try:
        implementation = json.loads(definition.context.outputs.get("implement", "{}"))
    except (json.JSONDecodeError, TypeError):
        implementation = {}
    for item in implementation.get("files", []):
        relative = Path(item.get("path", ""))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        source_path = (sandbox.root / relative).resolve()
        if not source_path.is_relative_to(sandbox.root.resolve()) or not source_path.is_file():
            continue
        target_path = bundle / "changes" / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        generated_files.append(relative.as_posix())

    run_record = store.get_run(run_id) or {}
    events = store.get_events(run_id)
    (bundle / "events.json").write_text(
        json.dumps(events, indent=2, ensure_ascii=True, default=str), encoding="utf-8"
    )
    (bundle / "run.json").write_text(
        json.dumps(run_record, indent=2, ensure_ascii=True, default=str), encoding="utf-8"
    )

    completed = result.completed_steps if result else ()
    status = run_record.get("status", "safe_stopped")
    failures = [event for event in events if event["kind"] == "step_failed"]
    summary = [
        "# Final Engineering Summary",
        "",
        f"- Scenario: {definition.context.state['scenario']}",
        f"- Run: `{run_id}`",
        f"- Status: {status}",
        f"- Requirement: {definition.context.state['requirement']}",
        f"- Graph revision: {definition.context.graph_revision}",
        f"- Base snapshot SHA-256: {definition.context.metadata.get('base_snapshot_sha256', 'unavailable')}",
        f"- Completed nodes: {', '.join(completed) if completed else 'none'}",
        f"- Failure events: {len(failures)}",
        f"- Generated files: {', '.join(generated_files) if generated_files else 'none'}",
        "",
        "## Decisions and Assumptions",
        "",
        f"- Acceptance criteria: {definition.context.state.get('acceptance_criteria', 'Intake stopped before criteria were approved.')}",
        f"- Assumptions: {definition.context.state.get('assumptions', [])}",
        f"- Clarifications needed: {definition.context.state.get('open_questions', [])}",
        "",
        "## Validation and Risk",
        "",
        "- Validation commands and results are recorded in `events.json`.",
        "- The brownfield scenario includes a deliberate regression signal and bounded repair loop.",
        "- Generated agent output remains in the isolated run sandbox for human review; it is not merged or deployed.",
        "- A deterministic stub run validates orchestration behavior, not model quality.",
        "",
        "## Limitations",
        "",
        "- Local SQLite and a single-machine sandbox are demonstration infrastructure, not a distributed production queue.",
        "- Remote-provider output is untrusted and requires independent review and validation.",
        "- Release readiness is an approval gate only; the prototype does not publish or deploy.",
    ]
    if error:
        summary.extend(["", "## Safe Stop", "", str(error)])
    (bundle / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    (bundle / "metrics.json").write_text(
        json.dumps(store.metrics(), indent=2), encoding="utf-8"
    )
    return bundle


async def _run_demo(args: argparse.Namespace) -> int:
    state_dir = args.state_dir.resolve()
    run_id = str(uuid.uuid4())
    source = args.source.resolve()
    if not source.is_dir():
        print(f"Source directory does not exist: {source}", file=sys.stderr)
        return 2

    answer = args.answer
    if args.scenario == "ambiguous" and answer is None and sys.stdin.isatty():
        print("Clarification needed: choose analytics events, privacy boundaries, and retention.")
        answer = input("Enter the agreed minimal scope (or press Enter to safe-stop): ").strip() or None

    if args.provider == "openai-compatible":
        print("The selected provider will receive the requirement and repository path inventory.")
        if not sys.stdin.isatty() or input("Type ALLOW NETWORK to continue: ").strip() != "ALLOW NETWORK":
            print("Remote provider use was not approved; safe stop.")
            return 2
    try:
        provider = create_provider(args.provider)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    sandbox_root = state_dir / "workspaces" / run_id
    sandbox = WorkspaceSandbox(source, sandbox_root)
    definition = build_scenario(
        args.scenario,
        provider,
        sandbox,
        answer=answer,
        run_id=run_id,
    )
    definition.context.state["sandbox_root"] = str(sandbox.root)
    definition.context.metadata.update(
        {
            "source_path": str(source),
            "base_snapshot_sha256": sandbox.fingerprint(),
            "provider": args.provider,
        }
    )
    store = SQLiteRunStore(state_dir / "runs.sqlite3")
    engine = WorkflowEngine(
        approval_handler=_approval_prompt,
        replan_handler=revise_scenario,
        rollback_handler=lambda _context, _target: sandbox.rollback(),
        run_store=store,
    )

    failure = None
    result = None
    try:
        result = await engine.run(definition.workflow, context=definition.context)
    except (WorkflowBlocked, WorkflowFailed, WorkflowRejected) as error:
        failure = error
        print(f"Run stopped safely: {error}")

    bundle = _write_bundle(state_dir, run_id, definition, sandbox, store, result, failure)
    print(f"Run ID: {run_id}")
    print(f"Status: {(store.get_run(run_id) or {}).get('status', 'not-recorded')}")
    print(f"Review bundle: {bundle}")
    print("Metrics: " + json.dumps(store.metrics(), sort_keys=True))
    return 0 if result is not None else 1


def main() -> int:
    args = _parser().parse_args()
    if args.command == "serve":
        serve_from_environment()
        return 0
    if args.command == "demo":
        return asyncio.run(_run_demo(args))
    store = SQLiteRunStore(args.database)
    if args.command == "runs":
        print(json.dumps({"runs": store.list_runs(), "metrics": store.metrics()}, indent=2))
        return 0
    events = store.get_events(args.run_id)
    print(json.dumps(events, indent=2))
    return 0 if events else 1


if __name__ == "__main__":
    raise SystemExit(main())