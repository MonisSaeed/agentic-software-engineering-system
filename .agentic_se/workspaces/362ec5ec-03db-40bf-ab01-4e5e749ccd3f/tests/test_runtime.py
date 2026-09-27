import asyncio
import tempfile
import unittest

from workflow_engine import (
    PolicyDenied,
    Step,
    Workflow,
    WorkflowContext,
    WorkflowEngine,
    WorkflowRejected,
)
from workflow_engine.store import SQLiteRunStore


class WorkflowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_nodes_synchronize_before_dependent_node(self):
        arrived = 0
        both_started = asyncio.Event()

        async def branch(context, key, value):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return value

        def join(context):
            return context.outputs["left"] + context.outputs["right"]

        workflow = Workflow(
            "parallel",
            (
                Step("left", lambda context: branch(context, "left", 20)),
                Step("right", lambda context: branch(context, "right", 22)),
                Step("join", join, depends_on=("left", "right")),
            ),
        )
        result = await WorkflowEngine(max_parallel_steps=2).run(workflow)
        self.assertEqual(result.context.outputs["join"], 42)
        self.assertEqual(result.statuses["join"], "succeeded")

    async def test_retries_then_uses_fallback(self):
        attempts = []

        def unstable(_context):
            attempts.append(1)
            raise RuntimeError("provider unavailable")

        workflow = Workflow(
            "fallback",
            (Step("agent", unstable, max_attempts=2, fallback_action=lambda _: "stubbed"),),
        )
        result = await WorkflowEngine().run(workflow)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(result.context.outputs["agent"], "stubbed")
        self.assertTrue(any(event.kind == "fallback_completed" for event in result.events))

    async def test_replan_requires_human_approval_and_creates_new_revision(self):
        requests = []

        def ask_for_replan(context):
            context.request_replan("upstream requirement changed")

        initial = Workflow("replan", (Step("inspect", ask_for_replan, stage="inspect"),))

        def replan(_workflow, _context, _reason):
            return Workflow("replan", (Step("updated", lambda _: "fresh"),), revision=2)

        result = await WorkflowEngine(
            approval_handler=lambda request, _context: requests.append(request) or True,
            replan_handler=replan,
        ).run(initial)

        self.assertEqual(result.context.graph_revision, 2)
        self.assertEqual(result.completed_steps, ("updated",))
        self.assertEqual(requests[0].step_name, "__replan__")
        self.assertTrue(any(event.kind == "replanned" for event in result.events))

    async def test_write_action_requires_approved_plan(self):
        workflow = Workflow(
            "guarded-write",
            (Step("edit", lambda _: "changed", autonomy="write", affected_paths=("src/a.py",)),),
        )
        with self.assertRaises(PolicyDenied):
            await WorkflowEngine().run(workflow)

    async def test_replan_rejection_stops_run(self):
        def ask(context):
            context.request_replan("new requirement")

        workflow = Workflow("blocked-replan", (Step("inspect", ask),))
        with self.assertRaises(WorkflowRejected):
            await WorkflowEngine(
                approval_handler=lambda _request, _context: False,
                replan_handler=lambda *_: workflow,
            ).run(workflow)

    async def test_run_store_persists_events_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRunStore(f"{directory}/runs.sqlite3")
            context = WorkflowContext("stored", state={"api_key": "never-store-this"})
            workflow = Workflow("stored", (Step("inspect", lambda _: {"ok": True}),))
            result = await WorkflowEngine(run_store=store).run(workflow, context=context)

            record = store.get_run(result.context.run_id)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["context"]["state"]["api_key"], "[REDACTED]")
            self.assertIn("run_completed", [event["kind"] for event in store.get_events(result.context.run_id)])
            self.assertEqual(store.metrics()["runs_completed"], 1)


if __name__ == "__main__":
    unittest.main()