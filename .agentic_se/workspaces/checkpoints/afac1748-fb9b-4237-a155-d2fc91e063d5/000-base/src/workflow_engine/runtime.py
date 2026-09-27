from __future__ import annotations

import asyncio
import inspect
import os
import secrets
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence


Stage = Literal[
    "intake",
    "inspect",
    "plan",
    "implement",
    "validate",
    "document",
    "review",
    "release_readiness",
]
Autonomy = Literal["read", "write", "elevated"]


class WorkflowError(RuntimeError):
    pass


class WorkflowDefinitionError(WorkflowError):
    pass


class ApprovalRequired(WorkflowError):
    pass


class WorkflowRejected(WorkflowError):
    pass


class WorkflowBlocked(WorkflowError):
    """A safe stop that needs human clarification or intervention."""


class PolicyDenied(WorkflowError):
    pass


class WorkflowFailed(WorkflowError):
    def __init__(self, step_name: str, cause: Exception) -> None:
        super().__init__(f"Workflow failed at step '{step_name}': {cause}")
        self.step_name = step_name
        self.cause = cause


class StepExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "step",
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.details = dict(details or {})
        self.retryable = retryable


StepAction = Callable[["WorkflowContext"], Any | Awaitable[Any]]
EventListener = Callable[["ExecutionEvent"], Any | Awaitable[Any]]
ApprovalHandler = Callable[["ApprovalRequest", "WorkflowContext"], Any]
ReplanHandler = Callable[["Workflow", "WorkflowContext", str], Any]
RollbackHandler = Callable[["WorkflowContext", str], Any | Awaitable[Any]]


@dataclass(frozen=True)
class Step:
    name: str
    action: StepAction | None = None
    depends_on: tuple[str, ...] = ()
    on_failure: str | None = None
    max_attempts: int = 1
    requires_approval: bool = False
    stage: Stage = "implement"
    autonomy: Autonomy = "read"
    affected_paths: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    fallback_action: StepAction | None = None
    idempotent: bool = True


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step, ...]
    max_loopbacks: int = 2
    revision: int = 1

    @property
    def entry_steps(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps if not step.depends_on)

    @property
    def exit_steps(self) -> tuple[str, ...]:
        depended_on = {dependency for step in self.steps for dependency in step.depends_on}
        return tuple(step.name for step in self.steps if step.name not in depended_on)


@dataclass
class WorkflowContext:
    workflow_name: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    graph_revision: int = 1
    state: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    lineage: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    replan_reason: str | None = None
    replan_affected_steps: tuple[str, ...] = ()

    def request_replan(self, reason: str, *, affected_steps: Sequence[str] = ()) -> None:
        if not reason.strip():
            raise ValueError("A re-plan request must include a reason")
        self.replan_reason = reason.strip()
        self.replan_affected_steps = tuple(dict.fromkeys(affected_steps))


@dataclass(frozen=True)
class ApprovalRequest:
    workflow_name: str
    run_id: str
    step_name: str
    attempt: int
    graph_revision: int
    stage: str
    affected_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    actor: str
    rationale: str = ""


@dataclass(frozen=True)
class ExecutionEvent:
    kind: str
    workflow_name: str
    run_id: str
    timestamp: str
    graph_revision: int
    step_name: str | None = None
    attempt: int | None = None
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    context: WorkflowContext
    completed_steps: tuple[str, ...]
    statuses: Mapping[str, str]
    loopbacks: int
    events: tuple[ExecutionEvent, ...]


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str


