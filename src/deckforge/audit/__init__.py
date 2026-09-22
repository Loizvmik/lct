"""Аудит готовых слайдов — детерминированный (Task 11) и визуальный по
картинке (Task 12, Приложение 1 ТЗ).

`audit.deterministic` не зовёт модель: всё, что она считает, обязано на
одном и том же файле давать один и тот же результат при любом числе
повторов. `audit.visual` — наоборот, вызывает модель по картинке слайда и
намеренно недетерминирована (ТЗ дословно, см. докстроку `audit.visual`).
`audit.report.AuditReport.merge` сводит оба списка находок в один."""
from deckforge.audit.config import AuditConfig
from deckforge.audit.findings import Finding, Severity
from deckforge.audit.deterministic import CHECK_IDS, run_deterministic
from deckforge.audit.report import AuditReport
from deckforge.audit.visual import CHECK_IDS as VISUAL_CHECK_IDS, VisualAuditResult, run_visual

__all__ = [
    "AuditConfig", "Finding", "Severity", "CHECK_IDS", "run_deterministic",
    "AuditReport", "VISUAL_CHECK_IDS", "VisualAuditResult", "run_visual",
]
