"""Жёсткие ограничения выбора раскладки (раздел 7.1): какие паттерны
шаблона вообще можно поставить на слайд с данным намерением.

Всё, что здесь отсекается, стоимостью не лечится: четыре пункта не лягут в
сетку на три, таблица не встанет туда, где нет места под таблицу, финальная
«Спасибо за внимание!» не может быть вторым слайдом. Остальное (повторы,
вкус стиля, плотность) решает `scoring`.

Если после фильтров не остаётся ни одного кандидата, ограничения
ослабляются по одному, от наименее важного, и причина записывается: колода
обязана собраться и на бедном шаблоне, но не молча.

Здесь же живут правила, перенесённые из `plan.variants` (финальная и
титульная раскладка, совместимость вида с написанным содержанием, разрыв
структуры): теперь их зовёт планировщик, а `plan.variants` лишь
переэкспортирует их для старых вызовов."""
from __future__ import annotations
import re
from dataclasses import dataclass

from deckforge.pattern.forms import (
    MAX_PHOTO_VOID, PatternForm, SlideRequirements, capabilities_of, unmet_requirements,
)
from deckforge.pattern.intent import SlideIntent
from deckforge.plan.spec import BulletBlock, CardBlock, KpiBlock, QuoteBlock, SlideSpec, TextBlock
from deckforge.template.patterns import CHART_TIER_FRAME, is_fixed_phrase

_THANKS_RE = re.compile(r"спасибо|благодар|thank|вопрос|questions|контакт|contact", re.IGNORECASE)

_HERO_KINDS = frozenset({"section", "closing", "image", "photo_text"})
_HERO_ONLY_KINDS = frozenset({"section", "closing"})
# Раскладки, смысл которых несёт картинка.
_PICTURE_KINDS = frozenset({"image", "photo_text"})


def is_closing_pattern(p, profile) -> bool:
    """Финальная раскладка шаблона: героическая, снятая с последнего
    слайда-примера («Спасибо за внимание!» с QR-кодом у VK Education). Ей
    место только на последнем слайде колоды: на втором слайде она
    выглядела концом презентации (27 сентября 2026)."""
    if p.kind not in ("section", "closing") or not p.source_slide_index:
        return False
    if any(_THANKS_RE.search(sl.sample_text or "") for sl in p.slots):
        # У VK Education «Спасибо за внимание!» стоит на слайде 52, а
        # похожие слайды 53–55 схлопнуты в другой паттерн: по одному номеру
        # финал не узнать.
        return True
    last = max((n for q in profile.patterns for n in q.source_slide_index), default=None)
    return last is not None and min(p.source_slide_index) == last


def cover_pattern_id(profile) -> str | None:
    """Титульная раскладка: героическая, ближе всех к началу шаблона.
    Обложки лежат первыми, а по кеглю заголовка обложка и разделители у
    VK Education неотличимы (все по 48 pt), поэтому решает место примера."""
    heroes = [
        p for p in profile.patterns
        if p.kind in _HERO_KINDS and p.source_slide_index and not is_closing_pattern(p, profile)
        and any(s.role == "headline" for s in p.slots)
    ]
    if not heroes:
        return None
    return min(heroes, key=lambda p: (min(p.source_slide_index), p.pattern_id)).pattern_id


def has_fixed_headline(p) -> bool:
    """Заголовок примера помечен моделью как постоянный текст шаблона
    («Спасибо за внимание!»). Такую раскладку можно ставить только
    финальным слайдом: содержательный заголовок на ней спорит с замыслом
    макета."""
    return any(s.role == "headline" and s.fixed and is_fixed_phrase(s.sample_text) for s in p.slots)


def _has_headline(p) -> bool:
    return any(s.role == "headline" for s in p.slots)


def capacity_units(form: PatternForm) -> int:
    """Сколько единиц содержания раскладка держит. У текстовой раскладки
    каждое свободное текстовое место (колонка) берёт по единице."""
    main = form.main
    if main is None:
        return 0
    if main.block == "text":
        return 1 + sum(1 for p in form.parts if not p.required and p.block == "text")
    return main.units


