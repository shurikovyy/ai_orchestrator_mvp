from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class TaskSpec(BaseModel):
    """User-level task contract."""

    id: str = Field(default_factory=lambda: f"task_{uuid4().hex[:8]}")
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    max_retries: int = Field(default=2, ge=0, le=10)

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Task description cannot be empty")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria_not_empty(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class PlanStep(BaseModel):
    id: str
    title: str
    description: str
    assigned_role: Literal["planner", "executor", "validator", "codex_executor"] = "executor"
    acceptance_criteria: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    task_id: str
    summary: str
    steps: list[PlanStep]


class ExecutionResult(BaseModel):
    step_id: str
    attempt: int
    status: Literal["completed", "failed"]
    content: str
    artifact_paths: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    step_id: str
    attempt: int
    approved: bool
    score: float = Field(ge=0.0, le=1.0)
    failed_criteria: list[str] = Field(default_factory=list)
    feedback: list[str] = Field(default_factory=list)


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}")
    task: TaskSpec
    plan: Plan | None = None
    executions: list[ExecutionResult] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    final_status: Literal["created", "planned", "running", "approved", "failed"] = "created"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
