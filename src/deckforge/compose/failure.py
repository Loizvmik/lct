"""Почему кандидат раскладки не принят и что с этим делать (раздел 12-13
идей четвёртой редакции, задача V3).

Лестница отказов сборки (`compose.builder._place_with_ladder`) раньше шла
одним путём для любой беды: клон, запасная раскладка, сокращение текста,
разбиение, с нуля. Но сокращение текста не помогает раскладке, у которой
нет места под карточки, а поиск раскладки «просторнее» бесполезен, когда
карточек больше, чем держит любой повтор. Поэтому каждая неудачная попытка
теперь называет причину (`Failure`), а `RepairPolicy` по главной причине
выбирает, какие ступени пробовать и в каком порядке.

Сюда же предел ужимания кегля (`FontBudget`, раздел 25): маленький кегль
спасает текст, но убивает дизайн, и глубже предела это отказ
`TEXT_OVERFLOW`, а не успех. Предел считают одной функцией сборка
(`builder._fit_cloned_text`) и аудит (T02), иначе они разошлись бы в том,
что считать браком.

Модель здесь не зовётся: классификация по кодам аудита и отказов клона,
сокращение без модели (`condense_to_theses`) режет по предложениям."""
from __future__ import annotations
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from deckforge.plan.spec import BulletBlock, CardBlock, SlideSpec, TextBlock
from deckforge.template.typography import fill_scale_gaps

# Категории в порядке старшинства: когда у попытки несколько причин,
# главной считается самая структурная. Лишние единицы не лечатся ни
# текстом, ни кеглем; отсутствие места под блок не лечится сокращением;
# переполнение лечится текстом; наложение и пустота только раскладкой.
CATEGORIES = (
    "TOO_MANY_UNITS", "MISSING_SLOT", "TEXT_OVERFLOW", "OVERLAP", "BUILD_ERROR", "LOW_DENSITY",
    "MODEL_FAILURE",
)

# Код отказа клона (`builder.CloneOutcome.code`) и собственные коды
# лестницы -> категория.
_CODE_CATEGORY = {
    "LAYOUT_NOT_FOUND": "MISSING_SLOT",
    "NO_EXAMPLE": "MISSING_SLOT",
    "EMPTY_SLOT_TEXT": "MISSING_SLOT",
    "BLOCK_NO_SLOT": "MISSING_SLOT",
    "HEADLINE_NO_SHAPE": "MISSING_SLOT",
    "SLOT_NO_SHAPE": "MISSING_SLOT",
    "UNITS_OVER_REPEAT": "TOO_MANY_UNITS",
    "FONT_BUDGET": "TEXT_OVERFLOW",
    "UNDERFILLED": "LOW_DENSITY",
    "CLONE_EXCEPTION": "BUILD_ERROR",
    "NO_MODEL": "MODEL_FAILURE",
    "SHORTEN_EMPTY": "MODEL_FAILURE",
    "SHORTEN_ERROR": "MODEL_FAILURE",
}

# Находки аудита, отклоняющие клон (`audit.deterministic.audit_slide_
# layout`), и T02 предела кегля -> категория.
_AUDIT_CATEGORY = {
    "L01": "OVERLAP", "L02": "OVERLAP", "L03": "TEXT_OVERFLOW", "L04": "TEXT_OVERFLOW",
    "T02": "TEXT_OVERFLOW", "D05": "LOW_DENSITY",
}

# Кто может исправить: `local` (ступень кегля, сдвиг рамки), `structural`
# (другая раскладка, другой текст), `none`. Совпадает с `Finding.repair`.
_REPAIR_RANK = {"none": 0, "local": 1, "structural": 2}


@dataclass(frozen=True)
class Failure:
    """Одна причина отказа кандидата. `code`: код находки аудита (L03) или
    отказа клона (UNITS_OVER_REPEAT); `pattern_id`: на какой раскладке."""
    category: str
    code: str
    recoverable_by: str = "structural"
    detail: str = ""
    pattern_id: str | None = None

    @property
    def label(self) -> str:
        return f"{self.category}/{self.code}"


