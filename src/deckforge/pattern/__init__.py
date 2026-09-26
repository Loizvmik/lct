"""Глобальный планировщик паттернов: какая раскладка шаблона достанется
каждому слайду, решается до текста и сразу для всей колоды (разделы 6-7
docs/architecture). Пакет работает только с профилем шаблона (роли слотов,
пределы слов, повторы), без python-pptx, координат и вызовов модели."""
from deckforge.pattern.planner import PatternAssignment, plan_patterns, repick_pattern

__all__ = ["PatternAssignment", "plan_patterns", "repick_pattern"]
