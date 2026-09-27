from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence


class WorkflowError(RuntimeError):
    """Base class for workflow definition and execution errors."""


class WorkflowDefinitionError(WorkflowError):
    pass


class ApprovalRequired(WorkflowError):
    pass


class WorkflowRejected(WorkflowError):
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
    ) -> None:
        super().__init__(message)
        self.category = category
        self.details = dict(details or {})


StepAction = Callable[["WorkflowContext"], Any | Awaitable[Any]]
EventListener = Callable[["ExecutionEvent"], Any | Awaitable[Any]]
ApprovalHandler = Callable[
    ["ApprovalRequest", "WorkflowContext"], bool | Awaitable[bool]
]


@dataclass(frozen=True)
class Step:
    name: str
    action: StepAction | None = None
    depends_on: tuple[str, ...] = ()
    on_failure: str | None = None
    max_attempts: int = 1
    requires_approval: bool = False


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step, ...]
    max_loopbacks: int = 3


@dataclass
class WorkflowContext:
    workflow_name: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalRequest:
    workflow_name: str
    run_id: str
    step_name: str
    attempt: int


@dataclass(frozen=True)
class ExecutionEvent:
    kind: str
    workflow_name: str
    run_id: str
    step_name: str | None = None
    attempt: int | None = None
    message: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    context: WorkflowContext
    completed_steps: tuple[str, ...]
    loopbacks: int
    events: tuple[ExecutionEvent, ...]


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str


