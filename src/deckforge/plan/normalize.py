"""Вырождение содержания под раскладки шаблона: после письма текста, до
вариантов.

Зачем. Писатель выбирает форму по смыслу пункта структуры, а не по тому,
сколько материала у него набралось. Отсюда слайды, которые форма делает
хуже, чем текст сам по себе: ряд «карточек» из одной карточки (сетка на
три места с двумя пустыми), список из двух пунктов «Медиана — 6,2 ч» и
«Пилот — 4 подразделения», который просится в крупные цифры. Код видит
это по структуре, без модели и без координат, поэтому правит сам.

Правила детерминированные и узкие, каждое со своей находкой в `findings`:
- `CardBlock` из одной карточки -> `KpiBlock`, если заголовок карточки
  число или процент и в шаблоне есть раскладки показателей, иначе ->
  `TextBlock` (заголовок карточки становится первым предложением);
- единственный на слайде `BulletBlock` из одного-двух коротких пунктов, и
  в каждом есть число, -> `KpiBlock` (число — значение, остаток — подпись),
  если в шаблоне есть раскладки показателей;
- две карточки при шаблоне без раскладки на две единицы повтора —
  содержание не трогается, но это записывается: сетка на три места с
  одним пустым видна глазом, и человек должен знать, откуда она.

Слайд, чья форма поменялась, теряет выбранную писателем раскладку
(`pattern_id`): она подбиралась под карточки или список, а не под то, во
что они превратились. Раскладку подберёт `variants.apply_variant`.

Граница слоёв: модуль в `plan/`, координат и python-pptx не знает,
профиль читает только как список видов и повторов."""
from __future__ import annotations
import re
from dataclasses import replace

from deckforge.plan.spec import BulletBlock, Card, CardBlock, DeckSpec, Kpi, KpiBlock, SlideSpec, TextBlock

# Число с необязательным знаком, дробной частью и короткой единицей.
# Единицы — только те, что встречаются в показателях на русском; «с» и
# «г» не входят: их не отличить от предлога и сокращения года.
_UNIT = r"(?:%|‰|×|x|п\.\s?п\.|(?:раза|раз|ч|мин|сек|дней|дня|дн|мес|лет|млн|млрд|тыс|руб|₽)(?![а-яёa-z]))"
_NUMBER = rf"[≈~<>≤≥+\-−–]?\s?\d+(?:[  ]\d{{3}})*(?:[.,]\d+)?\s?{_UNIT}?"
_NUMBER_RE = re.compile(_NUMBER, re.IGNORECASE)
_NUMERIC_TITLE_RE = re.compile(rf"^\s*{_NUMBER}\s*$", re.IGNORECASE)

# Пункт длиннее — уже утверждение, а не показатель с подписью.
_KPI_BULLET_MAX_WORDS = 8
_KPI_BULLET_MAX_ITEMS = 2

_KPI_KINDS = ("kpi", "kpi_caption")
_CARD_ROLES = frozenset({"card_title", "card_body"})


def normalize_deck(deck: DeckSpec, profile) -> DeckSpec:
    """Прогоняет каждый слайд через правила модуля. Без профиля ничего не
    делает: какие формы есть в шаблоне, тогда неизвестно."""
    if profile is None:
        return deck
    kinds = {p.kind for p in profile.patterns}
    two_unit_cards = _has_two_unit_card_layout(profile)
    slides = [_normalize_slide(slide, kinds, two_unit_cards) for slide in deck.slides]
    slides = _diversify_cards(slides, kinds)
    changed = sum(1 for before, after in zip(deck.slides, slides) if before is not after)
    meta = dict(deck.meta)
    if changed:
        meta["normalized_slides"] = str(changed)
    return replace(deck, slides=slides, meta=meta)


def normalized_count(deck: DeckSpec) -> int:
    return int(deck.meta.get("normalized_slides", "0"))


_HERO_KINDS = frozenset({"section", "image", "closing", "quote"})


def _blockless_as_section(slide: SlideSpec, kinds: set[str]) -> SlideSpec | None:
    """Слайд с одним заголовком, без блоков и визуала, но содержательного
    вида (bullets, cards…): писатель ничего не написал, и он садился на
    любую героическую раскладку, включая финальную «Спасибо за внимание!»
    (27 сентября 2026, слайд «О чём пойдёт речь»). Честнее считать его
    разделителем, если в шаблоне такие есть."""
    if slide.kind in _HERO_KINDS or slide.blocks or slide.visual is not None or "section" not in kinds:
        return None
    return replace(slide, kind="section", findings=[
        *slide.findings,
        f"Слайд {slide.index}: содержания нет, только заголовок; собран как разделитель.",
    ])


# Сколько карточных слайдов подряд по колоде допустимо до того, как
# следующий уходит в список по колонкам: у VK Education под четыре
# карточки с длинным текстом подходит одна раскладка (slide21), и пять
# карточных слайдов из одиннадцати выходили одинаковыми (27 сентября 2026).
_MAX_CARD_SLIDES = 2


def _diversify_cards(slides: list[SlideSpec], kinds: set[str]) -> list[SlideSpec]:
    """Третий и дальше карточный слайд колоды становится списком по
    колонкам (`two_col`, иначе `bullets`): «заголовок карточки: тело» на
    пункт. Смысл тот же, форма другая, и ранжир получает другие кандидаты."""
    target = "two_col" if "two_col" in kinds else ("bullets" if "bullets" in kinds else None)
    if target is None:
        return slides
    out: list[SlideSpec] = []
    seen = 0
    for slide in slides:
        cards = [b for b in slide.blocks if isinstance(b, CardBlock)]
        if slide.kind != "cards" or len(cards) != 1 or len(slide.blocks) != 1:
            out.append(slide)
            continue
        seen += 1
        if seen <= _MAX_CARD_SLIDES:
            out.append(slide)
            continue
        items = [f"{c.title}: {c.body}" if c.title else c.body for c in cards[0].items]
        out.append(replace(slide, kind=target, blocks=[BulletBlock(items=items)], findings=[
            *slide.findings,
            f"Слайд {slide.index}: карточки заменены списком, в колоде уже {_MAX_CARD_SLIDES} карточных слайда.",
        ]))
    return out


