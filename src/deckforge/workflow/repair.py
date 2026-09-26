"""Структурная починка слайда во время сборки (раздел 13.2, ступень 3
лестницы отказов `compose.builder._place_with_ladder`).

Сборка знает, что клон раскладки отклонён и почему, но модель не зовёт:
`compose/` рисует, а не пишет. Починка текста живёт здесь, на уровне
пайплайна, и сборка получает её готовой (`build_deck(..., repair=...)`).
API (`api.jobs`) и командная строка (`cli`) заводят её одинаково, через
`SlideRepairer`, чтобы оба пути чинили слайды по одним правилам.

Контракт для починки строится заново от раскладки, в которую слайд не
лёг: после письма колода могла потерять слайды (повторы) и поменять
нумерацию, и контракт писателя по номеру уже не найти. Строится тем же
`plan.contracts.build_contract`, что и у писателя, поэтому пределы те же."""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field

from deckforge.pattern.intent import SlideIntent
from deckforge.pattern.planner import PatternAssignment
from deckforge.plan.contracts import SlideContract, build_contract
from deckforge.plan.outline import SourceDoc
from deckforge.plan.spec import BulletBlock, CardBlock, KpiBlock, SlideSpec
from deckforge.plan.writer import shorten_to_contract
from deckforge.provider.base import LLMProvider

# Вызовов починки на колоду одного стиля. Каждый вызов идёт посреди
# сборки, последовательно по слайдам, и стоит 5-15 секунд модели: четыре
# вызова это до минуты в худшем случае при бюджете ТЗ пять минут. Живой
# прогон задачи P: клоном не собралось 3-4 слайда из 12 на стиль.
DEFAULT_MAX_CALLS = 4


def _units(slide: SlideSpec) -> int:
    for block in slide.blocks:
        if isinstance(block, (CardBlock, BulletBlock, KpiBlock)):
            return len(block.items)
    return len(slide.blocks)


def contract_for_slide(slide: SlideSpec, pattern_id: str, profile, style=None) -> SlideContract:
    """Контракт места `pattern_id` под уже написанный слайд: число единиц
    берётся из самого слайда, чтобы починка сокращала текст, а не
    выбрасывала карточки."""
    visual = slide.visual.kind if slide.visual is not None and slide.visual.kind in ("table", "chart") else None
    photo = slide.visual.photo_name if slide.visual is not None and slide.visual.kind == "photo" else None
    intent = SlideIntent(
        index=slide.index, outline_kind="context", intent=slide.headline, items=_units(slide),
        form=visual, photo=photo,
        photo_caption=slide.visual.caption if photo and slide.visual is not None else None,
    )
    kind = next((p.kind for p in profile.patterns if p.pattern_id == pattern_id), slide.kind)
    assignment = PatternAssignment(position=slide.index, intent=intent, pattern_id=pattern_id, kind=kind)
    return build_contract(assignment, profile, style)


def repair_slide(
    slide: SlideSpec, pattern_id: str, problems: list[str], *, profile, llm: LLMProvider,
    sources: list[SourceDoc], style=None, total: int = 0,
) -> SlideSpec | None:
    """Один вызов писателя «сократи под контракт этого места». `None`, если
    модель не справилась; сборка тогда идёт дальше по лестнице."""
    contract = contract_for_slide(slide, pattern_id, profile, style)
    style_value = getattr(style, "value", style)
    return shorten_to_contract(slide, contract, llm, sources, problems, total=total, style=style_value)


@dataclass
class SlideRepairer:
    """Реализация `compose.builder.SlideRepair` для одной колоды: держит
    модель, источники и лимит вызовов. Без модели (`llm=None`) ступень
    сокращения просто пропускается, лестница идёт к разбиению и сборке с
    нуля.

    Лимит двойной: число вызовов (`max_calls`) и дедлайн по часам
    (`deadline`, `time.monotonic()`), после которого починка не начинается:
    лучше слайд с нуля, чем колода позже пяти минут. Одновременность
    вызовов модели ограничивает писатель (`plan.writer._MODEL_SLOTS`, общий
    на процесс), здесь её не ограничиваем второй раз."""
    profile: object
    llm: LLMProvider | None
    sources: list[SourceDoc]
    style: object = None
    max_calls: int = DEFAULT_MAX_CALLS
    deadline: float | None = None
    total: int = 0
    calls: int = 0
    accepted: int = 0
    log: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def shorten(self, slide_spec: SlideSpec, pattern_id: str, problems: list[str]) -> SlideSpec | None:
        if self.llm is None:
            return None
        with self._lock:
            if self.calls >= self.max_calls:
                self.log.append(f"Слайд {slide_spec.index}: лимит сокращений ({self.max_calls}) исчерпан.")
                return None
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self.log.append(f"Слайд {slide_spec.index}: время на сокращение текста вышло.")
                return None
            self.calls += 1
        started = time.monotonic()
        result = repair_slide(
            slide_spec, pattern_id, problems, profile=self.profile, llm=self.llm, sources=self.sources,
            style=self.style, total=self.total,
        )
        with self._lock:
            self.accepted += result is not None
            self.log.append(
                f"Слайд {slide_spec.index}: сокращение под раскладку {pattern_id!r} "
                f"{'получено' if result is not None else 'не удалось'} за {time.monotonic() - started:.1f}с."
            )
        return result
