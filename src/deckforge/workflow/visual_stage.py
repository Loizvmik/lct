"""Стадия аудита по картинке внутри генерации: только рискованные слайды и
только если бюджет прогона позволяет (задача H).

Общая для API (`api.jobs._run_job`) и командной строки (`cli._cmd_generate`),
чтобы оба пути одинаково решали, запускать ли стадию, какие слайды брать и
что писать в отчёт о пропуске. Слайды берутся из одного варианта (dense):
содержание у трёх вариантов одно, различается вёрстка, а вопросы C01-C11
про смысл."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from deckforge.audit.findings import Finding
from deckforge.audit.risk import pick_risky_slides
from deckforge.audit.visual import VisualAuditResult, run_visual, run_visual_batch
from deckforge.plan.outline import SourceDoc
from deckforge.plan.spec import DeckSpec
from deckforge.workflow.budget import RunBudget, RunMode


@dataclass
class VisualStageOutcome:
    """Итог стадии. `result` есть, только если модель реально спрашивали;
    иначе `skipped_reason` объясняет почему (режим, нет модели, нечего
    проверять). `picked`: (позиция слайда с нуля, технический балл,
    семантический балл) — задача L, обе оценки `audit.risk`."""

    result: VisualAuditResult | None = None
    picked: list[tuple[int, float, float]] = field(default_factory=list)
    skipped_reason: str | None = None
    seconds: float = 0.0
    batch: bool = False

    @property
    def findings(self) -> list[Finding]:
        return list(self.result.findings) if self.result is not None else []

    def summary(self) -> dict:
        return {
            "ran": self.result is not None,
            "skipped_reason": self.skipped_reason,
            "slides": [pos for pos, _tech, _sem in self.picked],
            "risk": {
                str(pos): {"technical": round(tech, 2), "semantic": round(sem, 2), "total": round(tech + sem, 2)}
                for pos, tech, sem in self.picked
            },
            "findings": len(self.findings),
            "model_calls": self.result.model_calls if self.result is not None else 0,
            "seconds": round(self.seconds, 1),
            "batch": self.batch,
        }


def run_visual_stage(
    budget: RunBudget, spec: DeckSpec, det_findings: Iterable[Finding], vlm,
    render_pngs: Callable[[], list[Path]], *, sources: list[SourceDoc] | None = None,
    autofixed_slides: Iterable[int] = (), max_workers: int = 4,
) -> VisualStageOutcome:
    """PNG рендерятся через `render_pngs` лениво: командной строке их
    приходится рендерить отдельно (soffice, секунды), и делать это стоит,
    только если стадия реально пойдёт."""
    started = time.monotonic()
    policy = budget.policy
    mode = budget.mode_spec()

    def _done(outcome: VisualStageOutcome) -> VisualStageOutcome:
        outcome.seconds = time.monotonic() - started
        budget.record("visual_audit", outcome.seconds)
        if outcome.skipped_reason and "visual_audit" not in budget.skipped:
            budget.skipped["visual_audit"] = outcome.skipped_reason
        return outcome

    if mode.visual_audit_max_slides <= 0:
        # Режим решён на контрольной точке ДО этого вызова (`decide_mode
        # ("after_compose")`, задача L) — здесь только читаем итог, не
        # спрашиваем бюджет заново. `mode_history[-1]` несёт остаток
        # времени на момент того решения, не текущий (стадия могла начаться
        # позже точки).
        mode_name = budget.mode.value if budget.mode is not None else RunMode.FULL.value
        remaining_txt = ""
        if budget.mode_history:
            last = budget.mode_history[-1]
            remaining_txt = f"осталось {last['remaining_seconds']:.0f}с из {budget.deadline_seconds:.0f}с на точке «{last['checkpoint']}»"
        reason = f"режим {mode_name}: аудит по картинке выключен" + (f" ({remaining_txt})" if remaining_txt else "")
        return _done(VisualStageOutcome(skipped_reason=reason))
    if vlm is None:
        return _done(VisualStageOutcome(skipped_reason="модель для аудита по картинке не задана (нет ключа)"))

    picked = pick_risky_slides(
        spec, det_findings, max_slides=mode.visual_audit_max_slides,
        min_score=policy.visual_audit_min_risk, autofixed_slides=autofixed_slides,
    )
    if not picked:
        return _done(VisualStageOutcome(skipped_reason="рискованных слайдов нет"))

    try:
        pngs = render_pngs()
    except Exception as exc:  # noqa: BLE001: без превью стадия пропускается, генерация не падает
        return _done(VisualStageOutcome(picked=picked, skipped_reason=f"превью слайдов не отрисовались: {exc}"))

    positions = [pos for pos, _tech, _sem in picked]
    if policy.visual_audit_batch:
        result = run_visual_batch(pngs, spec, vlm, only_slides=positions, sources=sources)
    else:
        result = run_visual(
            pngs, spec, None, vlm, sources=sources, max_workers=max_workers,
            only_slides=set(positions), deck_level=False,
        )
    if result.skipped_reason:
        return _done(VisualStageOutcome(picked=picked, skipped_reason=result.skipped_reason))
    return _done(VisualStageOutcome(result=result, picked=picked, batch=policy.visual_audit_batch))
