import sys
import unittest

from workflow_engine import (
    ApprovalRequired,
    Step,
    StepExecutionError,
    Workflow,
    WorkflowEngine,
    WorkflowFailed,
    WorkflowRejected,
    command_step,
)


class WorkflowEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_dag_in_dependency_order_and_passes_state(self):
        observed = []

        def prepare(context):
            context.state["value"] = 40
            observed.append("prepare")

        def finish(context):
            observed.append("finish")
            return context.state["value"] + 2

        workflow = Workflow(
            "basic",
            (
                Step("finish", finish, depends_on=("prepare",)),
                Step("prepare", prepare),
            ),
        )

        result = await WorkflowEngine().run(workflow)

        self.assertEqual(observed, ["prepare", "finish"])
        self.assertEqual(result.context.outputs["finish"], 42)
        self.assertEqual(result.completed_steps, ("prepare", "finish"))

    async def test_test_failure_loops_back_to_repair_ancestor(self):
        repair_runs = []

        def repair(context):
            repair_runs.append(len(repair_runs) + 1)
            context.state["repair_count"] = len(repair_runs)

        def run_tests(context):
            if context.state["repair_count"] < 2:
                raise StepExecutionError("tests failed", category="test")

        workflow = Workflow(
            "repair-loop",
            (
                Step("repair", repair),
                Step("tests", run_tests, depends_on=("repair",), on_failure="repair"),
            ),
        )
        result = await WorkflowEngine().run(workflow)

        self.assertEqual(repair_runs, [1, 2])
        self.assertEqual(result.loopbacks, 1)
        self.assertEqual(
            [event.details.get("category") for event in result.events if event.kind == "step_failed"],
            ["test"],
        )

    async def test_build_command_failure_is_reported_to_listener(self):
        events = []
        workflow = Workflow(
            "build-fails",
            (command_step("build", (sys.executable, "-c", "raise SystemExit(7)")),),
        )

        with self.assertRaises(WorkflowFailed):
            await WorkflowEngine(event_listener=events.append).run(workflow)

        failure = next(event for event in events if event.kind == "step_failed")
        self.assertEqual(failure.details["category"], "build")
        self.assertEqual(failure.details["return_code"], 7)

    async def test_approval_checkpoint_rejects_and_blocks_following_steps(self):
        later_steps = []
        workflow = Workflow(
            "approval",
            (
                Step("review", requires_approval=True),
                Step("deploy", lambda _: later_steps.append("deployed"), depends_on=("review",)),
            ),
        )

        with self.assertRaises(WorkflowRejected):
            await WorkflowEngine(approval_handler=lambda _request, _context: False).run(workflow)
        self.assertEqual(later_steps, [])

    async def test_approval_checkpoint_requires_handler(self):
        workflow = Workflow("approval", (Step("review", requires_approval=True),))
        with self.assertRaises(ApprovalRequired):
            await WorkflowEngine().run(workflow)

    async def test_dependency_cycles_are_rejected_before_execution(self):
        workflow = Workflow(
            "cycle",
            (Step("first", depends_on=("second",)), Step("second", depends_on=("first",))),
        )
        with self.assertRaisesRegex(Exception, "contain a cycle"):
            await WorkflowEngine().run(workflow)


if __name__ == "__main__":
    unittest.main()