"""Синтетические раскладки для тестов планировщика: числа и роли без
файла шаблона, чтобы проверять правила выбора, а не разбор."""
from __future__ import annotations
from types import SimpleNamespace

import pytest


def slot(role: str, *, max_chars: int = 120, max_words: int | None = None, fixed: bool = False,
         sample_text: str | None = None, ordinal: bool = False):
    return SimpleNamespace(
        role=role, max_chars=max_chars, max_words=max_words, purpose=None, content_hint=None,
        ordinal=ordinal, fixed=fixed, sample_text=sample_text,
    )


def pattern(pattern_id: str, kind: str, slots, *, repeat: int | None = None, repeat_roles=("card_body",),
            decor: int = 0, score: float = 0.8, source: int = 10, density: float | None = 0.4, max_bullets: int = 5):
    return SimpleNamespace(
        pattern_id=pattern_id, kind=kind, slots=list(slots),
        repeat=SimpleNamespace(count=repeat, slot_roles=list(repeat_roles)) if repeat else None,
        decor=[object()] * decor,
        capacity=SimpleNamespace(max_items=repeat or 1, max_chars_per_item=120, max_bullets=max_bullets,
                                 max_series=4, max_rows=6, max_cols=4),
        score=score, source_slide_index=[source], source_density=density, is_dark=False,
    )


def headline(max_chars: int = 60, **kw):
    return slot("headline", max_chars=max_chars, **kw)


def cards(pid: str, n: int, *, words: int = 20, source: int = 10, decor: int = 0, kind: str = "cards", **kw):
    return pattern(
        pid, kind, [headline()] + [slot("card_body", max_chars=words * 7, max_words=words) for _ in range(n)],
        repeat=n, source=source, decor=decor, **kw,
    )


def bullets(pid: str, *, words: int = 60, source: int = 20, kind: str = "bullets", **kw):
    return pattern(pid, kind, [headline(), slot("bullet", max_chars=words * 7, max_words=words)], source=source, **kw)


def section(pid: str, *, source: int, text: bool = False, **kw):
    slots = [headline()] + ([slot("body", max_chars=60)] if text else [])
    return pattern(pid, "section", slots, source=source, **kw)


def profile(*patterns):
    return SimpleNamespace(patterns=list(patterns))


@pytest.fixture
def rich_profile():
    """Обложка, разделитель, финал «Спасибо», несколько карточек и
    списков: у каждого слайда есть альтернативы."""
    return profile(
        section("cover", source=1, text=True),
        section("divider", source=5),
        pattern("thanks", "section", [headline(sample_text="Спасибо за внимание!")], source=60),
        cards("cards3", 3, source=11), cards("cards4", 4, source=12), cards("cards4b", 4, source=13, decor=6),
        bullets("list_a", source=20), bullets("list_b", source=21, kind="two_col"),
        bullets("list_c", source=22),
    )
