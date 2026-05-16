from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ai_orchestrator.schemas import ExecutionResult, Plan, PlanStep, TaskSpec, ValidationResult


class Backend(ABC):
    """Abstract backend used by the deterministic flow engine."""

    name: str

    @abstractmethod
    def plan(self, task: TaskSpec) -> Plan:
        raise NotImplementedError

    @abstractmethod
    def execute_step(
        self,
        *,
        task: TaskSpec,
        step: PlanStep,
        attempt: int,
        previous_feedback: list[str],
        artifacts_dir: Path,
    ) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    def validate_step(
        self,
        *,
        task: TaskSpec,
        step: PlanStep,
        result: ExecutionResult,
    ) -> ValidationResult:
        raise NotImplementedError