class DefaultPolicy:
    """Fail-closed checks for high-impact workflow steps."""

    def __init__(
        self,
        allowed_write_prefixes: tuple[str, ...] = ("src/", "tests/", "docs/", "reviews/", "examples/"),
    ) -> None:
        self.allowed_write_prefixes = allowed_write_prefixes

    def authorize(self, step: Step, context: WorkflowContext) -> None:
        if step.autonomy == "elevated" and not step.requires_approval:
            raise PolicyDenied(f"Elevated step '{step.name}' has no approval checkpoint")
        if step.autonomy in ("write", "elevated") and not context.state.get("plan_approved"):
            raise PolicyDenied(f"Write step '{step.name}' requires an approved plan")
        if step.autonomy in ("write", "elevated"):
            if not step.affected_paths:
                raise PolicyDenied(f"Write step '{step.name}' must declare affected paths")
            for affected_path in step.affected_paths:
                normalized = affected_path.replace("\\", "/")
                path = PurePosixPath(normalized)
                if path.is_absolute() or ".." in path.parts:
                    raise PolicyDenied(f"Write step '{step.name}' declares an unsafe path")
                if not any(normalized.startswith(prefix) for prefix in self.allowed_write_prefixes):
                    raise PolicyDenied(f"Write step '{step.name}' exceeds its approved path scope")
        if step.stage == "release_readiness" and not step.requires_approval:
            raise PolicyDenied("Release readiness must have a human approval checkpoint")


def command_step(
    name: str,
    command: Sequence[str],
    *,
    category: Literal["test", "build", "lint", "security"] = "build",
    depends_on: tuple[str, ...] = (),
    on_failure: str | None = None,
    max_attempts: int = 1,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 60.0,
) -> Step:
    """Create a shell-free process step; nonzero exits become structured failures."""
    if not command:
        raise ValueError("command must contain an executable")
    args = tuple(str(part) for part in command)

    async def run(_: WorkflowContext) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env={**os.environ, **env} if env is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as error:
            process.kill()
            await process.communicate()
            raise StepExecutionError(
                f"{category} command timed out after {timeout_seconds}s",
                category=category,
                details={"command": args, "timeout_seconds": timeout_seconds},
                retryable=True,
            ) from error
        result = CommandResult(
            args,
            process.returncode,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )
        if result.return_code != 0:
            raise StepExecutionError(
                f"{category} command exited with status {result.return_code}",
                category=category,
                details={
                    "command": args,
                    "return_code": result.return_code,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-4000:],
                },
            )
        return result

    return Step(
        name=name,
        action=run,
        depends_on=depends_on,
        on_failure=on_failure,
        max_attempts=max_attempts,
        stage="validate" if category in ("test", "lint", "security") else "implement",
        timeout_seconds=timeout_seconds,
    )


