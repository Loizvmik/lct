"""Словарь автофигур шаблона — какой формой этот шаблон рисует КАРТОЧКУ.

`compose/diagrams.py` (Task 10) рисует карточки схем (process/cycle/
hierarchy/...) формой из этого словаря — прямоугольник, скруглённый
прямоугольник или овал, — а не скруглённой карточкой по вкусу генератора.

## Откуда берётся форма (Task 10 код-ревью, находка №1)

Первая версия этого модуля считала ПЕРЕПИСЬ всех `a:prstGeom` по слайдам,
макетам И мастерам пакета разом. На деле макеты/мастера несут служебные
рамки (фон, плейсхолдеры, декоративные обводки) в количестве, которое
перевешивает любой язык, который дизайнер реально использует для карточек
на СЛАЙДАХ. Собственный замер этой правки (разовый скрипт код-ревью, не
часть пакета — открывает каждый учебный `.pptx` и считает `rect`/
`roundRect`/`ellipse` тем же обходом, что и старый алгоритм, слайды+
макеты+мастера вместе):

    VK Tech:    rect 1643, roundRect 176, ellipse 18   → rect побеждает
    WorkSpace:  rect  160, roundRect 127, ellipse  8   → rect побеждает
    Education:  rect  286, roundRect  27, ellipse 100  → rect побеждает

Прямой угол побеждает на всех трёх. Если считать ТОЛЬКО по слайдам (без
макетов/мастеров) — WorkSpace уже даёт `roundRect` 127 против `rect` 122
(нужный ответ, но чужой формы всё ещё больше вокруг: `rect` — вторая по
частоте на слайдах WorkSpace, а не третья), Education остаётся за `rect`
(201 против 100 у `ellipse`) — почти весь этот `rect` на деле крупные
фотоконтейнеры/содержательные блоки, а не карточная плашка (у карточных
раскладок Education своего декоративного повтора вовсе нет, см. ниже).
Требование «форма из шаблона» формально соблюдалось (прямоугольник в
словаре шаблона правда есть), но различения между тремя шаблонами не
было — везде побеждал прямой угол одной и той же причиной (масса
служебных/содержательных прямоугольников, не имеющих отношения к
карточкам схем).

Решение — не считать ВСЕ автофигуры пакета, а опереться на то, что уже
УМЕЕТ отличать декоративную плашку карточки от служебной рамки: майнинг
раскладок (`template/patterns.py`) снимает композицию РЕАЛЬНЫХ слайдов-
примеров и помечает декор, который шагает той же сеткой, что и
повторяющиеся карточки контента (`DecorShape.repeat_group` — Task 9
повторное ревью, находка №1, "главная находка"). Такой декор — ровно
карточные плашки, форма которых и есть язык карточек ЭТОГО шаблона.
`build_shape_vocabulary` теперь считает гистограмму `prst` ПО ЭТОМУ ДЕКОРУ
(`_card_decor_vocabulary`), а не по всем автофигурам пакета. Тот же
собственный замер, по этому декору:

    VK Tech:    rect 27, roundRect 20, ellipse 3   → rect побеждает
    WorkSpace:  ellipse 6, roundRect 4             → ellipse побеждает
    Education:  ellipse 2 (единственная запись)    → ellipse побеждает
    ЛЦТ2026:    roundRect 18, rect 4               → roundRect побеждает

Три разных победителя на четырёх шаблонах — то самое различение, которого
не было у полной переписи; ЛЦТ2026 (контрольный, в тестах не участвует)
измерен тем же кодом для полноты картины.

## Карточка обрамляет текст, а не картинку (Task 10, вторая правка код-ревью)

Обязательный осмотр рендера первой правки нашёл дефект: схема «процесс» на
WorkSpace рисовалась гигантскими белыми овалами — форма пришла от
`ellipse`, победившего по декору группы повтора. Но у собственной
"cards"-раскладки WorkSpace декора вовсе нет (`decor_n=0`, см. "Сомнения"
отчёта первой правки) — сигнал шёл от декора ДРУГИХ раскладок: круглых
аватар-плашек прощального слайда и скруглённых бейджей под иконку в
списке. Ни то ни другое не карточка — карточка обрамляет ТЕКСТ, аватар и
бейдж обрамляют картинку/иконку.

Различение доступно в уже намайненных данных: раскладка знает не только
свой декор, но и свои текстовые слоты (`Pattern.slots`) с их боксами.
`_card_decor_vocabulary` теперь засчитывает декор группы повтора карточным,
только если внутри него (с точностью до `_TEXT_CONTAINMENT_THRESHOLD` = 95%
площади слота, слоту разрешено торчать за край плашки на пару процентов —
типографский инсет) лежит хотя бы один слот НЕ из `_NON_TEXT_ROLES`
(`image`/`icon`/`table`/`chart` — независимая копия одноимённого приватного
множества `patterns.py`, тот же принцип модульной границы, что и
`DecorShape.prst`/`adj`, см. её докстроку). Тот же собственный замер этой
правки, теперь по ТЕКСТОНЕСУЩЕМУ декору групп повтора:

    VK Tech:    roundRect 12, rect 7                → roundRect побеждает
    WorkSpace:  card-декора с текстом не нашлось     → запасной путь
    Education:  card-декора с текстом не нашлось     → запасной путь
    ЛЦТ2026:    roundRect 16 (единственная запись)   → roundRect побеждает

На WorkSpace и Education отбор по тексту убрал ВЕСЬ прежний сигнал (он и
был целиком декором без текста — ровно диагноз выше) — работает запасной
путь ниже; на VK Tech заодно ушли мелкие фигуры вроде буллет-точки
(0.012% площади холста, `roundRect`, слот их не касался вовсе) и
акцентные плашки, только частично перекрытые текстом (`rect`/`roundRect`
с перекрытием 0.5-0.87 площади слота, ниже порога) — второе сомнение
отчёта первой правки, снятое тем же отбором, что и главный дефект.

Если у шаблона нет ни одной раскладки с декором группы повтора, ИЛИ этот
декор нашёлся, но текста внутри ни у одного — запасной путь: перепись
автофигур, но ТОЛЬКО по слайдам (`_slide_vocabulary`), не по
макетам/мастерам — макеты несут служебные рамки, а не язык карточек (та
же причина, по которой полная перепись ошибалась выше), а слайды — то,
что дизайнер шаблона реально нарисовал руками. Этим путём теперь идут
WorkSpace (`roundRect` 127 против `rect` 122 — почти ничья, но нужный
ответ) и Education (`rect` 201 против `ellipse` 100 — по-прежнему в
основном крупные фотоконтейнеры, а не карточная плашка, честно
задокументированный компромисс запасного пути, не идеал).
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes

if TYPE_CHECKING:
    from deckforge.template.patterns import DecorShape, Pattern, PatternSlot

# Пресеты, которыми реально пользуется compose/diagrams.py — единственные
# три, что разведка нашла в заметном числе на всех четырёх шаблонах (см.
# докстроку модуля). Другие пресеты попадают в гистограмму, но диаграммы их
# не используют — не поддержаны разметкой скруглений/геометрии ниже.
KNOWN_AUTOSHAPE_PRESETS = ("rect", "roundRect", "ellipse")

# Роли слотов, не несущих текста как такового — независимая копия
# `patterns._NON_TEXT_ROLES` (тот же принцип модульной границы, что и
# `DecorShape.prst`/`adj` ниже: не импортировать чужое приватное имя
# поперёк `template/`, см. докстроку `DecorShape` в `patterns.py`).
_NON_TEXT_ROLES = frozenset({"image", "icon", "table", "chart"})

# Доля площади ТЕКСТОВОГО СЛОТА, которая обязана попасть внутрь декора,
# чтобы декор засчитался карточкой (обрамляет текст), а не бейджем/
# подложкой под картинку (Task 10, вторая правка код-ревью — см. докстроку
# модуля). Слот параметрической раскладки типично на пару процентов шире
# собственной плашки (текстовый инсет отступает от края фигуры, округление
# координат при майнинге) — порог 0.95 даёт вдвое больший запас (до 5%
# площади слота может остаться снаружи декора). Не подгонка под три файла:
# собственный замер этой правки (см. докстроку модуля) показал разрыв, а не
# постепенный спад — перекрытие либо около 1.0 (настоящая карточная
# плашка, слот целиком внутри), либо заметно ниже 0.9 (бейдж/акцентная
# плашка, накрывающая текст лишь частично, или вовсе 0.0 — декор без
# текста внутри).
_TEXT_CONTAINMENT_THRESHOLD = 0.95


@dataclass(frozen=True)
class ShapeVocabEntry:
    prst: str
    count: int
    # Степень скругления угла у roundRect — среднее значение параметра
    # скругления (`a:avLst/a:gd[@name="adj"]/@fmla`, доля 0..1 от меньшей
    # стороны фигуры согласно ECMA-376 §20.1.9.13 CT_PresetGeometry2D)
    # среди вхождений этого prst с непустым avLst. 0.0 у форм без параметра
    # скругления (rect, ellipse) или когда avLst не задан (шаблонное
    # значение самого prst).
    avg_adj: float = 0.0


def build_shape_vocabulary(
    pkg: PptxPackage, canvas: Canvas, patterns: "list[Pattern] | None" = None,
) -> list[ShapeVocabEntry]:
    """Гистограмма `prst` формы, которой шаблон рисует КАРТОЧКУ, по
    убыванию частоты (см. докстроку модуля).

    `patterns` — уже намайненные раскладки шаблона (`template.patterns.
    mine_patterns`, тот же объект, что `TemplateProfile.from_file` строит
    рядом, до pydantic-сериализации — см. `template/profile.py::from_file`);
    `None`/пустой список — как если бы карточного декора не нашлось вовсе
    (сразу запасной путь, нужно тестам/вызовам без мининга под рукой)."""
    card_entries = _card_decor_vocabulary(patterns or [])
    if card_entries:
        return card_entries
    return _slide_vocabulary(pkg, canvas)


def _card_decor_vocabulary(patterns: "list[Pattern]") -> list[ShapeVocabEntry]:
    """Гистограмма `prst` ПО ДЕКОРУ ГРУПП ПОВТОРА всех намайненных
    раскладок — карточные плашки, а не любой декор (логотип/разделительная
    линия декору повтора не принадлежат, см. `DecorShape.repeat_group`).

    Внутри декора группы повтора отдельно отсекается декор, который не
    обрамляет текст (Task 10, вторая правка код-ревью — см. докстроку
    модуля): круглый аватар-плашка или скруглённый бейдж под иконку тоже
    шагают той же сеткой, что карточки контента, но карточка обрамляет
    ТЕКСТ, а бейдж/аватар — картинку или иконку. Раскладка знает свои
    текстовые слоты (`Pattern.slots`) и их координаты — декор засчитывается
    карточным, только если внутри него лежит хотя бы один текстовый слот
    (см. `_contains_text`)."""
    counts: Counter[str] = Counter()
    adj_sums: dict[str, list[float]] = {}
    for pattern in patterns:
        text_slots = [slot for slot in pattern.slots if slot.role not in _NON_TEXT_ROLES]
        for decor in pattern.decor:
            if not decor.repeat_group or decor.prst is None:
                continue
            if not _contains_text(decor, text_slots):
                continue
            counts[decor.prst] += 1
            if decor.adj is not None:
                adj_sums.setdefault(decor.prst, []).append(decor.adj)
    return _entries_from_counts(counts, adj_sums)


def _contains_text(decor: "DecorShape", text_slots: "list[PatternSlot]") -> bool:
    """Лежит ли внутри `decor` хотя бы один текстовый слот раскладки —
    доля площади слота внутри декора не меньше `_TEXT_CONTAINMENT_
    THRESHOLD` (слот целиком внутри плашки с небольшим запасом на выступ за
    край, см. докстроку порога)."""
    for slot in text_slots:
        if slot.box.area <= 0:
            continue
        inter = decor.box.intersect(slot.box)
        if inter is None:
            continue
        if inter.area / slot.box.area >= _TEXT_CONTAINMENT_THRESHOLD:
            return True
    return False


def _slide_vocabulary(pkg: PptxPackage, canvas: Canvas) -> list[ShapeVocabEntry]:
    """Запасной путь (см. докстроку модуля) — перепись автофигур ТОЛЬКО по
    слайдам (`ppt/slides/slideN.xml`), не по макетам/мастерам."""
    counts: Counter[str] = Counter()
    adj_sums: dict[str, list[float]] = {}
    for part_name in _slide_parts(pkg):
        root = pkg.xml(part_name)
        for shape_ref in walk_shapes(root, canvas, include_groups=False):
            if shape_ref.kind != "shape":
                continue
            prst, adj = _prst_geom(shape_ref.element)
            if prst is None:
                continue
            counts[prst] += 1
            if adj is not None:
                adj_sums.setdefault(prst, []).append(adj)
    return _entries_from_counts(counts, adj_sums)


def _entries_from_counts(
    counts: Counter[str], adj_sums: dict[str, list[float]],
) -> list[ShapeVocabEntry]:
    entries = [
        ShapeVocabEntry(
            prst=prst, count=count,
            avg_adj=(sum(adj_sums[prst]) / len(adj_sums[prst])) if prst in adj_sums else 0.0,
        )
        for prst, count in counts.items()
    ]
    entries.sort(key=lambda e: -e.count)
    return entries


def _slide_parts(pkg: PptxPackage) -> list[str]:
    return sorted(
        name for name in pkg.names()
        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
    )


def _prst_geom(element) -> tuple[str | None, float | None]:
    sp_pr = element.find(qn("p:spPr"))
    if sp_pr is None:
        return None, None
    geom = sp_pr.find(qn("a:prstGeom"))
    if geom is None:
        return None, None
    prst = geom.get("prst")
    if not prst:
        return None, None

    adj: float | None = None
    av_lst = geom.find(qn("a:avLst"))
    if av_lst is not None:
        for gd in av_lst.findall(qn("a:gd")):
            if gd.get("name") != "adj":
                continue
            fmla = (gd.get("fmla") or "").split()
            if len(fmla) == 2 and fmla[0] == "val":
                try:
                    adj = int(fmla[1]) / 100000
                except ValueError:
                    adj = None
            break
    return prst, adj
