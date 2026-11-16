from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ManagerTaskPlan(BaseModel):
    """A single task proposed by manager-0 for the current directive."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    title: str = Field(min_length=2, max_length=240)
    description: str = Field(min_length=1, max_length=8_000)
    division_code: str = Field(pattern=r"^D(?:0[1-9]|1[0-2])$")
    assigned_agent_id: str = Field(min_length=1, max_length=64)
    expected_deliverables: list[str] = Field(max_length=50)
    depends_on: list[Annotated[StrictInt, Field(ge=0)]] = Field(max_length=50)


class ManagerPlan(BaseModel):
    """Strict structured output contract for manager-0."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    reply: str = Field(min_length=1, max_length=5_000)
    tasks: list[ManagerTaskPlan] = Field(max_length=30)

    @model_validator(mode="after")
    def validate_dependency_indexes(self) -> "ManagerPlan":
        task_count = len(self.tasks)
        for index, task in enumerate(self.tasks):
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError(f"Task {index} repeats a depends_on index.")
            for dependency_index in task.depends_on:
                if dependency_index >= task_count:
                    raise ValueError(
                        f"Task {index} depends_on index {dependency_index}, but the plan has {task_count} task(s)."
                    )
                if dependency_index == index:
                    raise ValueError(f"Task {index} cannot depend on itself.")
        return self
