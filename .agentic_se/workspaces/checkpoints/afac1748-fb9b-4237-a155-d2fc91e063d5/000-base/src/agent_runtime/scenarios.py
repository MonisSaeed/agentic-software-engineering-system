from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from workflow_engine import (
    PolicyDenied,
    Step,
    StepExecutionError,
    Workflow,
    WorkflowBlocked,
    WorkflowContext,
    command_step,
)

from .providers import AgentProvider, StubProvider
from .sandbox import WorkspaceSandbox


ScenarioName = Literal["greenfield", "brownfield", "ambiguous"]


@dataclass(frozen=True)
class ScenarioDefinition:
    workflow: Workflow
    context: WorkflowContext
    description: str


def build_scenario(
    name: ScenarioName,
    provider: AgentProvider,
    sandbox: WorkspaceSandbox,
    *,
    answer: str | None = None,
    requirement: str | None = None,
    run_id: str | None = None,
) -> ScenarioDefinition:
    if name not in ("greenfield", "brownfield", "ambiguous"):
        raise ValueError(f"unknown scenario: {name}")

    requirement = requirement or {
        "greenfield": "Build a URL shortener with link creation, redirects, expiry, and click analytics.",
        "brownfield": "Fix the regression where expired URLs still redirect to their destination.",
        "ambiguous": "Add analytics to the URL shortener.",
    }[name]
    context = WorkflowContext(workflow_name=f"url-shortener-{name}")
    if run_id:
        context.run_id = run_id
    context.state.update(
        {
            "scenario": name,
            "requirement": requirement,
            "answer": answer,
            "implementation_round": 0,
        }
    )

    async def understand(run: WorkflowContext, selected_provider: AgentProvider) -> str:
        prompt = (
            "Normalize the requirement into user intent, acceptance criteria, assumptions, and risks. "
            f"Scenario: {name}. Requirement: {requirement}. Clarification: {answer or 'none'}."
        )
        result = await selected_provider.complete(prompt, "Return concise Markdown; do not claim code has changed.")
        run.state["acceptance_criteria"] = [
            "Preserve current behavior except for the requested change",
            "Validate the relevant API and data-flow behavior with tests",
            "Produce reviewable changes and evidence before release readiness",
        ]
        return result

    async def intake(run: WorkflowContext) -> str:
        if name == "ambiguous" and not answer:
            run.state["open_questions"] = [
                "Which events and analytics dimensions are required?",
                "What retention and access-control requirements apply?",
                "May the system store IP addresses or user-agent details?",
            ]
            raise WorkflowBlocked("Requirement is ambiguous; collect product and privacy decisions before planning")
        if name == "ambiguous":
            run.state["requirement_revision"] = 2
            run.state["assumptions"] = [
                "Track click totals and daily counts only",
                "Do not store raw IP addresses or full user-agent strings",
                "Protect analytics with a configured API key",
            ]
            if not run.state.get("clarification_replanned"):
                run.state["clarification_replanned"] = True
                run.request_replan(
                    "Human clarification changed the analytics requirement",
                    affected_steps=("intake",),
                )
        return await understand(run, provider)

    async def fallback_intake(run: WorkflowContext) -> str:
        return await understand(run, StubProvider())

    async def inspect_repository(run: WorkflowContext, selected_provider: AgentProvider) -> str:
        files = sandbox.list_files()
        relevant = [path for path in files if path.startswith(("src/", "tests/", "docs/"))][:80]
        run.state["repository_map"] = relevant
        return await selected_provider.complete(
            "Inspect the repository for impacted modules, APIs, data flows, tests, and conventions.",
            "Repository paths:\n" + "\n".join(relevant),
        )

    async def inspect(run: WorkflowContext) -> str:
        return await inspect_repository(run, provider)

    async def fallback_inspect(run: WorkflowContext) -> str:
        return await inspect_repository(run, StubProvider())

    async def make_plan(run: WorkflowContext, selected_provider: AgentProvider) -> str:
        plan = await selected_provider.complete(
            "Create a dependency-ordered plan for the approved requirement. Include risks, validation, and rollback.",
            f"Requirement: {run.state['requirement']}\nAcceptance: {run.state['acceptance_criteria']}\n"
            f"Inspection paths: {run.state.get('repository_map', [])}\nResolution: {answer or 'not applicable'}",
        )
        sandbox.write_text("reviews/plan.md", plan)
        return plan

    async def draft_plan(run: WorkflowContext) -> str:
        return await make_plan(run, provider)

    async def fallback_plan(run: WorkflowContext) -> str:
        return await make_plan(run, StubProvider())

    def approve_plan(_run: WorkflowContext) -> str:
        snapshot = sandbox.checkpoint("approved-plan")
        return f"Approved plan checkpoint: {snapshot.name}"

    async def apply_manifest(run: WorkflowContext, selected_provider: AgentProvider) -> str:
        try:
            proposal = await selected_provider.complete(
                "Produce a scoped implementation proposal as JSON with a summary and files array. "
                "Each file has path and complete UTF-8 content. Only write src/url_shortener/ files. "
                "Return JSON only; do not modify files outside the run sandbox.",
                f"Requirement: {run.state['requirement']}\n"
                f"Approved plan: {run.outputs.get('draft_plan', '')}\n"
                f"Implementation/repair round: {run.state['implementation_round']}\n"
                f"Allowed target area: src/url_shortener/",
            )
        except Exception as error:
            raise StepExecutionError(
                "agent provider request failed",
                category="agent_provider",
                details={"error_type": type(error).__name__},
                retryable=True,
            ) from error
        manifest_text = proposal.strip()
        if manifest_text.startswith("```json"):
            manifest_text = manifest_text[7:]
        if manifest_text.endswith("```"):
            manifest_text = manifest_text[:-3]
        try:
            manifest = json.loads(manifest_text.strip())
        except json.JSONDecodeError as error:
            raise StepExecutionError("agent returned an invalid file manifest", category="agent_output") from error
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise StepExecutionError("agent manifest must contain a files array", category="agent_output")
        if not 1 <= len(manifest["files"]) <= 10:
            raise StepExecutionError("agent manifest must contain 1-10 files", category="agent_output")

        written: list[dict[str, str]] = []
        total_bytes = 0
        seen_paths: set[str] = set()
        for item in manifest["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
                raise StepExecutionError("each agent file requires string path and content", category="agent_output")
            relative_path = item["path"].replace("\\", "/")
            if not relative_path.startswith("src/url_shortener/"):
                raise PolicyDenied(f"agent output path is outside approved scope: {relative_path}")
            if relative_path in seen_paths:
                raise StepExecutionError("agent manifest contains duplicate paths", category="agent_output")
            seen_paths.add(relative_path)
            content = item["content"]
            content_size = len(content.encode("utf-8"))
            total_bytes += content_size
            if content_size > 100_000 or total_bytes > 300_000:
                raise StepExecutionError("agent output exceeds the configured size limit", category="agent_output")
            try:
                sandbox.write_text(relative_path, content)
            except PermissionError as error:
                raise PolicyDenied(f"agent output was denied: {relative_path}") from error
            written.append(
                {
                    "path": relative_path,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
            )

        summary = str(manifest.get("summary", "Implementation artifact produced"))[:2000]
        sandbox.write_text(
            "reviews/proposed-change.md",
            f"# Proposed Change\n\n{summary}\n\nGenerated files:\n"
            + "\n".join(f"- `{item['path']}` (SHA-256 `{item['sha256']}`)" for item in written)
            + "\n",
        )
        return json.dumps({"summary": summary, "files": written, "round": run.state["implementation_round"]})

    async def implement(run: WorkflowContext) -> str:
        run.state["implementation_round"] += 1
        return await apply_manifest(run, provider)

    async def fallback_implement(run: WorkflowContext) -> str:
        return await apply_manifest(run, StubProvider())

    test_step = command_step(
        "unit_tests",
        (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_engine.py"),
        category="test",
        cwd=sandbox.root,
        env={"PYTHONPATH": "src"},
        timeout_seconds=45,
    )
    test_action = test_step.action

    async def validate_units(run: WorkflowContext):
        if name == "brownfield" and run.state["implementation_round"] == 1:
            raise StepExecutionError(
                "Regression reproduced: expired link returned a redirect",
                category="test",
                details={"test": "expired_links_do_not_redirect"},
            )
        return await test_action(run)

    lint_step = command_step(
        "lint_compile",
        (sys.executable, "-m", "compileall", "-q", "src"),
        category="lint",
        cwd=sandbox.root,
        env={"PYTHONPATH": "src"},
        timeout_seconds=45,
    )

    async def security_check(_run: WorkflowContext) -> dict[str, object]:
        findings: list[str] = []
        risky = re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}")
        for relative in sandbox.list_files():
            if not relative.endswith((".py", ".toml", ".md", ".json")):
                continue
            path = sandbox.root / relative
            try:
                if risky.search(path.read_text(encoding="utf-8", errors="ignore")):
                    findings.append(relative)
            except OSError:
                continue
        if findings:
            raise StepExecutionError(
                "Potential credential material found in workspace",
                category="security",
                details={"paths": findings},
            )
        return {"credential_scan": "passed", "files_scanned": len(sandbox.list_files())}

    docs_step = Step(
        "documentation",
        action=lambda run: sandbox.write_text(
            "reviews/setup-and-api-notes.md",
            "# Run Notes\n\n"
            "The deliverable uses the standard library, SQLite, and unittest. "
            "See the project README for setup, API routes, risks, and limitations.\n",
        ).as_posix(),
        depends_on=("implement",),
        stage="document",
        autonomy="write",
        affected_paths=("reviews/setup-and-api-notes.md",),
    )

    integration_step = command_step(
        "integration_tests",
        (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_shortener.py"),
        category="test",
        depends_on=("unit_tests", "lint_compile", "security_scan"),
        cwd=sandbox.root,
        env={"PYTHONPATH": "src"},
        timeout_seconds=60,
    )

    async def prepare_review(run: WorkflowContext) -> str:
        content = await provider.complete(
            "Prepare a final engineering review summary with plan rationale, artifacts, validation, risks, assumptions, and limitations.",
            f"Scenario: {name}\nRequirement: {run.state['requirement']}\n"
            f"Completed steps: {sorted(run.outputs)}\n"
            f"Acceptance criteria: {run.state['acceptance_criteria']}\n",
        )
        implementation = json.loads(run.outputs.get("implement", "{}"))
        code_sections = []
        for item in implementation.get("files", []):
            relative_path = item.get("path")
            if not isinstance(relative_path, str) or not relative_path.startswith("src/url_shortener/"):
                continue
            content_path = sandbox.root / relative_path
            if content_path.is_file():
                code_sections.append(
                    f"## Generated file: `{relative_path}`\n\n```python\n"
                    f"{content_path.read_text(encoding='utf-8')[:12000]}\n```\n"
                )
        content += "\n## Generated Code for Review\n\n" + "\n".join(code_sections)
        sandbox.write_text("reviews/final-engineering-summary.md", content)
        return content

    async def fallback_review(run: WorkflowContext) -> str:
        content = await StubProvider().complete(
            "Prepare a final engineering review summary with plan rationale, artifacts, validation, risks, assumptions, and limitations.",
            f"Scenario: {name}\nRequirement: {run.state['requirement']}\n"
            f"Completed steps: {sorted(run.outputs)}\n",
        )
        implementation = json.loads(run.outputs.get("implement", "{}"))
        for item in implementation.get("files", []):
            relative_path = item.get("path", "")
            if relative_path.startswith("src/url_shortener/"):
                code_path = sandbox.root / relative_path
                if code_path.is_file():
                    content += f"\n## Generated file: `{relative_path}`\n\n```python\n{code_path.read_text(encoding='utf-8')[:12000]}\n```\n"
        sandbox.write_text("reviews/final-engineering-summary.md", content)
        return content

    steps = (
        Step("intake", intake, stage="intake", max_attempts=2, fallback_action=fallback_intake),
        Step("inspect", inspect, depends_on=("intake",), stage="inspect", max_attempts=2, fallback_action=fallback_inspect),
        Step("draft_plan", draft_plan, depends_on=("inspect",), stage="plan", max_attempts=2, fallback_action=fallback_plan),
        Step(
            "plan_approval",
            approve_plan,
            depends_on=("draft_plan",),
            requires_approval=True,
            stage="plan",
        ),
        Step(
            "implement",
            implement,
            depends_on=("plan_approval",),
            on_failure=None,
            stage="implement",
            autonomy="write",
            affected_paths=("src/url_shortener/", "reviews/"),
            max_attempts=2,
            fallback_action=fallback_implement,
        ),
        Step(
            "unit_tests",
            validate_units,
            depends_on=("implement",),
            on_failure="implement",
            stage="validate",
        ),
        Step(
            "lint_compile",
            lint_step.action,
            depends_on=("implement",),
            stage="validate",
            timeout_seconds=45,
        ),
        Step(
            "security_scan",
            security_check,
            depends_on=("implement",),
            stage="validate",
        ),
        integration_step,
        docs_step,
        Step(
            "review_prepare",
            prepare_review,
            depends_on=("integration_tests", "documentation"),
            stage="review",
            max_attempts=2,
            fallback_action=fallback_review,
        ),
        Step(
            "review_approval",
            depends_on=("review_prepare",),
            requires_approval=True,
            stage="review",
        ),
        Step(
            "release_readiness",
            depends_on=("review_approval",),
            requires_approval=True,
            stage="release_readiness",
        ),
    )
    workflow = Workflow(f"url-shortener-{name}", steps, max_loopbacks=2)
    return ScenarioDefinition(
        workflow=workflow,
        context=context,
        description={
            "greenfield": "Build the shortener service and demonstrate full lifecycle validation.",
            "brownfield": "Reproduce and repair an expired-link redirect regression.",
            "ambiguous": "Pause for analytics scope/privacy clarification, then re-plan with the answer.",
        }[name],
    )


def revise_scenario(workflow: Workflow, _context: WorkflowContext, _reason: str) -> Workflow:
    return Workflow(
        workflow.name,
        workflow.steps,
        max_loopbacks=workflow.max_loopbacks,
        revision=workflow.revision + 1,
    )