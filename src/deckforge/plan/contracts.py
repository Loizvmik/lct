"""Контракт слайда (`SlideContract`, раздел 9): что писатель обязан
написать под раскладку, которую планировщик уже выбрал.

Раньше текст писался под «самую вместительную раскладку вида», а
раскладка подбиралась после, и красивые раскладки отклонялись: писатель
давал 140-200 знаков на пункт, плашки VK Education держат 36-84 (раздел 10).
Теперь порядок обратный: раскладка известна до текста, и писатель получает
её места числами: заголовок до N слов, K карточек по M слов, таблица до R
строк. Код проверяет ответ по тому же контракту (`contract_problems`) и
один раз просит сократить или дополнить.

Контракт строится из формы раскладки (`pattern.forms`), тем же расчётом,
которым планировщик оценивал вместимость: цели и пределы не расходятся.
Цель места 0,8 предела, как у прежнего `slide_tools.list_layouts`.
Координат в контракте нет: роли, слова, знаки, число единиц."""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from deckforge.pattern.forms import MIN_HEADLINE_CHARS, FormPart, Limit, list_item_limit, pattern_form
from deckforge.pattern.style import load_style
from deckforge.template.patterns import CHART_TIER_TEXT
from deckforge.plan.spec import BulletBlock, CardBlock, KpiBlock, QuoteBlock, SlideSpec, TextBlock

_WORD_RE = re.compile(r"\S+")

# Таблица и график без замеренных пределов раскладки: тот же порядок, что
# `compose` рисует без обрезки (до 7 строк, 5 колонок, 5 серий).
_DEFAULT_VISUAL_LIMITS = {"max_rows": 6, "max_cols": 4, "max_series": 4}

# Показатель: значение короткое по природе («6,2 ч», «−80%»). Рамку под
# цифру разбор меряет по примеру (две-три цифры), и предел знаков по ней
# отверг бы любую единицу измерения, поэтому у значения предел в словах.
_KPI_VALUE_MAX_WORDS = 3

_BLOCK_TYPES = {"text": TextBlock, "bullets": BulletBlock, "cards": CardBlock, "kpi": KpiBlock, "quote": QuoteBlock}


def count_words(text: str | None) -> int:
    return len(_WORD_RE.findall(text or ""))


@dataclass(frozen=True)
class TextSpec:
    """Цель и предел одного места. `max_chars` 0: знаков не назвать."""
    target_words: int
    max_words: int
    max_chars: int = 0

    @classmethod
    def of(cls, limit: Limit) -> "TextSpec":
        return cls(target_words=limit.target_words, max_words=max(1, limit.max_words), max_chars=limit.max_chars)

    def to_dict(self) -> dict:
        out = {"target_words": self.target_words, "max_words": self.max_words}
        if self.max_chars:
            out["max_chars"] = self.max_chars
        return out

    def problem(self, text: str, what: str) -> str | None:
        words, chars = count_words(text), len(text or "")
        if words > self.max_words:
            return f"{what}: {words} слов при пределе {self.max_words}"
        if self.max_chars and chars > self.max_chars:
            return f"{what}: {chars} знаков при пределе {self.max_chars}"
        return None


@dataclass(frozen=True)
class SlotContract:
    """Одна часть содержания: блок ответа писателя.

    `block`: text / bullets / cards / kpi / quote. `count`: сколько единиц
    (карточек, пунктов, показателей), у абзаца и цитаты 1. `item`: предел
    единицы (тело карточки, пункт, абзац, значение показателя). `title`:
    заголовок карточки или подпись показателя."""
    block: str
    count: int
    item: TextSpec
    role: str
    title: TextSpec | None = None
    title_slot: bool = False
    purpose: str | None = None
    content_hint: str | None = None
    required: bool = True

    def to_dict(self) -> dict:
        out: dict = {"type": self.block, "count": self.count, "required": self.required}
        key = {"cards": "body", "kpi": "value", "bullets": "item"}.get(self.block, "text")
        out[key] = self.item.to_dict()
        if self.title is not None:
            out["label" if self.block == "kpi" else "title"] = self.title.to_dict()
        if self.block == "cards" and not self.title_slot:
            out["title_note"] = "у карточки нет своего места под заголовок: код сам поставит его жирной первой строкой тела; в тело его не повторяй"
        if self.purpose:
            out["purpose"] = self.purpose
        if self.content_hint:
            out["content_hint"] = self.content_hint
        return out