def _allowed_blocks(intent: SlideIntent) -> set[str | None]:
    """Какой главной частью раскладка вправе встретить это намерение.
    Показатели и цитату код сам не назначает: выдуманная цитата и число без
    источника хуже списка. Их берёт только пункт, который модель структуры
    так и заказала, или пункт «данные» (показатели)."""
    if intent.is_hero:
        # Обложке, разделителю и финалу нужен заголовок и, может быть,
        # подпись: список или абзац у раскладки ляжет одной строкой.
        return {None, "text", "bullets"}
    allowed: set[str | None] = {"bullets", "cards", "text"}
    if intent.form == "kpi" or intent.outline_kind == "data":
        allowed.add("kpi")
    if intent.form == "quote":
        allowed.add("quote")
    if intent.required_visual in ("table", "chart"):
        # Слайд, где главное: таблица или график: подпись необязательна.
        allowed.add(None)
    return allowed


@dataclass(frozen=True)
class Candidates:
    pattern_ids: tuple[str, ...]
    # Какие ограничения пришлось ослабить, чтобы кандидаты вообще нашлись.
    relaxed: tuple[str, ...] = ()


def _checks(intent: SlideIntent, form: PatternForm, p, *, position: int, last: int, cover_id: str | None,
            closing: bool) -> dict[str, bool]:
    """Каждое жёсткое ограничение отдельно: имя -> выполнено ли. Отдельно,
    чтобы ослаблять их по одному и называть, какое не выполнилось."""
    main = form.main
    block = main.block if main is not None else None
    units = capacity_units(form)
    needs_units = 0 if intent.is_hero else intent.items
    checks = {
        "place": position == last or not closing,
        "cover": p.pattern_id != cover_id or position == 0,
        "fixed_headline": position == last or not has_fixed_headline(p),
        "headline": _has_headline(p),
        "form": block in _allowed_blocks(intent),
    }
    if intent.is_hero:
        # Героическому слайду героическая раскладка: разделитель на
        # раскладке с колонками выглядит недописанным слайдом.
        checks["form"] = checks["form"] and (
            p.kind in _HERO_ONLY_KINDS or (p.pattern_id == cover_id and position == 0)
        )
        if closing and position == last:
            # Финальная раскладка шаблона встречает финал колоды, какими бы
            # местами она ни была занята (контакты у VK Tech: повтор из
            # двух карточек): её текст примера и есть замысел макета.
            checks["form"] = True
    if intent.form in ("kpi", "quote"):
        checks["form"] = checks["form"] and block == intent.form
    # Единиц хватает, или можно занять меньше. Повтор карточек под одну
    # единицу не ставится: ряд из одной карточки пуст на две трети.
    if needs_units <= 0:
        checks["units"] = True
    elif block in ("cards",) or (block == "kpi" and form.repeated):
        checks["units"] = 2 <= needs_units <= units
    elif block is None:
        checks["units"] = True
    else:
        checks["units"] = needs_units <= units
    visual = intent.required_visual
    if visual == "table":
        checks["visual"] = form.has_table
    elif visual == "chart":
        checks["visual"] = has_chart_place(form)
    elif visual == "photo":
        checks["visual"] = form.has_image
    else:
        # Главное у раскладки картинка или таблица, а слайду нечего туда
        # поставить: место останется пустым. Таблица примера без нашей
        # таблицы осталась бы чужими данными, а фото примера без нашего
        # удаляется, и в раскладке «текст рядом со скриншотом» половина
        # холста пустеет.
        empty_visual = form.has_table or (form.has_image and p.kind in _PICTURE_KINDS) or not_plain_content(form)
        checks["visual"] = (block is not None or intent.is_hero) and not empty_visual
    checks["capabilities"] = not unmet_requirements(
        SlideRequirements.from_intent(intent), capabilities_of(p, form),
    )
    return checks


def has_chart_place(form: PatternForm) -> bool:
    """Задача V1: у раскладки есть куда поставить график: родной график
    примера, картинка-график или крупное текстовое место без пунктов
    (`forms.PatternForm.chart_tier`). Какое из трёх лучше, решает
    стоимость (`scoring.chart_fit`), здесь только «можно ли»."""
    return form.chart_tier is not None and form.slide_class in ("content_pattern", "visual_prototype")