class WorkflowEngine:
    def __init__(
        self,
        *,
        event_listener: EventListener | None = None,
        approval_handler: ApprovalHandler | None = None,
        replan_handler: ReplanHandler | None = None,
        rollback_handler: RollbackHandler | None = None,
        policy: DefaultPolicy | None = None,
        run_store: Any | None = None,
        max_parallel_steps: int = 4,
    ) -> None:
        if max_parallel_steps < 1:
            raise ValueError("max_parallel_steps must be positive")
        self.event_listener = event_listener
        self.approval_handler = approval_handler
        self.replan_handler = replan_handler
        self.rollback_handler = rollback_handler
        self.policy = policy or DefaultPolicy()
        self.run_store = run_store
        self.max_parallel_steps = max_parallel_steps

    async def run(self, workflow: Workflow, *, context: WorkflowContext | None = None) -> ExecutionResult:
        ordered = self._validate_and_order(workflow)
        context = context or WorkflowContext(
            workflow_name=workflow.name, graph_revision=workflow.revision
        )
        statuses = {step.name: "pending" for step in ordered}
        events: list[ExecutionEvent] = []
        loopbacks = 0

        async def emit(
            kind: str,
            step: Step | None = None,
            attempt: int | None = None,
            message: str | None = None,
            details: Mapping[str, Any] | None = None,
        ) -> None:
            event = ExecutionEvent(
                kind=kind,
                workflow_name=workflow.name,
                run_id=context.run_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                graph_revision=context.graph_revision,
                step_name=step.name if step else None,
                attempt=attempt,
                message=message,
                details=dict(details or {}),
            )
            events.append(event)
            if self.run_store is not None:
                self.run_store.record_event(event, context)
            if self.event_listener is not None:
                result = self.event_listener(event)
                if inspect.isawaitable(result):
                    await result

        await emit("run_started", details={"revision": workflow.revision})
        while True:
            if context.replan_reason:
                if self.replan_handler is None:
                    raise WorkflowBlocked("Re-plan requested but no replan handler is configured")
                reason = context.replan_reason
                affected_steps = context.replan_affected_steps
                context.replan_reason = None
                context.replan_affected_steps = ()
                unknown = set(affected_steps) - {step.name for step in workflow.steps}
                if unknown:
                    raise WorkflowDefinitionError(f"Re-plan references unknown nodes: {sorted(unknown)}")
                await emit("replan_requested", message=reason, details={"affected_steps": affected_steps})
                await self._approve_replan(workflow, context, reason, emit)
                new_workflow = self.replan_handler(workflow, context, reason)
                if inspect.isawaitable(new_workflow):
                    new_workflow = await new_workflow
                if new_workflow.name != workflow.name:
                    raise WorkflowDefinitionError("A re-plan cannot change the workflow identity")
                ordered = self._validate_and_order(new_workflow)
                context.lineage.append(
                    {
                        "from_revision": context.graph_revision,
                        "to_revision": context.graph_revision + 1,
                        "reason": reason,
                        "affected_steps": affected_steps,
                        "prior_outputs": tuple(context.outputs),
                    }
                )
                context.outputs.clear()
                context.attempts.clear()
                context.state.pop("plan_approved", None)
                if hasattr(context, "_errors"):
                    context._errors.clear()
                context.graph_revision += 1
                workflow = new_workflow
                statuses = {step.name: "pending" for step in ordered}
                await emit("replanned", message=reason, details={"revision": context.graph_revision})

            failed_names = [step.name for step in ordered if statuses[step.name] == "failed"]
            if failed_names:
                failed_step = next(step for step in ordered if step.name == failed_names[0])
                error = getattr(context, "_errors", {}).get(failed_step.name, RuntimeError("step failed"))
                if failed_step.on_failure is None:
                    await emit("safe_stop", failed_step, message=str(error))
                    raise WorkflowFailed(failed_step.name, error)
                loopbacks += 1
                if loopbacks > workflow.max_loopbacks:
                    await emit("safe_stop", failed_step, message="loopback limit reached")
                    raise WorkflowFailed(failed_step.name, error)
                target = failed_step.on_failure
                if self.rollback_handler is not None:
                    rollback = self.rollback_handler(context, target)
                    if inspect.isawaitable(rollback):
                        await rollback
                invalidated = self._descendants(target, workflow.steps)
                for name in invalidated:
                    statuses[name] = "pending"
                    context.outputs.pop(name, None)
                    context.attempts.pop(name, None)
                    getattr(context, "_errors", {}).pop(name, None)
                await emit(
                    "rollback",
                    failed_step,
                    context.attempts.get(failed_step.name),
                    f"Restored checkpoint before returning to '{target}'",
                    {"target": target, "loopback": loopbacks},
                )
                await emit(
                    "loopback",
                    failed_step,
                    message=f"Returning to '{target}' after failure",
                    details={"target": target, "loopback": loopbacks},
                )
                continue

            if all(status == "succeeded" for status in statuses.values()):
                await emit("run_completed", details={"loopbacks": loopbacks})
                return ExecutionResult(
                    context,
                    tuple(step.name for step in ordered),
                    dict(statuses),
                    loopbacks,
                    tuple(events),
                )

            ready = [
                step
                for step in ordered
                if statuses[step.name] == "pending"
                and all(statuses[dependency] == "succeeded" for dependency in step.depends_on)
            ][: self.max_parallel_steps]
            if not ready:
                pending = [name for name, status in statuses.items() if status == "pending"]
                raise WorkflowDefinitionError(f"No runnable steps remain: {pending}")

            for step in ready:
                statuses[step.name] = "running"
            outcomes = await asyncio.gather(
                *(self._execute_step(step, workflow, context, emit) for step in ready),
                return_exceptions=True,
            )
            for step, outcome in zip(ready, outcomes):
                if isinstance(outcome, BaseException):
                    statuses[step.name] = "failed"
                    if not hasattr(context, "_errors"):
                        context._errors = {}
                    context._errors[step.name] = outcome
                    if isinstance(outcome, (ApprovalRequired, WorkflowRejected, WorkflowBlocked, PolicyDenied)):
                        await emit("safe_stop", step, message=str(outcome))
                        raise outcome
                else:
                    status, output = outcome
                    if status == "succeeded":
                        statuses[step.name] = status
                        context.outputs[step.name] = output
                        if step.stage == "plan" and step.requires_approval:
                            context.state["plan_approved"] = True
                        if step.stage == "release_readiness":
                            context.state["release_approved"] = True
                    else:
                        statuses[step.name] = "failed"
                        if not hasattr(context, "_errors"):
                            context._errors = {}
                        context._errors[step.name] = output

    async def _execute_step(self, step: Step, workflow: Workflow, context: WorkflowContext, emit: Any) -> tuple[str, Any]:
        try:
            self.policy.authorize(step, context)
            if step.requires_approval:
                if self.approval_handler is None:
                    raise ApprovalRequired(f"Step '{step.name}' requires a human approval handler")
                attempt = context.attempts.get(step.name, 0) + 1
                request = ApprovalRequest(
                    workflow.name,
                    context.run_id,
                    step.name,
                    attempt,
                    context.graph_revision,
                    step.stage,
                    step.affected_paths,
                )
                await emit("approval_requested", step, attempt, details={"paths": step.affected_paths})
                decision = self.approval_handler(request, context)
                if inspect.isawaitable(decision):
                    decision = await decision
                approval = decision if isinstance(decision, ApprovalDecision) else ApprovalDecision(
                    bool(decision), "approval-handler"
                )
                context.lineage.append(
                    {
                        "kind": "approval",
                        "step": step.name,
                        "revision": context.graph_revision,
                        "approved": approval.approved,
                        "actor": approval.actor,
                        "rationale": approval.rationale,
                    }
                )
                if not approval.approved:
                    await emit(
                        "approval_rejected",
                        step,
                        attempt,
                        details={"actor": approval.actor, "rationale": approval.rationale},
                    )
                    raise WorkflowRejected(f"Approval rejected at step '{step.name}'")
                await emit(
                    "approval_granted",
                    step,
                    attempt,
                    details={"actor": approval.actor, "rationale": approval.rationale},
                )

            if step.action is None:
                context.attempts[step.name] = 1
                await emit("step_started", step, 1)
                await emit("step_completed", step, 1)
                return "succeeded", None

            last_error: Exception | None = None
            for attempt in range(1, step.max_attempts + 1):
                context.attempts[step.name] = attempt
                await emit("step_started", step, attempt)
                try:
                    output = step.action(context)
                    if inspect.isawaitable(output):
                        output = await asyncio.wait_for(output, timeout=step.timeout_seconds)
                    await emit("step_completed", step, attempt)
                    return "succeeded", output
                except (ApprovalRequired, WorkflowRejected, WorkflowBlocked, PolicyDenied):
                    raise
                except Exception as error:
                    last_error = error
                    details = getattr(error, "details", {})
                    await emit(
                        "step_failed",
                        step,
                        attempt,
                        str(error),
                        {"category": getattr(error, "category", "step"), **details},
                    )
                    retryable = getattr(error, "retryable", not isinstance(error, StepExecutionError))
                    if attempt < step.max_attempts and retryable:
                        if not step.idempotent:
                            raise PolicyDenied(f"Non-idempotent step '{step.name}' cannot be retried")
                        await emit("retry_scheduled", step, attempt, details={"next_attempt": attempt + 1})
                        continue
                    break

            fallback_allowed = last_error is not None and getattr(
                last_error, "retryable", not isinstance(last_error, StepExecutionError)
            )
            if step.fallback_action is not None and fallback_allowed:
                await emit("fallback_started", step, context.attempts.get(step.name))
                try:
                    fallback_output = step.fallback_action(context)
                    if inspect.isawaitable(fallback_output):
                        fallback_output = await asyncio.wait_for(
                            fallback_output, timeout=step.timeout_seconds
                        )
                    await emit("fallback_completed", step)
                    return "succeeded", fallback_output
                except Exception as fallback_error:
                    await emit("fallback_failed", step, message=str(fallback_error))
                    last_error = fallback_error
            assert last_error is not None
            return "failed", last_error
        except Exception:
            raise

    async def _approve_replan(self, workflow: Workflow, context: WorkflowContext, reason: str, emit: Any) -> None:
        if self.approval_handler is None:
            raise ApprovalRequired("A re-plan requires human approval")
        request = ApprovalRequest(
            workflow.name,
            context.run_id,
            "__replan__",
            1,
            context.graph_revision,
            "plan",
        )
        await emit("approval_requested", message=reason, details={"scope": "replan"})
        decision = self.approval_handler(request, context)
        if inspect.isawaitable(decision):
            decision = await decision
        approval = decision if isinstance(decision, ApprovalDecision) else ApprovalDecision(
            bool(decision), "approval-handler"
        )
        context.lineage.append(
            {
                "kind": "approval",
                "step": "__replan__",
                "revision": context.graph_revision,
                "approved": approval.approved,
                "actor": approval.actor,
                "rationale": approval.rationale,
            }
        )
        if not approval.approved:
            await emit(
                "approval_rejected",
                message=reason,
                details={"scope": "replan", "actor": approval.actor, "rationale": approval.rationale},
            )
            raise WorkflowRejected("Re-plan was rejected")
        await emit(
            "approval_granted",
            message=reason,
            details={"scope": "replan", "actor": approval.actor, "rationale": approval.rationale},
        )

    @staticmethod
    def _validate_and_order(workflow: Workflow) -> list[Step]:
        if not workflow.name or workflow.revision < 1 or workflow.max_loopbacks < 0:
            raise WorkflowDefinitionError("Invalid workflow name, revision, or loopback limit")
        step_by_name: dict[str, Step] = {}
        for step in workflow.steps:
            if not step.name or step.name in step_by_name:
                raise WorkflowDefinitionError(f"Invalid or duplicate step name: '{step.name}'")
            if step.max_attempts < 1 or step.timeout_seconds <= 0:
                raise WorkflowDefinitionError(f"Invalid retry or timeout policy for '{step.name}'")
            if step.max_attempts > 1 and not step.idempotent:
                raise WorkflowDefinitionError(f"Retried step '{step.name}' must be idempotent")
            step_by_name[step.name] = step

        for step in workflow.steps:
            missing = set(step.depends_on) - step_by_name.keys()
            if missing:
                raise WorkflowDefinitionError(f"Step '{step.name}' has unknown dependencies: {sorted(missing)}")
            if step.on_failure is not None and step.on_failure not in step_by_name:
                raise WorkflowDefinitionError(f"Unknown failure target '{step.on_failure}'")

        pending = list(workflow.steps)
        ordered: list[Step] = []
        names: set[str] = set()
        while pending:
            ready = next((step for step in pending if set(step.depends_on) <= names), None)
            if ready is None:
                raise WorkflowDefinitionError("Workflow dependencies contain a cycle")
            ordered.append(ready)
            names.add(ready.name)
            pending.remove(ready)

        by_name = {step.name: step for step in ordered}
        order_index = {step.name: index for index, step in enumerate(ordered)}
        for step in ordered:
            if step.on_failure is not None:
                target = step.on_failure
                if target != step.name and target not in WorkflowEngine._ancestors(step.name, by_name):
                    raise WorkflowDefinitionError(
                        f"Failure target '{target}' for '{step.name}' must be itself or a dependency ancestor"
                    )
                if order_index[target] > order_index[step.name]:
                    raise WorkflowDefinitionError("Failure edges must point backward")
        return ordered

    @staticmethod
    def _ancestors(step_name: str, by_name: Mapping[str, Step]) -> set[str]:
        found: set[str] = set()
        pending = list(by_name[step_name].depends_on)
        while pending:
            item = pending.pop()
            if item not in found:
                found.add(item)
                pending.extend(by_name[item].depends_on)
        return found

    @staticmethod
    def _descendants(step_name: str, steps: Sequence[Step]) -> set[str]:
        result = {step_name}
        changed = True
        while changed:
            changed = False
            for step in steps:
                if step.name not in result and result.intersection(step.depends_on):
                    result.add(step.name)
                    changed = True
        return result