def _normalize_slide(slide: SlideSpec, kinds: set[str], two_unit_cards: bool) -> SlideSpec:
    has_kpi_layout = any(k in kinds for k in _KPI_KINDS)
    result = _blockless_as_section(slide, kinds)
    if result is not None:
        return result
    result = _single_card(slide, kinds, has_kpi_layout)
    if result is not None:
        return result
    result = _numeric_bullets(slide, kinds, has_kpi_layout)
    if result is not None:
        return result
    return _two_cards_note(slide, two_unit_cards)


def _single_card(slide: SlideSpec, kinds: set[str], has_kpi_layout: bool) -> SlideSpec | None:
    position = next(
        (i for i, b in enumerate(slide.blocks) if isinstance(b, CardBlock) and len(b.items) == 1), None,
    )
    if position is None:
        return None
    card: Card = slide.blocks[position].items[0]
    other_kpi = any(isinstance(b, KpiBlock) for b in slide.blocks)
    blocks = list(slide.blocks)
    numeric_title = bool(card.title) and bool(_NUMERIC_TITLE_RE.match(card.title)) and _is_notable(card.title)
    if has_kpi_layout and not other_kpi and numeric_title:
        blocks[position] = KpiBlock(items=[Kpi(value=card.title.strip(), label=card.body.strip())])
        kind = _kpi_kind(kinds, 1)
        what = "показатель"
    else:
        blocks[position] = TextBlock(text=_join_title(card.title, card.body))
        kind = "bullets" if slide.kind == "cards" and "bullets" in kinds else slide.kind
        what = "абзац"
    return _changed(slide, blocks, kind, f"одна карточка превращена в {what}: ряд из одной карточки пуст")


def _numeric_bullets(slide: SlideSpec, kinds: set[str], has_kpi_layout: bool) -> SlideSpec | None:
    if not has_kpi_layout or len(slide.blocks) != 1 or not isinstance(slide.blocks[0], BulletBlock):
        return None
    if slide.visual is not None and slide.visual.kind in ("table", "chart"):
        return None
    items = slide.blocks[0].items
    if not 1 <= len(items) <= _KPI_BULLET_MAX_ITEMS:
        return None
    kpis: list[Kpi] = []
    for item in items:
        kpi = _as_kpi(item)
        if kpi is None:
            return None
        kpis.append(kpi)
    kind = _kpi_kind(kinds, len(kpis))
    return _changed(slide, [KpiBlock(items=kpis)], kind, "короткие пункты с числами превращены в показатели")


def _two_cards_note(slide: SlideSpec, two_unit_cards: bool) -> SlideSpec:
    if two_unit_cards or slide.kind != "cards":
        return slide
    if not any(isinstance(b, CardBlock) and len(b.items) == 2 for b in slide.blocks):
        return slide
    note = (
        f"Слайд {slide.index}: две карточки, а раскладки на две единицы в шаблоне нет — "
        "они лягут в сетку побольше, одно место может остаться пустым."
    )
    slide.findings.append(note)
    return slide


def _as_kpi(item: str) -> Kpi | None:
    """Пункт как показатель: одно число и подпись из остальных слов."""
    text = item.strip()
    if not text or len(text.split()) > _KPI_BULLET_MAX_WORDS:
        return None
    match = _NUMBER_RE.search(text)
    if match is None or not _is_notable(match.group(0)):
        return None
    value = match.group(0).strip()
    label = (text[:match.start()] + " " + text[match.end():]).strip()
    label = re.sub(r"\s+", " ", label).strip(" ,.:;—–-")
    if not label:
        return None
    return Kpi(value=value, label=label[0].upper() + label[1:])


def _is_notable(value: str) -> bool:
    """Число, которое годится в показатель: с единицей, с дробной частью
    или хотя бы двузначное. Голая цифра — это номер шага («Шаг 1»), а не
    метрика, и крупной цифрой на слайде она ничего не говорит."""
    digits = re.sub(r"\D", "", value)
    has_unit = bool(re.search(r"[^\d\s.,≈~<>≤≥+\-−–]", value))
    has_fraction = bool(re.search(r"\d[.,]\d", value))
    return has_unit or has_fraction or len(digits) >= 2


def _kpi_kind(kinds: set[str], count: int) -> str:
    """`kpi` по умолчанию; одиночный показатель — на раскладку с подписью,
    если `kpi` в шаблоне нет, а она есть."""
    if "kpi" in kinds:
        return "kpi"
    return "kpi_caption" if count == 1 and "kpi_caption" in kinds else "kpi"


def _join_title(title: str, body: str) -> str:
    title, body = title.strip(), body.strip()
    if not title:
        return body
    if not body:
        return title
    return f"{title} {body}" if title[-1] in ".!?:…" else f"{title}. {body}"


def _changed(slide: SlideSpec, blocks: list, kind: str, why: str) -> SlideSpec:
    return replace(
        slide, blocks=blocks, kind=kind, pattern_id=None,
        findings=[*slide.findings, f"Слайд {slide.index}: {why}."],
    )


def _has_two_unit_card_layout(profile) -> bool:
    return any(
        p.repeat is not None and p.repeat.count == 2 and _CARD_ROLES & set(p.repeat.slot_roles)
        for p in profile.patterns
    )
