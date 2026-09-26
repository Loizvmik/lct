"""`workflow.budget.RunBudget`: режимы прогона (задача L) — выбор режима на
контрольной точке по остатку времени, фиксация до следующей точки. Часы
подменяются, чтобы проверять пороги без `sleep`."""
from __future__ import annotations
from pathlib import Path

from deckforge.workflow.budget import BudgetPolicy, ModeSpec, RunBudget, RunMode, load_policy


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


def test_same_inputs_give_full_mode_at_both_checkpoints():
    """Задача L: одинаковые входы при нормальной латентности (несколько
    секунд между точками из пятиминутного бюджета) обязаны давать один и
    тот же режим на обеих контрольных точках — FULL, тот же критерий, что
    в брифе задачи."""
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300)
    clock.now += 5  # write заняла 5с — нормальная латентность
    assert budget.decide_mode("after_write") is RunMode.FULL
    clock.now += 8  # rerank+сборка ещё 8с
    assert budget.decide_mode("after_compose") is RunMode.FULL
    assert budget.mode is RunMode.FULL
    assert budget.mode_checkpoint == "after_compose"
    assert [entry["mode"] for entry in budget.mode_history] == ["full", "full"]


def test_mode_degrades_as_remaining_time_shrinks():
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300)
    clock.now += 181  # осталось 119 — чуть ниже порога FULL (120)
    assert budget.decide_mode("after_write") is RunMode.FAST
    clock.now += 45  # осталось 74 — чуть ниже порога FAST (75)
    assert budget.decide_mode("after_compose") is RunMode.EMERGENCY
    assert [entry["checkpoint"] for entry in budget.mode_history] == ["after_write", "after_compose"]


def test_mode_spec_before_any_checkpoint_is_full():
    """До первой контрольной точки (стадия проверяется в изоляции, без
    полного пайплайна) режим не решён — деградация не должна начинаться
    по умолчанию, только по факту вызова `decide_mode`."""
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300)
    clock.now += 1000  # бюджет давно исчерпан, но точки не было
    assert budget.mode is None
    assert budget.mode_spec() == BudgetPolicy().modes[RunMode.FULL]


def test_mode_table_controls_rerank_and_visual_audit_scope():
    clock = _Clock()
    modes = {
        RunMode.FULL: ModeSpec(min_remaining=150, rerank=True, visual_audit_max_slides=4),
        RunMode.FAST: ModeSpec(min_remaining=75, rerank=False, visual_audit_max_slides=2),
        RunMode.EMERGENCY: ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=0),
    }
    budget = _budget(clock, budget_seconds=300, modes=modes)
    clock.now += 200  # осталось 100 — не хватает на FULL (150)
    assert budget.decide_mode("after_write") is RunMode.FAST
    spec = budget.mode_spec()
    assert spec.rerank is False
    assert spec.visual_audit_max_slides == 2


def test_mandatory_stages_are_not_gated_by_mode():
    """Обязательные стадии (задача H, сохранено задачей L) режимом не
    управляются вовсе — только `rerank`/`visual_audit_max_slides` из
    `ModeSpec` читает вызывающий код (`cli.py`/`api.jobs`)."""
    clock = _Clock()
    budget = _budget(clock, budget_seconds=300)
    clock.now += 1000
    assert budget.remaining() < 0
    budget.decide_mode("after_write")
    assert budget.mode is RunMode.EMERGENCY
    for stage in ("parse", "outline", "write", "compose", "audit", "export"):
        budget.record(stage, 1.0)
    assert set(budget.stage_seconds) == {"parse", "outline", "write", "compose", "audit", "export"}


def test_record_accumulates_and_summary_is_serialisable():
    import json

    clock = _Clock()
    budget = _budget(clock)
    budget.record("audit", 1.5)
    budget.record("audit", 2.0)
    budget.record("export", -1.0)  # отрицательное время не портит сумму
    budget.decide_mode("after_write")
    summary = budget.summary()
    assert summary["stage_seconds"] == {"audit": 3.5, "export": 0.0}
    assert summary["mode"] == "full"
    assert summary["mode_checkpoint"] == "after_write"
    assert len(summary["mode_history"]) == 1
    json.dumps(summary)


def test_load_policy_reads_run_section(tmp_path: Path):
    app_yaml = Path("config/app.yaml")
    policy = load_policy(app_yaml)
    assert policy.budget_seconds == 300
    # Задача M: 4 -> 8 / 2 -> 4 (аудит по картинке теперь идёт по трём
    # вариантам параллельно, не по одному dense — см. `config/app.yaml`).
    assert policy.modes[RunMode.FULL] == ModeSpec(min_remaining=120, rerank=True, visual_audit_max_slides=8)
    assert policy.modes[RunMode.FAST] == ModeSpec(min_remaining=75, rerank=False, visual_audit_max_slides=4)
    assert policy.modes[RunMode.EMERGENCY] == ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=0)


def test_load_policy_falls_back_to_defaults(tmp_path: Path):
    assert load_policy(tmp_path / "missing.yaml") == BudgetPolicy()
