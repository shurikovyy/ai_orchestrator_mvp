from contextlib import contextmanager
from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.schemas import TaskSpec

TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


@contextmanager
def temporary_test_dir():
    path = TEST_TEMP_ROOT / f"tmp_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class EngineTests(unittest.TestCase):
    def test_mock_backend_retries_and_approves(self) -> None:
        with temporary_test_dir() as tmp:
            task = TaskSpec(
                description="Create demo artifact",
                acceptance_criteria=["has title", "mentions validation loop"],
                max_retries=2,
            )
            state = TaskExecutionEngine(MockBackend(), tmp).run(task)
            self.assertEqual(state.final_status, "approved")
            self.assertGreaterEqual(len(state.executions), 2)
            self.assertTrue((tmp / state.run_id / "final_report.md").exists())

    def test_no_infinite_loop_when_criteria_fail(self) -> None:
        with temporary_test_dir() as tmp:
            task = TaskSpec(
                description="Create demo artifact",
                acceptance_criteria=["criterion impossible only on final attempt?"],
                max_retries=0,
            )
            state = TaskExecutionEngine(MockBackend(), tmp).run(task)
            self.assertIn(state.final_status, {"approved", "failed"})
            self.assertEqual(len(state.executions), 1)

    def test_review_packet_uses_final_status(self) -> None:
        with temporary_test_dir() as tmp:
            task = TaskSpec(
                description="Create demo artifact",
                acceptance_criteria=["has title", "mentions validation loop"],
                max_retries=2,
            )
            state = TaskExecutionEngine(MockBackend(), tmp).run(task)
            packet = (tmp / state.run_id / "REVIEW_PACKET.md").read_text(encoding="utf-8")
            self.assertIn("Run status: `approved`", packet)
            self.assertNotIn("Run status: `running`", packet)


if __name__ == "__main__":
    unittest.main()
