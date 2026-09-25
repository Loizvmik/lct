"""Проверка того, что исходные факты дошли до содержания и готового PPTX."""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from deckforge.plan.spec import (
    BulletBlock, CardBlock, DeckSpec, KpiBlock, QuoteBlock, SlideSpec, TextBlock,
    numeric_facts,
)

_NUMBER_RE = re.compile(r"(?<![\w])(?:[+−-]?\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?\s*%?)(?![\w])")
_GENERIC_HEADLINES = {
    "тема и цель презентации", "о чём пойдёт речь", "контекст задачи",
    "в чём проблема", "что показывает измерение", "что предлагается сделать",
    "как это работает", "результат пилота/проверки", "риски и как их снимаем",
    "что нужно для раскатки", "о чём просим комитет/аудиторию",
    "итог и следующий шаг", "дополнительный контекст",
}


@dataclass(frozen=True)
class SourceFact:
    value: str
    normalized: str
    context: str


class ContentValidationError(ValueError):
    pass


def _normalize_number(value: str) -> str:
    return value.replace("\u00a0", "").replace(" ", "").replace(",", ".").replace("−", "-").lower()


def extract_source_facts(sources: list[str]) -> list[SourceFact]:
    facts: list[SourceFact] = []
    seen: set[str] = set()
    for source in sources:
        allowed = {_normalize_number(value) for value in numeric_facts(source)}
        for match in _NUMBER_RE.finditer(source):
            value = match.group(0).strip()
            normalized = _normalize_number(value)
            if normalized not in allowed:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            left = max(0, source.rfind(".", 0, match.start()) + 1)
            right_hit = source.find(".", match.end())
            right = len(source) if right_hit < 0 else right_hit + 1
            context = " ".join(source[left:right].split())[:240]
            facts.append(SourceFact(value=value, normalized=normalized, context=context))
    return facts


def slide_text(slide: SlideSpec, *, include_headline: bool = True) -> str:
    parts: list[str] = [slide.headline] if include_headline else []
    parts.extend(text for text in (slide.subhead, slide.source_note) if text)
    for block in slide.blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, BulletBlock):
            parts.extend(block.items)
        elif isinstance(block, CardBlock):
            for card in block.items:
                parts.extend((card.title, card.body))
        elif isinstance(block, KpiBlock):
            for item in block.items:
                parts.extend((item.value, item.label))
        elif isinstance(block, QuoteBlock):
            parts.extend((block.text, block.author or ""))
    if slide.visual is not None:
        if slide.visual.caption:
            parts.append(slide.visual.caption)
        if slide.visual.table:
            parts.extend(cell for row in slide.visual.table.rows for cell in row)
        if slide.visual.chart:
            parts.extend(slide.visual.chart.categories)
            for series in slide.visual.chart.series:
                parts.append(series.name)
                parts.extend(str(value) for value in series.values)
    return "\n".join(part for part in parts if part and part.strip())


def deck_text(deck: DeckSpec) -> str:
    return "\n".join(slide_text(slide) for slide in deck.slides)


def _block_texts(deck: DeckSpec) -> list[tuple[int, str, str]]:
    result: list[tuple[int, str, str]] = []
    for slide in deck.slides:
        for block in slide.blocks:
            if isinstance(block, TextBlock):
                result.append((slide.index, "body", block.text))
            elif isinstance(block, BulletBlock):
                result.extend((slide.index, "bullet", item) for item in block.items)
            elif isinstance(block, CardBlock):
                for card in block.items:
                    if card.title:
                        result.append((slide.index, "card_title", card.title))
                    result.append((slide.index, "card_body", card.body))
            elif isinstance(block, KpiBlock):
                for item in block.items:
                    result.extend(((slide.index, "kpi_value", item.value), (slide.index, "kpi_label", item.label)))
            elif isinstance(block, QuoteBlock):
                result.append((slide.index, "quote", block.text))
                if block.author:
                    result.append((slide.index, "quote_author", block.author))
    return [(index, role, text) for index, role, text in result if text and text.strip()]


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def source_coverage_report(deck: DeckSpec, sources: list[str]) -> dict:
    facts = extract_source_facts(sources)
    deck_numbers = {_normalize_number(value) for value in numeric_facts(deck_text(deck))}
    items = [
        {
            "value": fact.value,
            "context": fact.context,
            "used": fact.normalized in deck_numbers,
            "reason": None if fact.normalized in deck_numbers else "Факт не выбран для повествования моделью.",
        }
        for fact in facts
    ]
    used = sum(1 for item in items if item["used"])
    return {
        "source_fact_count": len(items),
        "used_fact_count": used,
        "coverage_ratio": 1.0 if not items else round(used / len(items), 3),
        "facts": items,
    }


def validate_deck_content(deck: DeckSpec, sources: list[str]) -> dict:
    report = source_coverage_report(deck, sources)
    body_chars = sum(len(slide_text(slide, include_headline=False).strip()) for slide in deck.slides)
    headline_only = [
        slide.index for slide in deck.slides
        if not slide_text(slide, include_headline=False).strip()
    ]
    generic = [slide.index for slide in deck.slides if slide.headline.strip().lower() in _GENERIC_HEADLINES]
    problems: list[str] = []
    if sources and body_chars < max(80, len(deck.slides) * 18):
        problems.append("в слайдах почти нет содержательного текста из исходных материалов")
    if len(headline_only) > max(2, len(deck.slides) // 3):
        problems.append(f"слишком много слайдов только с заголовком: {headline_only}")
    if generic:
        problems.append(f"обнаружены служебные заголовки запасного плана: {generic}")
    if report["source_fact_count"] and report["used_fact_count"] == 0:
        problems.append("ни один числовой факт из исходных материалов не использован")
    if problems:
        raise ContentValidationError("; ".join(problems))
    report["body_char_count"] = body_chars
    report["headline_only_slides"] = headline_only
    return report


def pptx_slide_text(path: Path) -> str:
    texts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", name).group()),
        )
        for name in names:
            root = etree.fromstring(archive.read(name))
            for paragraph in root.xpath("//*[local-name()='p']"):
                text = "".join(paragraph.xpath(".//*[local-name()='t']/text()"))
                if text:
                    texts.append(text)
    return "\n".join(texts)


def validate_pptx_content(path: Path, deck: DeckSpec, coverage: dict) -> dict:
    rendered = pptx_slide_text(path)
    normalized_rendered = _normalize_text(rendered)
    rendered_numbers = {_normalize_number(value) for value in numeric_facts(rendered)}
    required = [
        _normalize_number(item["value"]) for item in coverage.get("facts", []) if item.get("used")
    ]
    missing = [value for value in required if value not in rendered_numbers]
    if missing:
        raise ContentValidationError(
            "при сборке PPTX потерялись использованные факты: " + ", ".join(missing)
        )
    expected_blocks = _block_texts(deck)
    missing_blocks = [
        (index, role) for index, role, text in expected_blocks
        if _normalize_text(text) not in normalized_rendered
    ]
    if missing_blocks:
        labels = ", ".join(f"{index + 1}:{role}" for index, role in missing_blocks[:12])
        raise ContentValidationError(
            f"при сборке PPTX потеряны блоки слайдов ({labels})"
        )
    if len(rendered.strip()) < max(80, len(deck.slides) * 18):
        raise ContentValidationError("готовый PPTX содержит слишком мало текста")
    return {
        "text_char_count": len(rendered),
        "verified_fact_count": len(required),
        "verified_block_count": len(expected_blocks),
        "missing_block_count": 0,
    }