def classify(code: str, detail: str = "", pattern_id: str | None = None) -> Failure:
    """Отказ по коду клона или лестницы. Незнакомый код считается ошибкой
    сборки: лучше другая раскладка, чем молчаливое «всё хорошо»."""
    category = _CODE_CATEGORY.get(code, "BUILD_ERROR")
    return Failure(category, code, "structural", detail, pattern_id)


def from_findings(findings: Iterable, pattern_id: str | None = None) -> list[Failure]:
    """Находки аудита -> отказы, одна на код. `recoverable_by` берётся из
    `Finding.repair` (самая тяжёлая среди находок этого кода): L03 с малым
    переполнением чинится кеглем, с большим только структурно."""
    by_code: dict[str, Failure] = {}
    for f in findings:
        category = _AUDIT_CATEGORY.get(f.check_id, "BUILD_ERROR")
        repair = getattr(f, "repair", "structural") or "structural"
        known = by_code.get(f.check_id)
        if known is None or _REPAIR_RANK.get(repair, 2) > _REPAIR_RANK.get(known.recoverable_by, 2):
            by_code[f.check_id] = Failure(category, f.check_id, repair, str(getattr(f, "message", ""))[:200], pattern_id)
    return list(by_code.values())


def primary(failures: Iterable[Failure]) -> Failure | None:
    """Главная причина: самая старшая категория, при равенстве первая по
    времени (раскладка планировщика важнее запасных)."""
    ranked = [(CATEGORIES.index(f.category) if f.category in CATEGORIES else len(CATEGORIES), i, f)
              for i, f in enumerate(failures)]
    return min(ranked, key=lambda t: t[:2])[2] if ranked else None


# ---------------------------------------------------------------------------
# Политика починки
# ---------------------------------------------------------------------------

# Ступени, из которых собирается путь починки. Первые четыре пробуют клон
# другой раскладки с тем же текстом, различаясь тем, какие раскладки и в
# каком порядке: `roomier` по текстовой вместимости, `alternate` с местами
# под все блоки, `larger` с повтором не меньше числа единиц, `denser` с
# меньшей вместимостью. `accept_best`: самый заполненный из принятых
# аудитом, но пустоватых клонов. `shorten`: сокращение моделью,
# `fallback_text`: без модели, `split`: два слайда, `scratch`: с нуля.
STEPS = (
    "roomier", "alternate", "larger", "denser", "accept_best", "shorten", "fallback_text", "split", "scratch",
)

DEFAULT_POLICY: dict[str, tuple[str, ...]] = {
    "TEXT_OVERFLOW": ("roomier", "shorten", "split", "scratch"),
    "MISSING_SLOT": ("alternate", "scratch"),
    "TOO_MANY_UNITS": ("larger", "split", "scratch"),
    "LOW_DENSITY": ("denser", "accept_best"),
    "OVERLAP": ("alternate", "scratch"),
    "BUILD_ERROR": ("alternate", "scratch"),
    "MODEL_FAILURE": ("fallback_text",),
}


@dataclass(frozen=True)
class RepairPolicy:
    table: dict[str, tuple[str, ...]]

    def steps(self, category: str) -> tuple[str, ...]:
        return self.table.get(category) or DEFAULT_POLICY.get(category) or ("alternate", "scratch")

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> "RepairPolicy":
        """Таблица из `compose.repair_policy` поверх `DEFAULT_POLICY`.
        Незнакомая ступень или категория это опечатка в конфиге: падаем
        сразу, а не молча идём другим путём."""
        table = dict(DEFAULT_POLICY)
        for category, steps in (mapping or {}).items():
            if category not in CATEGORIES:
                raise ValueError(f"compose.repair_policy: неизвестная категория {category!r}")
            unknown = [s for s in steps if s not in STEPS]
            if unknown:
                raise ValueError(f"compose.repair_policy.{category}: неизвестные ступени {unknown}")
            table[category] = tuple(steps)
        return cls(table)


# ---------------------------------------------------------------------------
# Предел ужимания кегля
# ---------------------------------------------------------------------------


# Виды раскладок, где заголовок и есть содержание: обложка, разделитель и
# финал в профиле шаблона все вида `section`.
HERO_KINDS = frozenset({"section"})


