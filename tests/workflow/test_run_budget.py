"""`workflow.budget.RunBudget`: пороги необязательных стадий и деградация.
Часы подменяются, чтобы проверять пороги без `sleep`."""
from __future__ import annotations
from pathlib import Path

from deckforge.workflow.budget import BudgetPolicy, RunBudget, load_policy


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _budget(clock: _Clock, **policy) -> RunBudget:
    return RunBudget.from_policy(BudgetPolicy(**policy), clock=clock)


def test_remaining_counts_down_from_creation():
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300)
    assert budget.remaining() == 300
    clock.now += 120
    assert budget.elapsed() == 120
    assert budget.remaining() == 180


def test_optional_stages_degrade_by_threshold():
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300, rerank_min_remaining=120, visual_audit_min_remaining=75)
    clock.now += 150  # осталось 150
    assert budget.can_run("rerank")
    assert budget.can_run("visual_audit")
    clock.now += 40  # осталось 110: rerank уже нельзя, аудит ещё можно
    assert not budget.can_run("rerank")
    assert budget.can_run("visual_audit")
    clock.now += 40  # осталось 70
    assert not budget.can_run("visual_audit")


def test_mandatory_stages_always_run_even_over_budget():
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300)
    clock.now += 1000
    assert budget.remaining() < 0
    for stage in ("parse", "outline", "write", "compose", "audit", "export"):
        assert budget.can_run(stage)


def test_check_records_skip_reason():
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300, rerank_min_remaining=120)
    clock.now += 250
    assert budget.check("rerank") is False
    assert "rerank" in budget.skipped
    assert "120" in budget.skipped["rerank"]
    assert budget.check("export") is True
    assert "export" not in budget.skipped


def test_record_accumulates_and_summary_is_serialisable():
    import json

    clock = _Clock()
    budget = _budget(clock)
    budget.record("audit", 1.5)
    budget.record("audit", 2.0)
    budget.record("export", -1.0)  # отрицательное время не портит сумму
    summary = budget.summary()
    assert summary["stage_seconds"] == {"audit": 3.5, "export": 0.0}
    json.dumps(summary)


def test_load_policy_reads_run_section(tmp_path: Path):
    app_yaml = Path("config/app.yaml")
    policy = load_policy(app_yaml)
    assert policy.budget_seconds == 300
    assert policy.rerank_min_remaining == 120
    assert policy.visual_audit_min_remaining == 75
    assert policy.visual_audit_max_slides == 4


def test_load_policy_falls_back_to_defaults(tmp_path: Path):
    assert load_policy(tmp_path / "missing.yaml") == BudgetPolicy()