def not_plain_content(form: PatternForm) -> bool:
    """Родной график примера на слайде без нашего графика: сборка удаляет
    его данные-образец (`clone.remove_sample_frames`), и половина слайда
    пустеет (ЛЦТ2026, slide21: график занимает левую половину). То же с
    картинкой-графиком (VK Education, слайды 47-50): без нашего графика
    она удаляется как чужое содержание, и слайд остаётся с одним
    заголовком. Правила, лист ассетов и инструкция шаблона
    (`prototypes.SLIDE_CLASSES`) не раскладки содержания вовсе."""
    if form.slide_class in ("style_guide", "asset_sheet", "instruction"):
        return True
    return form.has_chart or form.chart_tier == CHART_TIER_FRAME or form.slide_class == "visual_prototype"


# Порядок ослабления: от наименее важного к самому важному. Место
# финальной и титульной раскладки не ослабляется никогда.
_RELAX_ORDER = ("units", "capabilities", "form", "visual", "fixed_headline", "headline")

RELAX_TITLES = {
    "units": "единиц содержания больше, чем мест у любой раскладки",
    "form": "в шаблоне нет раскладки нужной формы",
    "visual": "нет места под таблицу, график или фото",
    "capabilities": "у раскладок нет мест под содержание слайда или остаётся пустота на месте фото примера",
    "fixed_headline": "осталась только раскладка с постоянным заголовком",
    "headline": "осталась только раскладка без заголовка",
}


def sample_photo_fits(intent: SlideIntent, p) -> bool:
    """Фото примера (`Pattern.photo_area`) клон удаляет, если на его
    место не ляжет фото слайда. Раскладка годится, только если после
    удаления пустеет не больше `forms.MAX_PHOTO_VOID` холста (задача V2):
    «Паттерн + фото» VK Education без своей фотографии это белая половина
    слайда, а с фотографией примера это чужая девушка в кресле в нашей
    колоде."""
    return capabilities_of(p).photo_void(bool(intent.photo)) <= MAX_PHOTO_VOID


def candidates_for(
    intent: SlideIntent, profile, forms: dict[str, PatternForm], *, position: int, last: int,
    cover_id: str | None = None, closing_ids: frozenset[str] = frozenset(),
) -> Candidates:
    """Паттерны, которые можно поставить на слайд `position` колоды из
    `last + 1` слайдов. Пустой результат только у шаблона без паттернов."""
    table = []
    for p in profile.patterns:
        checks = _checks(
            intent, forms[p.pattern_id], p, position=position, last=last, cover_id=cover_id,
            closing=p.pattern_id in closing_ids,
        )
        table.append((p.pattern_id, checks))
    for step in range(len(_RELAX_ORDER) + 1):
        ignore = set(_RELAX_ORDER[:step])
        ok = [
            pid for pid, checks in table
            if all(v for name, v in checks.items() if name not in ignore)
        ]
        if ok:
            chosen = [checks for pid, checks in table if pid in ok]
            relaxed = tuple(
                name for name in _RELAX_ORDER[:step] if any(not checks[name] for checks in chosen)
            )
            return Candidates(pattern_ids=tuple(ok), relaxed=relaxed)
    return Candidates(pattern_ids=tuple(p.pattern_id for p in profile.patterns), relaxed=("all",))


# ---------------------------------------------------------------------------
# Совместимость вида раскладки с уже написанным содержанием (перенесено из
# `plan.variants`): нужна, когда содержание после письма поменяло форму
# (`plan.normalize`: одна карточка стала абзацем) и слайду нужна другая
# раскладка.
# ---------------------------------------------------------------------------

KPI_CAPTION_MAX_ITEMS = 1


