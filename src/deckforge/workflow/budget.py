"""Бюджет времени одного прогона генерации (ТЗ: пять минут на презентацию).

До этого модуля время ограничивалось только снизу вверх: дедлайн одного
вызова модели (`llm.deadline_seconds`) и бюджет шага переранжирования
(`llm.pattern_picker_step_budget_seconds`). Сумма стадий при этом ничем не
ограничивалась: замер показал 302,8 с на генерацию и ещё 152,8 с на аудит
по картинке. `RunBudget` смотрит сверху вниз: сколько осталось от общего
бюджета, и решает, запускать ли необязательную стадию.

Политика деградации простая и явная. Обязательные стадии (разбор,
структура, текст, сборка, детерминированный аудит, экспорт) идут всегда:
без них нет файла, а файл важнее уложиться в срок. Необязательные
(переранжирование раскладок моделью, аудит по картинке) идут, только если
до конца бюджета осталось не меньше их порога из `config/app.yaml`
(`run.*_min_remaining`). Порог означает «сколько стадия обычно занимает плюс
запас на обязательные стадии после неё», а не её собственный дедлайн:
пропущенная стадия ничего не ломает, у каждой есть детерминированный
запасной путь (выбор раскладки кодом; отчёт без находок C01-C11 с
причиной пропуска)."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Callable

# Стадии, которые бюджет вправе пропустить. Всё остальное считается
# обязательным: `can_run` для него всегда `True`.
OPTIONAL_STAGES: tuple[str, ...] = ("rerank", "visual_audit")


@dataclass(frozen=True)
class BudgetPolicy:
    """Числа из раздела `run:` конфига. Дефолты дублируют `config/app.yaml`,
    чтобы бюджет работал и там, где конфиг не читается (тесты на чужом
    дереве), тем же приёмом, что и остальные настройки `Settings`."""

    budget_seconds: float = 300.0
    rerank_min_remaining: float = 120.0
    visual_audit_min_remaining: float = 75.0
    visual_audit_max_slides: int = 4
    visual_audit_min_risk: float = 1.0
    visual_audit_batch: bool = False

    def min_remaining(self, stage: str) -> float | None:
        if stage == "rerank":
            return self.rerank_min_remaining
        if stage == "visual_audit":
            return self.visual_audit_min_remaining
        return None


@dataclass
class RunBudget:
    """Часы одного прогона. Создаётся в самом начале генерации, до первого
    вызова модели: всё, что было до создания, в бюджет не попадает.

    `clock` подменяется в тестах, чтобы проверять пороги без `sleep`."""

    deadline_seconds: float
    policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    clock: Callable[[], float] = time.monotonic
    stage_seconds: dict[str, float] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._started = self.clock()

    @classmethod
    def from_policy(cls, policy: BudgetPolicy, *, clock: Callable[[], float] = time.monotonic) -> RunBudget:
        return cls(deadline_seconds=policy.budget_seconds, policy=policy, clock=clock)

    def elapsed(self) -> float:
        return self.clock() - self._started

    def remaining(self) -> float:
        """Сколько секунд осталось; отрицательное число значит, что бюджет
        уже превышен (обязательные стадии всё равно доделываются)."""
        return self.deadline_seconds - self.elapsed()

    def can_run(self, stage: str) -> bool:
        threshold = self.policy.min_remaining(stage)
        if threshold is None:
            return True
        return self.remaining() >= threshold

    def check(self, stage: str) -> bool:
        """`can_run` плюс запись причины пропуска: интерфейсу и логу нужно
        знать не только что стадия не шла, но и почему."""
        if self.can_run(stage):
            return True
        threshold = self.policy.min_remaining(stage)
        self.skipped[stage] = (
            f"осталось {max(self.remaining(), 0.0):.0f}с из {self.deadline_seconds:.0f}с, "
            f"стадии нужно не меньше {threshold:.0f}с"
        )
        return False

    def record(self, stage: str, seconds: float) -> None:
        """Время стадии копится, а не перезаписывается: стадия может идти
        кусками (аудит трёх вариантов, экспорт вперемешку)."""
        self.stage_seconds[stage] = self.stage_seconds.get(stage, 0.0) + max(seconds, 0.0)

    def summary(self) -> dict:
        return {
            "budget_seconds": self.deadline_seconds,
            "elapsed_seconds": round(self.elapsed(), 1),
            "stage_seconds": {k: round(v, 1) for k, v in self.stage_seconds.items()},
            "skipped": dict(self.skipped),
        }


def load_policy(app_yaml_path) -> BudgetPolicy:
    """Политика из `config/app.yaml`; нечитаемый конфиг даёт дефолты, а не
    падение: бюджет не должен ронять генерацию."""
    try:
        from deckforge.settings import Settings

        run = Settings.load(app_yaml_path).run
    except Exception:  # noqa: BLE001: без конфига бюджет работает на дефолтах
        return BudgetPolicy()
    return BudgetPolicy(
        budget_seconds=run.budget_seconds,
        rerank_min_remaining=run.rerank_min_remaining,
        visual_audit_min_remaining=run.visual_audit_min_remaining,
        visual_audit_max_slides=run.visual_audit_max_slides,
        visual_audit_min_risk=run.visual_audit_min_risk,
        visual_audit_batch=run.visual_audit_batch,
    )
