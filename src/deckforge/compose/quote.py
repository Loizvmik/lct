"""Цитата с нуля для шаблона без раскладки цитаты: крупный текст курсивом
кеглем h2 в кавычках языка колоды («» для русского), под ним автор мелко,
слева декоративная открывающая кавычка цветом акцента (или бренда) кеглем
display.

Раньше цитата без своей раскладки ложилась абзацем в карточки или на
финальную раскладку «Спасибо за внимание!». Модуль только раскладывает
места; рисует и ужимает их `builder.place_slide`."""
from __future__ import annotations
import re

from deckforge.compose.blocks import Paragraph, SlotContent
from deckforge.compose.kpi import line_height, lines_needed, text_slot, width_in
from deckforge.ooxml.geometry import Box
from deckforge.plan.spec import QuoteBlock

# Ширина колонки под декоративную кавычку, доля ширины места.
_MARK_SHARE = 0.1
_GAP = 0.015
# Текст цитаты не крупнее стольких кеглей body: h2 у VK Education 44 pt,
# и цитата в два предложения занимала восемь строк на весь слайд.
_TEXT_MAX_BODY = 2.0
_SHRINK = 0.9
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_EDGE_QUOTES = "«»“”„\"'‘’ "


def quote_marks(text: str, language: str | None = None) -> tuple[str, str]:
    """Кавычки языка: «ёлочки» для русского, “лапки” для остальных. Язык
    колоды знают не все вызовы, и тогда решает сам текст."""
    lang = (language or "").lower()
    if lang.startswith("ru") or (not lang and _CYRILLIC_RE.search(text or "")):
        return "«", "»"
    return "“", "”"


def quote_layout(
    block: QuoteBlock, region: Box, profile, family: str, language: str | None = None, *, with_mark: bool = True,
) -> list[SlotContent]:
    """Места цитаты в `region`: кавычка-декор (`with_mark`: в палитре есть
    акцент или бренд), текст, автор."""
    text = (block.text or "").strip().strip(_EDGE_QUOTES).strip()
    if not text:
        return []
    opening, closing = quote_marks(text, language)
    norm = profile.canvas_norm
    display_pt = profile.type_scale_pt("display", 0.0) or profile.type_scale_pt("h1", 0.0) or 48.0
    body_pt = profile.type_scale_pt("body", 0.0) or 14.0
    text_pt = min(profile.type_scale_pt("h2", 0.0) or body_pt * 1.5, body_pt * _TEXT_MAX_BODY)
    mark_w = region.width * _MARK_SHARE if with_mark else 0.0
    text_left = region.left + (mark_w + _GAP if with_mark else 0.0)
    text_w = region.right - text_left
    author = (block.author or "").strip()
    author_h = line_height(profile, body_pt, 2) if author else 0.0
    quoted = f"{opening}{text}{closing}"
    room = region.height - (author_h + _GAP if author else 0.0)
    while True:
        lines = lines_needed([quoted], family, text_pt, width_in(profile, text_w))
        text_h = line_height(profile, text_pt, lines)
        if text_h <= room or text_pt <= body_pt:
            break
        text_pt = max(body_pt, text_pt * _SHRINK)
    text_h = min(text_h, room)
    total = text_h + (_GAP + author_h if author else 0.0)
    top = region.top + max(0.0, (region.height - total) / 2)
    mark_pt = min(display_pt, max(text_pt * 2.5, body_pt))
    mark_h = min(line_height(profile, mark_pt), region.height)
    contents = [
        SlotContent(text_slot("quote", Box(text_left, top, text_w, text_h), text_pt * norm), "quote",
                    [Paragraph(quoted, italic=True)]),
    ]
    if with_mark:
        contents.insert(0, SlotContent(
            text_slot("quote_mark", Box(region.left, top, mark_w, mark_h), mark_pt * norm),
            "quote_mark", [Paragraph(opening)],
        ))
    if author:
        contents.append(SlotContent(
            text_slot("quote_author", Box(text_left, top + text_h + _GAP, text_w, author_h), body_pt * norm),
            "quote_author", [Paragraph(f"— {author}")],
        ))
    return contents
