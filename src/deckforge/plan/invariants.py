"""Смысловой инвариант слайда (задача V2): что пункт плана обязан нести
после письма, чем бы ни кончилась нормализация.

Раньше пустой ответ писателя на содержательный пункт тихо становился
разделителем (`plan.normalize._blockless_as_section`): «Результат
пилота/проверки» в живом прогоне 27 сентября 2026 (visual, слайд 8)
вышел заголовком на героической раскладке, хотя в плане это был слайд с
результатами. Смысл пункта после письма меняться не должен: пустой
результат это отказ контракта и повод для ремонта, а не новый вид
слайда. Разделителем становится только пункт, который план сам сделал
героическим (титул, финал, разделитель стиля airy) или цитатой.

Граница слоёв: модуль в `plan/`, координат и python-pptx не знает."""
from __future__ import annotations
from dataclasses import dataclass

from deckforge.plan.spec import BulletBlock, CardBlock, KpiBlock, QuoteBlock, SlideSpec, TextBlock

# Виды пункта плана, у которых слайд героический по замыслу: заголовок и,
# может быть, строка. Разделитель стиля airy узнаётся по контракту.
_HERO_OUTLINE_KINDS = frozenset({"title", "closing", "divider", "section"})


@dataclass(frozen=True)
class SemanticInvariant:
    """`slide_role`: вид пункта плана (`problem`, `case`...).
    `required_facts`: что пункт обещал показать (`needs` структуры).
    `required_content_types`: чем это должно быть на слайде: `content`
    (любой блок текста, списка, карточек, показателей), `table`, `chart`.
    `may_be_section`: слайд вправе остаться одним заголовком."""
    slide_role: str
    required_facts: tuple[str, ...] = ()
    required_content_types: tuple[str, ...] = ()
    may_be_section: bool = False


def invariant_for(contract) -> SemanticInvariant:
    """Инвариант пункта по его контракту: вид пункта, обещанные факты,
    обязательное главное место раскладки и заказанный визуал."""
    role = getattr(contract, "outline_kind", "") or ""
    quote = any(getattr(slot, "block", None) == "quote" for slot in getattr(contract, "slots", ()))
    hero = bool(getattr(contract, "is_divider", False)) or role in _HERO_OUTLINE_KINDS
    types: list[str] = []
    if not hero:
        types.append("content")
    visual = getattr(contract, "required_visual", None)
    if visual in ("table", "chart"):
        types.append(visual)
    return SemanticInvariant(
        slide_role=role, required_facts=tuple(getattr(contract, "evidence", ()) or ()),
        required_content_types=tuple(types), may_be_section=hero or quote,
    )


def has_content(slide: SlideSpec) -> bool:
    for block in slide.blocks:
        if isinstance(block, (TextBlock, QuoteBlock)) and block.text.strip():
            return True
        if isinstance(block, (BulletBlock, CardBlock, KpiBlock)) and block.items:
            return True
    return slide.visual is not None


def invariant_problems(slide: SlideSpec, invariant: SemanticInvariant) -> list[str]:
    """Нарушения инварианта словами, для ремонта писателем. Пустой список:
    слайд несёт то, что обещал пункт плана."""
    problems: list[str] = []
    if "content" in invariant.required_content_types and not has_content(slide):
        facts = f" ({', '.join(invariant.required_facts[:4])})" if invariant.required_facts else ""
        problems.append(
            f"пункт плана «{invariant.slide_role}» содержательный{facts}, а блоков на слайде нет: "
            "напиши блоки по контракту раскладки, разделителем этот слайд быть не может"
        )
    # Таблицу и график, заказанные пунктом, проверяет контракт раскладки
    # (`contract_problems`): второй раз модели то же нарушение не нужно.
    return problems