@dataclass(frozen=True)
class FontBudget:
    """Кегль при подгонке не ниже `min_ratio` от кегля примера и не больше
    `max_steps` ступеней шкалы шаблона вниз. Берётся более строгое из двух:
    у заголовка 44pt по шкале с промежуточными ступенями 40 и 36 предел
    36, у текста 16pt при шкале 16/14/12 предел 14 (0,8 дали бы 12,8)."""
    min_ratio: float = 0.8
    max_steps: int = 2
    # Заголовок героической раскладки (обложка, разделитель, финал): его
    # даёт пользователь, сокращать нельзя, а сборка обложки с нуля хуже,
    # чем заголовок мельче примера. Поэтому предел мягче.
    hero_min_ratio: float = 0.6
    hero_max_steps: int = 4

    def for_slot(self, role: str, pattern_kind: str) -> "FontBudget":
        """Предел для конкретного места: у заголовка героической раскладки
        свой, у остальных общий."""
        if role == "headline" and pattern_kind in HERO_KINDS:
            return FontBudget(self.hero_min_ratio, self.hero_max_steps, self.hero_min_ratio, self.hero_max_steps)
        return self

    def floor_pt(self, native_pt: float, scale_pt: Iterable[float]) -> float:
        if native_pt <= 0:
            return 0.0
        # Кегль примера входит в шкалу: между ним и ближайшей ступенью ниже
        # `fill_scale_gaps` вставляет промежуточные, как и сборка.
        ladder = fill_scale_gaps([*scale_pt, native_pt])
        below = sorted({round(v, 2) for v in ladder if v < native_pt - 0.05}, reverse=True)
        if self.max_steps <= 0 or not below:
            by_steps = native_pt
        else:
            by_steps = below[min(self.max_steps, len(below)) - 1]
        return max(native_pt * self.min_ratio, by_steps)


_APP_YAML = Path(__file__).resolve().parents[3] / "config" / "app.yaml"


@lru_cache(maxsize=1)
def _compose_config():
    from deckforge.settings import Settings
    try:
        return Settings.load(_APP_YAML).compose
    except Exception:  # noqa: BLE001: нет конфига (тесты на чужом дереве): значения по умолчанию
        return None


def repair_policy() -> RepairPolicy:
    config = _compose_config()
    return RepairPolicy.from_mapping(config.repair_policy if config is not None else None)


def font_budget() -> FontBudget:
    config = _compose_config()
    if config is None:
        return FontBudget()
    budget = config.font_degradation_budget
    return FontBudget(
        min_ratio=budget.min_ratio, max_steps=budget.max_steps,
        hero_min_ratio=budget.hero_headline_min_ratio, hero_max_steps=budget.hero_headline_max_steps,
    )


# ---------------------------------------------------------------------------
# Сокращение без модели
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[«\"(A-ZА-ЯЁ0-9])")


def _first_sentence(text: str) -> str:
    head = _SENTENCE_END.split(text.strip(), maxsplit=1)[0].strip()
    return head or text


def condense_to_theses(slide_spec: SlideSpec) -> SlideSpec | None:
    """Запасной текст, когда модель не ответила: у каждого пункта,
    карточки и абзаца остаётся первое предложение, тезис. Факты из хвоста
    теряются, поэтому вызывающий пишет находку. `None`: сокращать нечего
    (все пункты уже в одно предложение)."""
    changed = False
    blocks = []
    for block in slide_spec.blocks:
        if isinstance(block, BulletBlock):
            items = [_first_sentence(t) for t in block.items]
            changed |= items != list(block.items)
            blocks.append(replace(block, items=items))
        elif isinstance(block, CardBlock):
            cards = [replace(c, body=_first_sentence(c.body)) for c in block.items]
            changed |= any(a.body != b.body for a, b in zip(cards, block.items))
            blocks.append(replace(block, items=cards))
        elif isinstance(block, TextBlock):
            text = _first_sentence(block.text)
            changed |= text != block.text
            blocks.append(replace(block, text=text))
        else:
            blocks.append(block)
    subhead = _first_sentence(slide_spec.subhead) if slide_spec.subhead else slide_spec.subhead
    changed |= subhead != slide_spec.subhead
    if not changed:
        return None
    return replace(slide_spec, blocks=blocks, subhead=subhead, findings=[])
