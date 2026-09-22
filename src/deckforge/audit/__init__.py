"""Детерминированный аудит готовых слайдов (Task 11, Приложение 1 ТЗ).

`deckforge.audit` не зовёт модель — недетерминированные (контекстные)
проверки того же аудита будут отдельной задачей (см. брифом дословно:
"аудит — часть пайплайна... какие из них детерминированные, а какие
контекстуальные"). Всё, что здесь считается, обязано на одном и том же
файле давать один и тот же результат при любом числе повторов.
"""
from deckforge.audit.config import AuditConfig
from deckforge.audit.findings import Finding, Severity
from deckforge.audit.deterministic import CHECK_IDS, run_deterministic

__all__ = ["AuditConfig", "Finding", "Severity", "CHECK_IDS", "run_deterministic"]
