"""`AuditReport` — сведение детерминированного (`audit.deterministic`) и
визуального (`audit.visual`) аудитов в один объект (Task 12, интерфейс
брифа дословно: `AuditReport.merge(deterministic, visual) -> AuditReport`).

Не более того: сведение — это объединение списков находок в один стабильно
отсортированный список плюс честная передача причины, по которой
визуальная часть могла не выполниться (`visual_skipped_reason`, см.
докстроку `audit.visual` про честную деградацию без модели) — само
объединение не решает, чинить находку или нет, это пользовательский
интерфейс поверх отчёта (не входит в эту задачу)."""
from __future__ import annotations
from dataclasses import dataclass, field

from deckforge.audit.findings import Finding
from deckforge.audit.visual import VisualAuditResult


def _stable_sort(findings: list[Finding]) -> list[Finding]:
    """Тот же критерий, что `audit.deterministic._stable_sort` — независимо
    реализован здесь, а не импортирован (модуль приватный, и `report.py` —
    такой же независимый потребитель одного и того же общего критерия
    сортировки находок, что и три места, считающие WCAG-контраст заново
    (см. докстроку `deterministic.py` про этот же принцип)."""
    return sorted(
        findings,
        key=lambda f: (
            f.slide_index if f.slide_index is not None else -1,
            f.check_id, f.shape_ref or "", f.message,
        ),
    )


@dataclass
class AuditReport:
    """Итоговый список находок обоих аудитов вместе, плюс `visual_skipped_
    reason` — непустая строка, если визуальная часть не выполнялась вовсе
    (см. `audit.visual.VisualAuditResult.skipped_reason`); в этом случае
    `findings` всё равно несёт полный результат детерминированного аудита
    — отсутствие модели не должно занижать то, что код и так способен
    проверить сам."""

    findings: list[Finding] = field(default_factory=list)
    visual_skipped_reason: str | None = None
    deterministic_count: int = 0
    visual_count: int = 0
    # Задача G (PPTEval): оценки 1-5 переносятся из `VisualAuditResult` как
    # есть, не пересчитываются здесь заново — `merge` только сводит, а не
    # решает, что считать оценкой (тот же принцип, что и с
    # `visual_skipped_reason` выше). Плоский список находок (не
    # `VisualAuditResult`) не несёт оценок вовсе — интерфейс брифа
    # (`run_visual(...) -> list[Finding]`) старше самих оценок и о них не
    # знает, поэтому в этом случае поля остаются пустыми/`None`, честно.
    slide_scores: dict[int, dict] = field(default_factory=dict)
    deck_score: dict | None = None
    content_avg: float | None = None
    design_avg: float | None = None

    @classmethod
    def merge(
        cls,
        deterministic: list[Finding],
        visual: "VisualAuditResult | list[Finding]",
    ) -> "AuditReport":
        det = list(deterministic)
        if isinstance(visual, VisualAuditResult):
            vis_findings = list(visual.findings)
            skipped_reason = visual.skipped_reason
            slide_scores = dict(visual.slide_scores)
            deck_score = visual.deck_score
            content_avg = visual.content_avg
            design_avg = visual.design_avg
        else:
            vis_findings = list(visual)
            skipped_reason = None
            slide_scores = {}
            deck_score = None
            content_avg = None
            design_avg = None

        return cls(
            findings=_stable_sort(det + vis_findings),
            visual_skipped_reason=skipped_reason,
            deterministic_count=len(det),
            visual_count=len(vis_findings),
            slide_scores=slide_scores,
            deck_score=deck_score,
            content_avg=content_avg,
            design_avg=design_avg,
        )

    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def by_check(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.check_id] = out.get(f.check_id, 0) + 1
        return out
