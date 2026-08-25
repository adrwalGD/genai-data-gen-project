"""F3.2 — real Gemini planning: instructions are reflected in a plan that still validates."""

import pytest

from genai_data_gen_project.config import Settings
from genai_data_gen_project.generation import planner
from genai_data_gen_project.generation.expander import expand
from genai_data_gen_project.generation.recipes import DecimalRangeRecipe, validate_plan
from genai_data_gen_project.generation.validator import validate
from genai_data_gen_project.llm.client import GeminiClient
from genai_data_gen_project.schema.parser import parse_ddl

pytestmark = pytest.mark.llm


def test_instructions_shape_the_plan(sample_ddl: dict[str, str]) -> None:
    schema = parse_ddl(sample_ddl["company"])
    llm = GeminiClient(Settings())
    instructions = (
        "All salaries must be between 50000 and 90000. Companies are located only in Polish cities. "
        "About 30% of employees are contractors. Use realistic Polish first and last names."
    )
    plan, output = planner.plan_with_llm(schema, instructions, 100, llm, temperature=0.2)
    assert validate_plan(plan, schema) == []
    assert output.overrides, "expected at least one override"
    applied = int(plan.notes[-1].split(": ")[1].split("/")[0])
    assert applied >= 2, plan.notes
    salary = plan.table("Employees").column("salary").recipe  # type: ignore[union-attr]
    assert isinstance(salary, DecimalRangeRecipe) and salary.min >= 50000 and salary.max <= 90000
    tables = expand(schema, plan, seed=1)
    report = validate(schema, tables)
    assert report.ok, report.summary()
    assert all(50000 <= float(s) <= 90000 for s in tables["Employees"]["salary"])
