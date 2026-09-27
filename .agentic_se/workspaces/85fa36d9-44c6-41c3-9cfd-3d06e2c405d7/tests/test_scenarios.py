import tempfile
import unittest
from pathlib import Path

from agent_runtime import StubProvider, WorkspaceSandbox
from agent_runtime.scenarios import build_scenario, revise_scenario
from workflow_engine import WorkflowBlocked, WorkflowEngine
from workflow_engine.store import SQLiteRunStore


class ScenarioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]
        self.provider = StubProvider()

    def tearDown(self):
        self.directory.cleanup()

    def sandbox(self, name):
        return WorkspaceSandbox(
            self.root,
            Path(self.directory.name) / name,
        )

    async def run_scenario(self, name, *, answer=None):
        sandbox = self.sandbox(name)
        definition = build_scenario(name, self.provider, sandbox, answer=answer)
        store = SQLiteRunStore(Path(self.directory.name) / f"{name}.sqlite3")
        engine = WorkflowEngine(
            approval_handler=lambda _request, _context: True,
            replan_handler=revise_scenario,
            rollback_handler=lambda _context, _target: sandbox.rollback(),
            run_store=store,
        )
        result = await engine.run(definition.workflow, context=definition.context)
        return result, store

    async def test_greenfield_completes_all_gates_and_validations(self):
        result, _store = await self.run_scenario("greenfield")
        self.assertEqual(result.statuses["release_readiness"], "succeeded")
        self.assertIn("integration_tests", result.completed_steps)
        self.assertEqual(
            sum(event.kind == "approval_granted" for event in result.events), 3
        )

    async def test_brownfield_failure_rolls_back_and_repairs(self):
        result, store = await self.run_scenario("brownfield")
        self.assertEqual(result.loopbacks, 1)
        self.assertTrue(any(event.kind == "rollback" for event in result.events))
        self.assertEqual(result.context.state["implementation_round"], 2)
        self.assertEqual(store.metrics()["rollback_count"], 1)

    async def test_ambiguous_requirement_safe_stops_without_answer(self):
        sandbox = self.sandbox("ambiguous")
        definition = build_scenario("ambiguous", self.provider, sandbox)
        engine = WorkflowEngine(approval_handler=lambda _request, _context: True)
        with self.assertRaises(WorkflowBlocked):
            await engine.run(definition.workflow, context=definition.context)
        self.assertIn("open_questions", definition.context.state)

    async def test_ambiguous_requirement_resumes_after_clarification(self):
        result, _store = await self.run_scenario(
            "ambiguous",
            answer="Track total and daily click counts; no personal identifiers; API-key protected.",
        )
        self.assertEqual(result.context.state["requirement_revision"], 2)
        self.assertEqual(result.context.graph_revision, 2)
        self.assertEqual(result.statuses["release_readiness"], "succeeded")

    def test_sandbox_rejects_path_escape_and_restores_checkpoint(self):
        sandbox = self.sandbox("sandbox")
        sandbox.write_text("reviews/plan.md", "approved")
        checkpoint = sandbox.checkpoint("approved")
        sandbox.write_text("reviews/change.md", "unapproved change")
        with self.assertRaises(PermissionError):
            sandbox.write_text("../outside.txt", "blocked")
        sandbox.rollback()
        self.assertTrue((sandbox.root / "reviews/plan.md").exists())
        self.assertFalse((sandbox.root / "reviews/change.md").exists())
        self.assertTrue(checkpoint.is_dir())


if __name__ == "__main__":
    unittest.main()