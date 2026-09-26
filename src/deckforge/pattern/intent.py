"""Намерение слайда (`SlideIntent`): что планировщику паттернов известно о
слайде ДО того, как написан текст.

Пункт структуры (`plan.outline.OutlineSlide`) несёт смысл, а планировщику
нужны ещё и числа: сколько единиц содержания (пунктов, карточек,
показателей) слайд хочет показать, нужна ли таблица, график или фото. Эти
числа и решают жёсткие ограничения выбора раскладки: четыре пункта не
положить в сетку на три, таблицу не положить туда, где нет места под
таблицу. Текста ещё нет, поэтому число элементов берётся из структуры
(`items`, если модель структуры его назвала, иначе число `needs`, иначе
типичное для вида пункта)."""
from __future__ import annotations
from dataclasses import dataclass, field

# Объём колоды по ТЗ: 10-15 слайдов. Разделители airy не выводят за верх.
MAX_SLIDES = 15

# Полными фразами, не словами-темами: разделитель использует ту же
# героическую раскладку, что и содержательные слайды, и голое «Данные» в
# ней читается как брак (перенесено из `plan.variants`).
DIVIDER_LABELS = (
    "Дальше — контекст", "Дальше — цифры", "Дальше — решение",
    "Дальше — результаты", "Дальше — риски", "Дальше — план", "Дальше — итоги",
)

# Виды пункта, у которых слайд героический: заголовок и, может быть,
# одна строка. Содержательных единиц у них нет.
HERO_OUTLINE_KINDS = frozenset({"title", "closing"})

# Типичное число единиц содержания по виду пункта, когда структура его не
# назвала и `needs` пуст. Порядок величин с живых прогонов 23-27 сентября
# 2026: повестка и дорожная карта обычно 4 пункта, сравнение 2 стороны.
_DEFAULT_ITEMS = {
    "title": 0, "closing": 0, "ask": 1, "agenda": 4, "context": 3, "problem": 2,
    "solution": 3, "how_it_works": 3, "data": 3, "comparison": 2, "case": 3,
    "roadmap": 4, "team": 3, "risks": 3,
}
_MAX_ITEMS = 8

# Формы, которые модель структуры может заказать явно. Цитату и показатель
# код сам не назначает: выдуманная цитата и число без источника хуже
# обычного списка.
FORMS = ("quote", "kpi", "table", "chart")


@dataclass(frozen=True)
class SlideIntent:
    # Номер пункта структуры; у разделителя -1 (его в структуре нет).
    index: int
    outline_kind: str
    intent: str
    needs: tuple[str, ...] = ()
    items: int = 0
    form: str | None = None
    photo: str | None = None
    photo_caption: str | None = None
    # Разделитель стиля airy: текста не пишет никто, заголовок статичный.
    divider: bool = False
    label: str | None = None
    # Визуал пункта из слоя типов данных (`plan.data_types.VisualIntent`,
    # через `OutlineSlide.visual_intent`): едет в контракт писателя как
    # есть, чтобы числа графика и таблицы были числами источника, а не
    # пересказом модели.
    visual_intent: object | None = field(default=None, compare=False)

    @property
    def is_hero(self) -> bool:
        return self.divider or self.outline_kind in HERO_OUTLINE_KINDS

    @property
    def required_visual(self) -> str | None:
        if self.form in ("table", "chart"):
            return self.form
        if self.photo:
            return "photo"
        return None


def _items_of(slide) -> int:
    explicit = getattr(slide, "items", None)
    if slide.kind in HERO_OUTLINE_KINDS:
        return 0
    if isinstance(explicit, int) and explicit > 0:
        return min(explicit, _MAX_ITEMS)
    if slide.needs:
        return max(1, min(len(slide.needs), 6))
    return _DEFAULT_ITEMS.get(slide.kind, 3)


def intents_from_outline(outline, photos: dict[int, tuple[str, str | None]] | None = None) -> list[SlideIntent]:
    """Намерения по пунктам структуры. `photos`: номер пункта -> (имя файла
    фото, подпись), распределение фотографий контент-пакета до планирования
    (`plan.photos.assign_photos` по скелету структуры): слайд с фото обязан
    получить раскладку с местом под картинку, а это решается здесь, а не
    после текста."""
    photos = photos or {}
    result: list[SlideIntent] = []
    for i, slide in enumerate(outline.slides):
        form = getattr(slide, "form", None)
        photo = photos.get(i)
        result.append(SlideIntent(
            index=i, outline_kind=slide.kind, intent=slide.intent, needs=tuple(slide.needs),
            items=_items_of(slide), form=form if form in FORMS else None,
            photo=photo[0] if photo else None, photo_caption=photo[1] if photo else None,
            visual_intent=getattr(slide, "visual_intent", None),
        ))
    return result


def with_dividers(intents: list[SlideIntent]) -> list[SlideIntent]:
    """Разделители стиля airy перед содержательными слайдами, кроме первого
    и кроме героических. Бюджет считается до цикла: ни один исходный слайд
    не пропускается, ограничено только число добавочных, чтобы колода
    осталась в 10-15 слайдах ТЗ. Перенесено из `plan.variants._with_dividers`:
    разделитель теперь получает раскладку от планировщика, как любой слайд."""
    if not intents:
        return intents
    budget = max(0, MAX_SLIDES - len(intents))
    result = [intents[0]]
    label_i = 0
    for intent in intents[1:]:
        if budget > 0 and not intent.is_hero:
            label = DIVIDER_LABELS[label_i % len(DIVIDER_LABELS)]
            result.append(SlideIntent(
                index=-1, outline_kind="divider", intent=label, divider=True, label=label,
            ))
            label_i += 1
            budget -= 1
        result.append(intent)
    return result