@dataclass(frozen=True)
class SlideContract:
    slide_id: int
    intent: str
    pattern_id: str | None
    kind: str
    headline: TextSpec
    slots: tuple[SlotContract, ...] = ()
    subhead: TextSpec | None = None
    evidence: tuple[str, ...] = ()
    required_visual: str | None = None
    visual_limits: dict = field(default_factory=dict)
    target_density: float | None = None
    outline_kind: str = ""
    divider_label: str | None = None
    photo: str | None = None
    photo_caption: str | None = None
    gap_note: str | None = None
    # Запасные раскладки планировщика (`PatternAssignment.alternatives`):
    # писатель их не видит, они едут со слайдом до сборки.
    alternatives: tuple[str, ...] = ()
    # Задача V1: данные графика или таблицы из источника
    # (`data_types.VisualIntent.payload`): писатель переносит их в
    # `visual` как есть и пишет подписи и вывод, запасной слайд строит
    # визуал по ним без модели. `visual_reason`: почему визуал нужен.
    visual_data: dict | None = None
    visual_reason: str | None = None

    @property
    def is_divider(self) -> bool:
        return self.divider_label is not None

    def to_prompt(self) -> dict:
        """То, что видит писатель: без номера раскладки в роли выбора (он
        уже решён), без координат."""
        out: dict = {
            "slide_id": self.slide_id,
            "intent": self.intent,
            "slide_kind": self.kind,
            "evidence": list(self.evidence),
            "headline": self.headline.to_dict(),
            "blocks": [slot.to_dict() for slot in self.slots],
        }
        if self.subhead is not None:
            out["subhead"] = {**self.subhead.to_dict(), "required": False}
        if self.required_visual in ("table", "chart"):
            out["visual"] = {"type": self.required_visual, **self.visual_limits}
            if self.visual_data is not None:
                out["visual"]["data"] = self.visual_data
            if self.visual_reason:
                out["visual"]["reason"] = self.visual_reason
        if self.target_density is not None:
            out["target_density"] = self.target_density
        return out


def _headline_spec(form) -> TextSpec:
    if form is None or form.headline is None:
        return TextSpec(target_words=6, max_words=8)
    limit = form.headline
    # Предел в словах без предела знаков у заголовка встречается у схемы
    # от модели; ниже трёх слов вывод не сформулировать.
    # Предел ниже `MIN_HEADLINE_CHARS` поднимается до него: вывод короче
    # не написать, а раскладки с такой рамкой планировщик и так штрафует
    # (`scoring.short_headline`), сюда они попадают только без выбора.
    chars = max(limit.max_chars, MIN_HEADLINE_CHARS) if limit.max_chars else 0
    return TextSpec(target_words=max(2, limit.target_words), max_words=max(3, limit.max_words), max_chars=chars)


def _slot(part: FormPart, count: int, *, required: bool = True) -> SlotContract:
    if part.block == "kpi":
        value = TextSpec(target_words=1, max_words=_KPI_VALUE_MAX_WORDS)
        label = TextSpec.of(part.title) if part.title is not None else None
        return SlotContract(
            block="kpi", count=count, item=value, role=part.role, title=label, title_slot=part.title_slot,
            purpose=part.purpose, content_hint=part.content_hint, required=required,
        )
    item = TextSpec.of(list_item_limit(part, count))
    title = TextSpec.of(part.title) if part.block == "cards" and part.title is not None else None
    return SlotContract(
        block=part.block, count=count, item=item, role=part.role, title=title, title_slot=part.title_slot,
        purpose=part.purpose, content_hint=part.content_hint, required=required,
    )