def command_step(
    name: str,
    command: Sequence[str],
    *,
    category: Literal["test", "build"] = "build",
    depends_on: tuple[str, ...] = (),
    on_failure: str | None = None,
    max_attempts: int = 1,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Step:
    """Create a step that runs a process and reports nonzero exits as failures."""
    if not command:
        raise ValueError("command must contain an executable")
    command_args = tuple(str(part) for part in command)

    async def run(_: WorkflowContext) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *command_args,
            cwd=cwd,
            env={**os.environ, **env} if env is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        result = CommandResult(
            command=command_args,
            return_code=process.returncode,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
        )
        if result.return_code != 0:
            raise StepExecutionError(
                f"{category} command exited with status {result.return_code}",
                category=category,
                details={
                    "command": command_args,
                    "return_code": result.return_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )
        return result

    return Step(
        name=name,
        action=run,
        depends_on=depends_on,
        on_failure=on_failure,
        max_attempts=max_attempts,
    )


class WorkflowEngine:
    def __init__(
        self,
        *,
        event_listener: EventListener | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self.event_listener = event_listener
        self.approval_handler = approval_handler

    async def run(self, workflow: Workflow) -> ExecutionResult:
        ordered_steps = self._validate_and_order(workflow)
        position = {step.name: index for index, step in enumerate(ordered_steps)}
        context = WorkflowContext(workflow_name=workflow.name)
        completed: set[str] = set()
        events: list[ExecutionEvent] = []
        loopbacks = 0
        cursor = 0

        async def emit(event: ExecutionEvent) -> None:
            events.append(event)
            if self.event_listener is not None:
                response = self.event_listener(event)
                if inspect.isawaitable(response):
                    await response

        while cursor < len(ordered_steps):
            step = ordered_steps[cursor]
            if step.name in completed:
                cursor += 1
                continue

            attempt = context.attempts.get(step.name, 0) + 1
            context.attempts[step.name] = attempt
            await emit(ExecutionEvent("step_started", workflow.name, context.run_id, step.name, attempt))

            if step.requires_approval:
                if self.approval_handler is None:
                    raise ApprovalRequired(
                        f"Step '{step.name}' requires an approval handler"
                    )
                request = ApprovalRequest(workflow.name, context.run_id, step.name, attempt)
                await emit(ExecutionEvent("approval_requested", workflow.name, context.run_id, step.name, attempt))
                decision = self.approval_handler(request, context)
                if inspect.isawaitable(decision):
                    decision = await decision
                if not decision:
                    await emit(ExecutionEvent("approval_rejected", workflow.name, context.run_id, step.name, attempt))
                    raise WorkflowRejected(f"Approval rejected at step '{step.name}'")
                await emit(ExecutionEvent("approval_granted", workflow.name, context.run_id, step.name, attempt))

            try:
                if step.action is None:
                    if not step.requires_approval:
                        raise WorkflowDefinitionError(
                            f"Step '{step.name}' needs an action or approval checkpoint"
                        )
                    output = None
                else:
                    output = step.action(context)
                    if inspect.isawaitable(output):
                        output = await output
                context.outputs[step.name] = output
                completed.add(step.name)
                await emit(ExecutionEvent("step_completed", workflow.name, context.run_id, step.name, attempt))
                cursor += 1
            except Exception as error:
                details = getattr(error, "details", {})
                category = getattr(error, "category", "step")
                await emit(
                    ExecutionEvent(
                        "step_failed",
                        workflow.name,
                        context.run_id,
                        step.name,
                        attempt,
                        str(error),
                        {"category": category, **details},
                    )
                )
                if attempt < step.max_attempts:
                    continue
                if step.on_failure is None:
                    raise WorkflowFailed(step.name, error) from error
                loopbacks += 1
                if loopbacks > workflow.max_loopbacks:
                    raise WorkflowFailed(step.name, error) from error

                target = step.on_failure
                invalidated = self._descendants(target, workflow.steps)
                for invalidated_name in invalidated:
                    completed.discard(invalidated_name)
                    context.outputs.pop(invalidated_name, None)
                    context.attempts.pop(invalidated_name, None)
                await emit(
                    ExecutionEvent(
                        "loopback",
                        workflow.name,
                        context.run_id,
                        step.name,
                        attempt,
                        f"Returning to '{target}' after {category} failure",
                        {"target": target, "loopback": loopbacks, "category": category},
                    )
                )
                cursor = position[target]

        return ExecutionResult(
            context=context,
            completed_steps=tuple(step.name for step in ordered_steps if step.name in completed),
            loopbacks=loopbacks,
            events=tuple(events),
        )

    @staticmethod
    def _validate_and_order(workflow: Workflow) -> list[Step]:
        if not workflow.name:
            raise WorkflowDefinitionError("Workflow name cannot be empty")
        if workflow.max_loopbacks < 0:
            raise WorkflowDefinitionError("max_loopbacks cannot be negative")
        step_by_name: dict[str, Step] = {}
        for step in workflow.steps:
            if not step.name or step.name in step_by_name:
                raise WorkflowDefinitionError(f"Invalid or duplicate step name: '{step.name}'")
            if step.max_attempts < 1:
                raise WorkflowDefinitionError(f"Step '{step.name}' must allow at least one attempt")
            step_by_name[step.name] = step

        for step in workflow.steps:
            missing = set(step.depends_on) - step_by_name.keys()
            if missing:
                raise WorkflowDefinitionError(
                    f"Step '{step.name}' has unknown dependencies: {sorted(missing)}"
                )
            if step.on_failure is not None and step.on_failure not in step_by_name:
                raise WorkflowDefinitionError(
                    f"Step '{step.name}' has unknown failure target '{step.on_failure}'"
                )

        pending = list(workflow.steps)
        ordered: list[Step] = []
        ordered_names: set[str] = set()
        while pending:
            ready = next(
                (step for step in pending if set(step.depends_on) <= ordered_names),
                None,
            )
            if ready is None:
                raise WorkflowDefinitionError("Workflow dependencies contain a cycle")
            ordered.append(ready)
            ordered_names.add(ready.name)
            pending.remove(ready)

        order_index = {step.name: index for index, step in enumerate(ordered)}
        for step in workflow.steps:
            if step.on_failure is not None:
                target = step.on_failure
                if target != step.name and target not in WorkflowEngine._ancestors(step.name, step_by_name):
                    raise WorkflowDefinitionError(
                        f"Failure target '{target}' for '{step.name}' must be the step itself or an ancestor"
                    )
                if order_index[target] > order_index[step.name]:
                    raise WorkflowDefinitionError(
                        f"Failure target '{target}' must precede '{step.name}'"
                    )
        return ordered

    @staticmethod
    def _ancestors(step_name: str, step_by_name: Mapping[str, Step]) -> set[str]:
        ancestors: set[str] = set()
        pending = list(step_by_name[step_name].depends_on)
        while pending:
            dependency = pending.pop()
            if dependency not in ancestors:
                ancestors.add(dependency)
                pending.extend(step_by_name[dependency].depends_on)
        return ancestors

    @staticmethod
    def _descendants(step_name: str, steps: Sequence[Step]) -> set[str]:
        descendants = {step_name}
        changed = True
        while changed:
            changed = False
            for step in steps:
                if step.name not in descendants and descendants.intersection(step.depends_on):
                    descendants.add(step.name)
                    changed = True
        return descendants


from .runtime import (
    ApprovalDecision as ApprovalDecision,
    ApprovalRequest as ApprovalRequest,
    ApprovalRequired as ApprovalRequired,
    CommandResult as CommandResult,
    DefaultPolicy as DefaultPolicy,
    ExecutionEvent as ExecutionEvent,
    ExecutionResult as ExecutionResult,
    PolicyDenied as PolicyDenied,
    Step as Step,
    StepExecutionError as StepExecutionError,
    Workflow as Workflow,
    WorkflowBlocked as WorkflowBlocked,
    WorkflowContext as WorkflowContext,
    WorkflowDefinitionError as WorkflowDefinitionError,
    WorkflowEngine as WorkflowEngine,
    WorkflowError as WorkflowError,
    WorkflowFailed as WorkflowFailed,
    WorkflowRejected as WorkflowRejected,
    command_step as command_step,
)