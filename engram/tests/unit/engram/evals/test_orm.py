"""Unit tests for the evaluation-run summary ORM.

Asserts the mapper configuration — table names, the `kb` schema, the summary columns, and the
case-results foreign key to `evaluation_runs` — without a database (persistence is the repository's
integration concern).
"""

from __future__ import annotations

from engram.evals.shared.infrastructure.orm import EvaluationCaseResultORM, EvaluationRunORM


class TestEvaluationRunORM:
    def test_table_name_and_schema(self):
        assert EvaluationRunORM.__tablename__ == "evaluation_runs"
        assert EvaluationRunORM.__table__.schema == "kb"

    def test_has_the_summary_columns(self):
        columns = set(EvaluationRunORM.__table__.columns.keys())

        assert {"id", "dataset", "model_name", "variant", "started_at", "finished_at", "metrics", "logfire_url"} <= columns

    def test_id_is_the_primary_key(self):
        assert [column.name for column in EvaluationRunORM.__table__.primary_key] == ["id"]


class TestEvaluationCaseResultORM:
    def test_table_name_and_schema(self):
        assert EvaluationCaseResultORM.__tablename__ == "evaluation_case_results"
        assert EvaluationCaseResultORM.__table__.schema == "kb"

    def test_has_the_per_case_columns(self):
        columns = set(EvaluationCaseResultORM.__table__.columns.keys())

        assert {"id", "run_id", "case_name", "scores", "n_tool_calls", "total_tokens", "duration_s"} <= columns

    def test_run_id_is_a_foreign_key_to_eval_runs(self):
        targets = {fk.target_fullname for fk in EvaluationCaseResultORM.__table__.foreign_keys}

        assert any("evaluation_runs" in target for target in targets)