def _content_slots(intent, form) -> list[SlotContract]:
    if form is None:
        return [SlotContract(block="bullets", count=max(1, intent.items), item=TextSpec(12, 16), role="bullet")]
    main = form.main
    extras = [p for p in form.parts if not p.required]
    if intent.is_hero:
        # Подпись под заголовком обложки или финала, если у раскладки есть
        # место: одна строка, не список.
        if main is not None and main.block in ("text", "bullets"):
            return [_slot(FormPart(block="text", units=1, unit=main.unit, role=main.role,
                                   purpose=main.purpose, content_hint=main.content_hint), 1, required=False)]
        return []
    if main is None:
        return []
    items = max(1, intent.items)
    if main.block == "cards":
        count = max(2, min(items, main.units))
    elif main.block == "text":
        # Колонки: по единице содержания на текстовое место, лишние места
        # необязательны.
        slots = [_slot(main, 1)]
        for i, extra in enumerate(extras):
            slots.append(_slot(extra, 1, required=i + 1 < items))
        return slots
    elif main.block in ("quote",):
        count = 1
    else:
        count = max(1, min(items, main.units))
    return [_slot(main, count)] + [_slot(extra, 1, required=False) for extra in extras]


def _matching_visual_intent(intent):
    """Визуал слоя данных, если он того же вида, что визуал контракта."""
    vi = getattr(intent, "visual_intent", None)
    if vi is None or intent.required_visual not in ("table", "chart") or vi.type != intent.required_visual:
        return None
    return vi


def _visual_data(intent) -> dict | None:
    vi = _matching_visual_intent(intent)
    return vi.payload() if vi is not None else None


def _visual_reason(intent) -> str | None:
    vi = _matching_visual_intent(intent)
    return vi.reason if vi is not None else None


def _chart_takes_main(intent, form) -> bool:
    """График встаёт на главное текстовое место раскладки (у неё нет ни
    родного графика, ни картинки-графика): писать туда нечего, пункты
    легли бы под график."""
    return intent.required_visual == "chart" and form is not None and form.chart_tier == CHART_TIER_TEXT


def build_contract(assignment, profile, style=None) -> SlideContract:
    """Контракт одного слайда из назначения планировщика."""
    intent = assignment.intent
    pattern = next((p for p in profile.patterns if p.pattern_id == assignment.pattern_id), None) \
        if profile is not None else None
    form = pattern_form(pattern) if pattern is not None else None
    policy = load_style(style) if style is not None else None
    limits = {}
    if intent.required_visual in ("table", "chart"):
        cap = pattern.capacity if pattern is not None else None
        limits = {
            key: (getattr(cap, key, 0) or default) if cap is not None else default
            for key, default in _DEFAULT_VISUAL_LIMITS.items()
        }
    return SlideContract(
        slide_id=assignment.position,
        intent=intent.intent,
        pattern_id=assignment.pattern_id,
        kind=assignment.kind,
        headline=_headline_spec(form),
        slots=tuple(_content_slots(intent, form)) if not intent.divider and not _chart_takes_main(intent, form) else (),
        subhead=TextSpec.of(form.subhead) if form is not None and form.subhead is not None and not intent.divider else None,
        evidence=tuple(intent.needs),
        required_visual=intent.required_visual,
        visual_limits=limits,
        target_density=policy.target_density if policy is not None else None,
        outline_kind=intent.outline_kind,
        divider_label=intent.label if intent.divider else None,
        photo=intent.photo,
        photo_caption=intent.photo_caption,
        gap_note=assignment.gap_note(),
        alternatives=tuple(getattr(assignment, "alternatives", ()) or ()),
        visual_data=_visual_data(intent),
        visual_reason=_visual_reason(intent),
    )


def build_contracts(assignments, profile, style=None) -> list[SlideContract]:
    return [build_contract(a, profile, style) for a in assignments]


# ---------------------------------------------------------------------------
# Проверка ответа писателя по контракту
# ---------------------------------------------------------------------------


def _units_of(block) -> list[tuple[str, str | None]]:
    """(основной текст, заголовок/подпись) каждой единицы блока."""
    if isinstance(block, TextBlock):
        return [(block.text, None)]
    if isinstance(block, BulletBlock):
        return [(item, None) for item in block.items]
    if isinstance(block, CardBlock):
        return [(card.body, card.title) for card in block.items]
    if isinstance(block, KpiBlock):
        return [(kpi.value, kpi.label) for kpi in block.items]
    if isinstance(block, QuoteBlock):
        return [(block.text, None)]
    return []