def kinds_that_hold_cards(profile) -> tuple[str, ...]:
    """Виды раскладок этого шаблона, среди которых есть хоть одна с
    повтором по ролям карточек. `cards` идёт первым, если он есть."""
    if profile is None:
        return ()
    kinds = {
        p.kind for p in profile.patterns
        if p.repeat is not None and set(p.repeat.slot_roles) & {"card_title", "card_body"}
    }
    if not kinds:
        return ()
    ordered = ["cards"] if "cards" in kinds else []
    ordered += sorted(k for k in kinds if k != "cards")
    return tuple(ordered)


def block_has_text(block) -> bool:
    if isinstance(block, TextBlock):
        return bool(block.text.strip())
    if isinstance(block, BulletBlock):
        return any(item.strip() for item in block.items)
    return False


def compatible_kinds(slide: SlideSpec, profile=None) -> tuple[str, ...]:
    """Виды раскладки, на которые содержание `slide` ляжет без потери по
    структуре: карточкам нужен повтор, показателям слоты показателей,
    таблице таблица, цитате цитата (или любой текстовый вид), слайду без
    блоков героическая раскладка. Полное обоснование каждого правила в
    истории `plan.variants._compatible_kinds`, откуда функция перенесена."""
    has_card = any(isinstance(b, CardBlock) and b.items for b in slide.blocks)
    kpi_block = next((b for b in slide.blocks if isinstance(b, KpiBlock) and b.items), None)
    has_table = slide.visual is not None and slide.visual.table is not None
    has_quote = any(isinstance(b, QuoteBlock) and b.text.strip() for b in slide.blocks)
    n_text = sum(1 for b in slide.blocks if isinstance(b, TextBlock))
    has_bullets = any(isinstance(b, BulletBlock) and b.items for b in slide.blocks)

    if has_card:
        return kinds_that_hold_cards(profile) or ("cards",)
    if kpi_block is not None:
        if len(kpi_block.items) <= KPI_CAPTION_MAX_ITEMS:
            return ("kpi_caption", "kpi")
        return ("kpi",)
    if has_table:
        return ("table",)
    if has_quote:
        return ("quote", "section", "bullets")
    if not slide.blocks and slide.visual is None:
        return ("section",)
    if slide.kind == "section" and not has_bullets and n_text <= 1 and slide.visual is None:
        return ("section", "bullets")
    if slide.visual is not None and slide.visual.kind in ("photo", "icon"):
        if sum(1 for b in slide.blocks if isinstance(b, (TextBlock, BulletBlock)) and block_has_text(b)) >= 1:
            return ("photo_text", "image", "bullets", "two_col")
        return ("image", "bullets", "two_col")
    if n_text >= 2:
        return ("two_col", "bullets")
    if has_bullets or n_text >= 1:
        return ("bullets", "two_col")
    return ("bullets",)


def structural_gap(slide: SlideSpec, profile) -> str | None:
    """Почему ни одна раскладка шаблона не вмещает содержание слайда по
    структуре, или `None`. Выбор не меняет, только называет причину
    числами: «нужно 6 единиц, максимум в шаблоне 4»."""
    if profile is None or not profile.patterns:
        return None
    for block in slide.blocks:
        if isinstance(block, CardBlock) and block.items:
            holders = [
                p for p in profile.patterns
                if p.repeat is not None and set(p.repeat.slot_roles) & {"card_title", "card_body"}
            ]
            return _units_gap(len(block.items), holders, "карточек", "карточки")
        if isinstance(block, KpiBlock) and block.items:
            holders = [p for p in profile.patterns if p.kind in ("kpi", "kpi_caption")]
            return _units_gap(len(block.items), holders, "показателей", "показатели")
    if slide.visual is not None and slide.visual.table is not None:
        if not any(p.kind == "table" for p in profile.patterns):
            return "нужен слот под таблицу, в шаблоне его нет"
    return None


def _units_gap(needed: int, holders: list, many: str, target: str) -> str | None:
    if not holders:
        return f"{many} на слайде: {needed}, раскладок под {target} в шаблоне нет"
    most = max(max(p.capacity.max_items, p.repeat.count if p.repeat is not None else 0) for p in holders)
    if needed > most:
        return f"нужно {needed} единиц, максимум в шаблоне {most}"
    return None
