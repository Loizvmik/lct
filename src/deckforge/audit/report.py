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
        else:
            vis_findings = list(visual)
            skipped_reason = None

        return cls(
            findings=_stable_sort(det + vis_findings),
            visual_skipped_reason=skipped_reason,
            deterministic_count=len(det),
            visual_count=len(vis_findings),
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