def _match(slide: SlideSpec, contract: SlideContract) -> tuple[list[tuple[SlotContract, object | None]], list]:
    """Блоки ответа по местам контракта, в порядке контракта: каждому месту
    первый ещё не взятый блок того же типа. Второе значение: блоки, которым
    места не нашлось."""
    left = list(slide.blocks)
    pairs = []
    for slot in contract.slots:
        cls = _BLOCK_TYPES[slot.block]
        found = next((b for b in left if isinstance(b, cls)), None)
        if found is not None:
            left.remove(found)
        pairs.append((slot, found))
    return pairs, left


_BLOCK_TITLES = {"text": "абзац", "bullets": "список", "cards": "карточки", "kpi": "показатели", "quote": "цитата"}


def contract_problems(slide: SlideSpec, contract: SlideContract) -> list[str]:
    """Нарушения контракта человеческими словами, для ремонтного вызова и
    находки. Пустой список: ответ укладывается."""
    if contract.is_divider:
        return []
    problems: list[str] = []
    headline = contract.headline.problem(slide.headline, "заголовок")
    if headline:
        problems.append(headline)
    if slide.subhead and contract.subhead is None:
        problems.append("подзаголовок: места под него у раскладки нет, убери его")
    elif slide.subhead and contract.subhead is not None:
        sub = contract.subhead.problem(slide.subhead, "подзаголовок")
        if sub:
            problems.append(sub)
    pairs, extra = _match(slide, contract)
    for i, (slot, block) in enumerate(pairs):
        name = f"блок {i + 1} ({_BLOCK_TITLES[slot.block]})"
        if block is None:
            if slot.required:
                problems.append(f"{name}: нет в ответе, нужен тип {slot.block!r}")
            continue
        units = _units_of(block)
        if slot.block in ("cards", "kpi", "bullets") and len(units) != slot.count:
            problems.append(f"{name}: {len(units)} единиц, нужно ровно {slot.count}")
        for j, (text, title) in enumerate(units):
            issue = slot.item.problem(text, f"{name}, единица {j + 1}")
            if issue:
                problems.append(issue)
            if title and slot.title is not None:
                issue = slot.title.problem(title, f"{name}, заголовок единицы {j + 1}")
                if issue:
                    problems.append(issue)
    for block in extra:
        problems.append(f"лишний блок типа {type(block).__name__}: места под него у раскладки нет")
    visual = slide.visual
    if contract.required_visual in ("table", "chart"):
        if visual is None or visual.kind != contract.required_visual:
            problems.append(f"нужен visual типа {contract.required_visual!r}")
    elif visual is not None and visual.kind in ("table", "chart"):
        problems.append(f"visual {visual.kind!r}: места под него у раскладки нет, убери его")
    return problems


def contract_fill(slide: SlideSpec, contract: SlideContract) -> tuple[int, int]:
    """(мест в пределах контракта, мест всего): заголовок и каждая
    единица каждого обязательного или заполненного места. Для отчёта о
    том, насколько текст лёг в композицию."""
    if contract.is_divider:
        return 1, 1
    total = 1
    ok = 1 if contract.headline.problem(slide.headline, "") is None else 0
    pairs, _extra = _match(slide, contract)
    for slot, block in pairs:
        if block is None:
            total += slot.count if slot.required else 0
            continue
        for text, _title in _units_of(block):
            total += 1
            ok += slot.item.problem(text, "") is None
    return ok, total


def plan_contracts(outline_or_intents, profile, style, *, beam_width: int | None = None):
    """Раскладки на всю колоду стиля и контракты под них: один вызов на
    стиль для пайплайна (CLI, API). Без модели, миллисекунды."""
    from deckforge.pattern.planner import DEFAULT_BEAM_WIDTH, plan_patterns

    assignments = plan_patterns(
        outline_or_intents, profile, style, beam_width=beam_width or DEFAULT_BEAM_WIDTH,
    )
    return assignments, build_contracts(assignments, profile, style)
