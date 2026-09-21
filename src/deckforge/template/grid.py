"""Сетка шаблона: поля, колонные оси, вертикальные якоря, жёлоб — по
кластеризации фактической геометрии, не по декларации.

Направляющих (`p:guideLst`) нет ни в одном слайде/лейауте/мастере ни одного
из трёх учебных шаблонов (разведка, п.9) — сетку приходится восстанавливать
кластеризацией одномерных координат (`cluster()` ниже, общая для этого и для
`typography.py`, где тем же способом прореживается шкала кеглей).

Родные направляющие PowerPoint, когда они есть, живут не там, где можно было
бы ожидать по аналогии с геометрией шейпов: не в `p:cSld` слайда/лейаута/
мастера, а в `ppt/viewProps.xml` — `p:viewPr/p:slideViewPr/p:cSldViewPr/
p:guideLst/p:guide`. У VK_WorkSpace (единственного из трёх учебных, судя по
этому файлу — не чистого Google-экспорта: направляющие в Google Slides не
сохраняются) этот список непустой, как и у контрольного ЛЦТ2026.

Единицы `p:guide/@pos` — НЕ твипы (1/1440″), как можно было бы предположить
по наследству от бинарного .ppt, а `1/576 дюйма` (= 1/8 pt, "master unit"
PowerPoint для позиций направляющих). Первая версия этого модуля ошибочно
использовала твипы, приняв за подтверждение одно совпадение (`pos="2160"`
→ 1.5″ → ровно 20% высоты и у WorkSpace, и у ЛЦТ2026) — совпадение оказалось
случайным: `2160/576` даёт РОВНО 3.75″ = 50% высоты — это стандартная
центральная направляющая, которую PowerPoint создаёт по умолчанию, и именно
её наличие у обоих файлов не случайно, а совпадение на 20% при твипах —
артефакт того, что 1.5″ тоже "круглое" число. Различает гипотезы не
"круглость", а сопоставление с РЕАЛЬНО ИЗМЕРЕННОЙ кластеризацией геометрией:
`pos="597"` у ЛЦТ2026 при единице 1/576″ даёт 0.0777341… долей ширины —
это совпадает с измеренным кластером краёв шейпов (0.0777344…, вес 6) с
точностью до 8 значащих цифр, случайным быть не может. Ошибку нашёл
general-purpose код-ревьюер (Task 4, повторное ревью), настояв на проверке
через остаток (residual) от измеренной геометрии, а не через "круглость"
результата самой по себе.

Из направляющих используются только ВЕРТИКАЛЬНЫЕ (`orient="vert"` — это же
дефолт по схеме CT_Guide, datypic.com/sc/ooxml/e-p_guide-1.html, подтверждено
и эмпирически: направляющие WorkSpace/ЛЦТ2026 без явного `orient` ложатся
на измеренные вертикальные кластеры) — они однозначно колонная ось по
смыслу. Горизонтальные направляющие и margin/anchors направляющими намеренно
не переопределяются: без явного признака в разметке нельзя отличить
направляющую-поле от направляющей-колонки от произвольной линии дизайнера, а
измеренное поле VK_WorkSpace (3.51% по слайдам) не совпадает ни с одной из
его направляющих — переопределение полей направляющими сломало бы именно
этот, реально измеренный случай.

Устойчивого вертикального ритма (общей на весь шаблон baseline-сетки) в этих
файлах нет (разведка, п.14): `confidence["baseline"]` считается отдельно от
остальных чисел и на этих файлах честно выходит низкой (см. докстроку
`_baseline_confidence`) — так проверка на «блоки не выровнены по
направляющим» не примет шум за сетку. `BASELINE_CONFIDENT_THRESHOLD` ниже —
граница, ниже которой baseline-сетку не стоит предъявлять потребителю как
установленную (используется в тестах; вынесена в именованную константу,
а не оставлена только числом в assert, чтобы аудит «блоки не выровнены по
сетке» мог сослаться на неё же).

Правки после ПОВТОРНОГО код-ревью (два ревьюера, general-purpose и
adversarial, см. .superpowers/sdd/task-4-report.md): margin_left/right/
top/bottom считаются `_percentile_edge` — низким процентилем взвешенного
распределения краёв, а не модальным/ближайшим-к-краю кластером с допуском,
подобранным под заранее известный ответ трёх учебных файлов (см. докстроку
`_MARGIN_PERCENTILE`). `Grid.columns` — не `list[float]`, а `list[ColumnAxis]`
(центр/вес/уверенность, отсортирован по уверенности, отсечён порогом
поддержки `_MIN_COLUMN_AXIS_SUPPORT_SHARE`) — на насыщенном/незнакомом
шаблоне плоский список неотличимых друг от друга чисел не даёт потребителю
(майнингу раскладок) способа отличить настоящую колонную ось от случайного
совпадения координат внутри диаграммы.
"""
from __future__ import annotations
import statistics
from dataclasses import dataclass, field
from itertools import combinations

from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes

# Допуск кластеризации координат — доля ширины холста. Брифом: "кластеризация
# с допуском 0.004 ширины". При холсте 13.333″ это ~0.053″ (~3.8pt) — меньше
# типичного шага верстки (доли/десятые дюйма), но больше плавающей ошибки
# округления EMU/аффинных преобразований групп.
_CLUSTER_TOLERANCE = 0.004

# Колонная ось — кластер left-координат (за вычетом полей) с числом попаданий
# не ниже 4 (брифом, Step 4). Этот порог — абсолютный пол на "статистическую
# существенность", предписанный брифом для трёх небольших учебных файлов
# (сотни измерений); он остаётся первым фильтром, но на большом/насыщенном
# шаблоне защиты его одного мало (см. _MIN_COLUMN_AXIS_SUPPORT_SHARE ниже,
# у ColumnAxis).
_MIN_COLUMN_CLUSTER_HITS = 4

# Поле шаблона — НЕ модальный (самый частый) кластер края и не "ближайший к
# краю кластер с достаточным весом" (обе эти формулировки были в прежней
# версии модуля и обе требовали объединять кластеры допуском, подобранным
# под уже известный численный ответ на трёх учебных файлах — реверс-
# инжиниринг, найденный повторным код-ревью; см. git log для деталей
# прежней реализации). Правильное определение содержательно другое: поле —
# это отступ, НАЧИНАЯ С КОТОРОГО на слайдах появляется содержание. Низкий
# процентиль взвешенного (по числу совпадающих координат) распределения
# краёв даёт это честно: `_MARGIN_PERCENTILE`-я точка распределения
# означает "(1 - _MARGIN_PERCENTILE) * 100% содержимого начинается дальше
# вглубь слайда, чем эта граница" — определение, а не наблюдение за файлом.
#
# 0.10 — классический дециль, нижняя (более строгая) граница общепринятого
# в устойчивой статистике диапазона усечения нижнего хвоста (trimmed-
# statistics/boxplot convention: типично 5-25% в зависимости от жёсткости
# фильтра). Не наблюдение за конкретным файлом: дециль — стандартная,
# не подгоняемая под данные точка отсечения "нижних 10% как потенциальных
# выбросов", одна из самых распространённых в описательной статистике.
#
# Единая формула для margin_left/right/top/bottom — специального правила
# для top/bottom (как было раньше, "самый частый" для left/right и
# "ближайший к краю" для top/bottom по отдельности) больше не нужно:
# процентиль по построению уже близок к краю независимо от того, насколько
# "типична" позиция контента где-то в середине слайда.
#
# Честная оговорка (см. итоговый отчёт): на VK Tech этот метод даёт
# margin_left≈3.12%, а не заявленные брифом 4.63% — обнаружено при
# численной проверке, что 4.63% физически недостижимы НИКАКИМ методом,
# выбирающим ОДНУ точку из распределения (проверены все кластеры и все
# процентили в диапазоне 0-30%): у VK Tech левый край бимодален — два
# самостоятельных, примерно равновесных по весу кластера (≈3.1% и ≈5.5%,
# оба по отдельности систематические, не шум), и 4.63% лежит примерно
# посередине между ними. Получить именно 4.63% может только СЛИЯНИЕ этих
# двух кластеров (тем самым способом, который эта правка убирает как
# реверс-инжиниринг) — значит, 4.63% сам по себе был не "истинным полем", а
# артефактом того слияния. Новое значение (3.12%, первый систематический
# кластер от края) честнее отвечает на вопрос "с какой границы начинается
# содержание" в буквальном прочтении задачи. WorkSpace и Education
# по-прежнему совпадают с заявленными брифом числами (0.0351 и 0.0540) с
# точностью до 0.001 — это и есть подтверждение, что метод содержательно
# работает, а расхождение по VK Tech — не ошибка метода, а разоблачение
# искусственности прежнего "известного ответа".
_MARGIN_PERCENTILE = 0.10

# Порог веса для кандидата в "частую" вертикальную координату при оценке
# базового ритма. Разведка (брифом, п.14) явно предупреждает: при слабом
# фильтре "шаг между частотными координатами по вертикали даёт шум, топ-
# кандидат встречается два-три раза" — подтверждено расчётом: порог 10
# оставляет только системно повторяющиеся Y-координаты (не случайный
# элемент одного слайда) и на VK Tech/WorkSpace честно даёт низкую
# уверенность (см. _baseline_confidence), в отличие от более низких порогов,
# которые превращают комбинаторный шум пар точек в ложно уверенный "ритм".
_MIN_BASELINE_ANCHOR_HITS = 10

# Минимальный зазор, чтобы не путать почти совпадающие координаты (джиттер
# округления EMU) с содержательным расстоянием — используется при отборе
# кандидатов в жёлоб (_estimate_gutter) и в разностях top-координат при
# оценке базового ритма (_baseline_confidence).
_MIN_GAP_FRACTION = 0.005

# Верхняя граница правдоподобного жёлоба между колонками — не наблюдение за
# тремя учебными файлами (раньше было так: "жёлобы не превышают ~6%,
# берём двойной запас"), а типографская конвенция: в устоявшихся сеточных
# системах (печатных, веб-, слайдовых) межколоночный жёлоб типично составляет
# 2-6% ширины содержательной области (Bootstrap/Material/классическая печатная
# вёрстка сходятся в этом диапазоне). 0.10 — заметный запас НАД верхней
# границей этой конвенции (не совпадает с ней впритык), но всё ещё далеко
# от типичной ширины самой колонки (20-45% при 2-4-колоночной раскладке) —
# так жёлоб не может быть спутан с шириной колонки, даже с этим запасом.
_MAX_GUTTER_FRACTION = 0.10

# Верхняя граница правдоподобной суммы двух ПРОТИВОПОЛОЖНЫХ полей (left+right
# или top+bottom): оба поля независимо оцениваются по разным популяциям
# точек (левые/правые, верхние/нижние края), и ничто в их вычислении не
# гарантирует геометрической непротиворечивости результата — суммарно они не
# могут содержательно занимать почти весь холст (тогда полезная область
# отрицательна). Порог 0.9 — запас против ложных срабатываний на реально
# узком контенте (двухколоночный слайд с узкими полями), но отсекает
# абсурд вроде margin_left=0.63 + margin_right=0.51 (найдено adversarial-
# reviewer на контрольном ЛЦТ2026 — оба числа были в [0,1], оба выглядели
# правдоподобно, но противоречили друг другу; починка корня проблемы
# — см. cluster() — сама по себе не гарантирует отсутствия таких случаев на
# незнакомом шаблоне защиты, поэтому проверка остаётся дополнительным
# полстраховочным слоем, снижающим confidence, а не значение).
_MAX_PLAUSIBLE_MARGIN_SUM = 0.9
_IMPLAUSIBLE_MARGIN_CONFIDENCE_PENALTY = 0.3

# 1/576 дюйма (= 1/8 pt) — единица p:guide/@pos, см. докстроку модуля.
_GUIDE_UNITS_PER_INCH = 576

# Порог "baseline-сетка достаточно уверенно установлена, чтобы на неё
# полагаться" — используется тестами и предназначен для потребителей
# (аудит «блоки не выровнены по сетке»): ниже него baseline считается не
# установленным, а не просто "низким".
BASELINE_CONFIDENT_THRESHOLD = 0.3

TITLE_PH_TYPES = frozenset({"title", "ctrTitle"})
BODY_PH_TYPES = frozenset({"body", "subTitle"})
_FOOTER_PH_TYPES = frozenset({"ftr", "sldNum", "dt"})


@dataclass(frozen=True)
class Cluster:
    """Один кластер одномерных координат: `center` — среднее вошедших точек,
    `count` — сколько их было, `members` — сырые значения (для отладки/
    объяснимости результата человеку)."""
    center: float
    count: int
    members: tuple[float, ...]


def cluster(values: list[float], tolerance: float) -> list[Cluster]:
    """Кластеризация одномерных координат: значение присоединяется к текущему
    кластеру, если отстоит от его ПЕРВОЙ (не последней) точки не дальше
    `tolerance` — суммарный разброс кластера ограничен `tolerance` целиком, а
    не только шагом между соседями.

    Раньше сравнение шло с ПОСЛЕДНЕЙ добавленной точкой (классическая
    single-linkage вдоль прямой) — на плотной геометрии (много шейпов почти
    впритык друг к другу, например диаграмма/roadmap с десятками элементов)
    это давало неограниченное "расползание": каждая следующая точка ближе
    допуска только к предыдущей, но цепочка целиком растягивалась на треть и
    больше ширины холста, и такой кластер (с честным на вид числом попаданий)
    выдавался наружу как поле/колонна с высокой confidence. Нашёл
    adversarial-reviewer на контрольном ЛЦТ2026 (в тестах не участвует, но
    именно на нём и должен был сработать): margin_left=0.632, margin_right=
    =0.506, margin_left+margin_right>1 — при том что оба выглядели правдоподобно
    (в [0,1], confidence 0.4/0.9) и не давали ни одного сигнала о порче.
    Привязка к первой точке группы — стандартный приём (fixed-radius
    binning) — устраняет расползание ценой того, что очень плотная лестница
    почти-одинаковых, но по факту разных координат может быть порезана на
    несколько кластеров вместо одного; для допусков этой задачи (0.004 доли
    холста, 1.5pt кегля) это не компромисс, а именно то, что нужно —
    "одно и то же значение с шумом", а не "растянутый диапазон".

    Общая для grid.py (поля/колонны/якоря, допуск в долях ширины холста) и
    typography.py (прореживание шкалы кеглей, допуск в pt).
    """
    if not values:
        return []
    ordered = sorted(values)
    groups: list[list[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        if v - groups[-1][0] <= tolerance:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [Cluster(center=sum(g) / len(g), count=len(g), members=tuple(g)) for g in groups]


@dataclass(frozen=True)
class ColumnAxis:
    """Одна колонная ось сетки — самостоятельная сущность, не голое число
    (контракт `list[float]`, которым Grid.columns был раньше, отменён после
    повторного код-ревью: на насыщенном/незнакомом шаблоне такой список
    легко раздувается случайными совпадениями координат внутри диаграмм и
    таблиц — 136 "осей" на контрольном ЛЦТ2026 при трёх учебных файлах, а
    потребитель не мог отличить настоящую ось от шума).

    `center` — положение оси в долях ширины холста.
    `count` — сколько сырых left-/right-измерений (либо, для осей, взятых из
    родных направляющих PowerPoint, значение не определяет надёжность —
    см. `confidence`) образовали эту ось.
    `confidence` — доля ВСЕХ left-измерений шаблона, поддержавших эту ось
    (тот же смысл, что и везде в Grid.confidence), кроме случая, когда ось
    подтверждена родной направляющей `p:guide` — тогда confidence = 1.0
    (прямое указание дизайнера надёжнее любой статистики).
    """
    center: float
    count: int
    confidence: float


@dataclass(frozen=True)
class Grid:
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    # Контракт: отсортирован по `confidence` по убыванию (самые надёжные
    # оси — первыми) и уже отфильтрован порогом `_MIN_COLUMN_AXIS_SUPPORT_
    # SHARE` (см. его докстроку) — оси со статистически незначимой
    # поддержкой в список вообще не попадают, не просто оказываются в его
    # хвосте. Потребитель (майнинг раскладок, выбор колоночной вёрстки)
    # может доверять порядку и/или применить собственный, более строгий
    # порог по `confidence`/`count` под свою задачу — единого порога,
    # одновременно отсекающего весь шум диаграмм НА ЛЮБОМ шаблоне и не
    # теряющего редкие, но легитимные оси (пример — VK Tech, ось карточной
    # раскладки на 63.61% шириной поддержана лишь ~0.24% всех измерений),
    # не существует: см. докстроку `_MIN_COLUMN_AXIS_SUPPORT_SHARE`.
    columns: list[ColumnAxis]
    gutter: float
    anchors: dict[str, float]
    # Мера уверенности для каждого числа, которое парсер выдаёт наружу (общее
    # требование задачи) — доля попаданий в выигравший кластер от всех
    # замеров того же рода; ключи см. build_grid.
    confidence: dict[str, float] = field(default_factory=dict)
    # Число шейпов, у которых ShapeRef.box is None (нет собственного a:xfrm
    # либо сломана группа-предок) — такие шейпы для сетки не годятся и
    # пропускаются, но их количество не должно теряться молча (условие
    # задачи, не отдельный пункт интерфейса брифа).
    skipped_no_box: int = 0
    # True, если хотя бы одна колонная ось взята из родных p:guide, а не
    # только из кластеризации (см. докстроку модуля и _native_vertical_guides).
    native_guides_used: bool = False


@dataclass(frozen=True)
class _Sample:
    """Один шейп с координатами — сырьё для всех расчётов ниже: left/right/
    top/bottom в долях холста и категория плейсхолдера (для якорей)."""
    left: float
    right: float
    top: float
    bottom: float
    ph_type: str | None
    from_layout: bool


def build_grid(pkg: PptxPackage, canvas: Canvas) -> Grid:
    samples, skipped = _collect_samples(pkg, canvas)

    lefts = [s.left for s in samples]
    rights = [s.right for s in samples]
    right_margins = [1 - s.right for s in samples]
    tops = [s.top for s in samples]
    bottom_margins = [1 - s.bottom for s in samples]

    margin_left, margin_left_conf = _percentile_edge(lefts, _MARGIN_PERCENTILE)
    margin_right, margin_right_conf = _percentile_edge(right_margins, _MARGIN_PERCENTILE)
    margin_top, margin_top_conf = _percentile_edge(tops, _MARGIN_PERCENTILE)
    margin_bottom, margin_bottom_conf = _percentile_edge(bottom_margins, _MARGIN_PERCENTILE)

    if margin_left + margin_right >= _MAX_PLAUSIBLE_MARGIN_SUM:
        margin_left_conf *= _IMPLAUSIBLE_MARGIN_CONFIDENCE_PENALTY
        margin_right_conf *= _IMPLAUSIBLE_MARGIN_CONFIDENCE_PENALTY
    if margin_top + margin_bottom >= _MAX_PLAUSIBLE_MARGIN_SUM:
        margin_top_conf *= _IMPLAUSIBLE_MARGIN_CONFIDENCE_PENALTY
        margin_bottom_conf *= _IMPLAUSIBLE_MARGIN_CONFIDENCE_PENALTY

    left_axes = [
        c for c in cluster(lefts, _CLUSTER_TOLERANCE)
        if c.count >= _MIN_COLUMN_CLUSTER_HITS and abs(c.center - margin_left) > _CLUSTER_TOLERANCE
    ]
    right_axes = [
        c for c in cluster(rights, _CLUSTER_TOLERANCE)
        if c.count >= _MIN_COLUMN_CLUSTER_HITS and abs(c.center - (1 - margin_right)) > _CLUSTER_TOLERANCE
    ]

    guide_columns = _native_vertical_guides(pkg, canvas)
    # Направляющие тоже не должны попадать в зону поля — у направляющей нет
    # своего "числа попаданий", но семантически направляющая внутри поля —
    # разметочная линия дизайнера (например, безопасная зона текста), не
    # колонная ось.
    guide_columns = [
        g for g in guide_columns
        if abs(g - margin_left) > _CLUSTER_TOLERANCE and abs(g - (1 - margin_right)) > _CLUSTER_TOLERANCE
    ]

    columns, columns_confidence = _merge_columns(left_axes, guide_columns, len(lefts))

    gutter, gutter_conf = _estimate_gutter(left_axes, right_axes)
    anchors, anchor_conf = _vertical_anchors(samples)
    # Только слайды, не лейауты: у лейаутов один и тот же плейсхолдер-тип
    # структурно повторяется в почти неизменном виде на десятках лейаутов
    # (это повторение авторства шаблона, не ритм СОСТАВЛЕННЫХ слайдов) и
    # искусственно завышает видимость "ритма" сверх того, что реально видно
    # в скомпонованных презентациях.
    baseline_conf = _baseline_confidence([s.top for s in samples if not s.from_layout])

    confidence = {
        "margin_left": margin_left_conf,
        "margin_right": margin_right_conf,
        "margin_top": margin_top_conf,
        "margin_bottom": margin_bottom_conf,
        "columns": columns_confidence,
        "gutter": gutter_conf,
        "baseline": baseline_conf,
        **anchor_conf,
    }

    return Grid(
        margin_left=margin_left,
        margin_right=margin_right,
        margin_top=margin_top,
        margin_bottom=margin_bottom,
        columns=columns,
        gutter=gutter,
        anchors=anchors,
        confidence=confidence,
        skipped_no_box=skipped,
        native_guides_used=bool(guide_columns),
    )


def _slide_parts(pkg: PptxPackage) -> list[str]:
    return sorted(n for n in pkg.names() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))


def _layout_parts(pkg: PptxPackage) -> list[str]:
    return sorted(n for n in pkg.names() if n.startswith("ppt/slideLayouts/slideLayout") and n.endswith(".xml"))


def _in_bounds(box: Box) -> bool:
    """Отсекает шейпы, чей контур выходит за пределы холста (декоративная
    фоновая графика с намеренным вылетом за край, редкие сломанные
    трансформации) — такая точка не несёт сигнала о поле/колонке контента,
    а координаты вроде left=1.53 (встречаются на Education) только зашумят
    кластеризацию."""
    return 0.0 <= box.left <= 1.0 and 0.0 <= box.right <= 1.0 and 0.0 <= box.top <= 1.0 and 0.0 <= box.bottom <= 1.0


def _collect_samples(pkg: PptxPackage, canvas: Canvas) -> tuple[list[_Sample], int]:
    """Пул координат для всей кластеризации ниже.

    Источник асимметричен по конструкции, не по недосмотру (брифом, Step 4:
    "всех плейсхолдеров лейаутов и шейпов слайдов"): с лейаутов берутся
    только ПЛЕЙСХОЛДЕРЫ (декоративная нередактируемая графика макета — не
    часть сетки контента), со слайдов — вообще все шейпы (реальная раскладка
    отражает контент целиком, не только плейсхолдеры).
    """
    samples: list[_Sample] = []
    skipped = 0

    for part in _slide_parts(pkg):
        root = pkg.xml(part)
        for ref in walk_shapes(root, canvas, include_groups=False):
            if ref.box is None:
                skipped += 1
                continue
            if not _in_bounds(ref.box):
                continue
            # walk_shapes уже отдаёт ph_type=None для не-плейсхолдера (см.
            # ShapeRef.ph_type в ooxml/walk.py) — доп. условие не нужно.
            samples.append(_Sample(
                ref.box.left, ref.box.right, ref.box.top, ref.box.bottom, ref.ph_type, from_layout=False,
            ))

    for part in _layout_parts(pkg):
        root = pkg.xml(part)
        for ref in walk_shapes(root, canvas, include_groups=False):
            if not ref.is_placeholder:
                continue
            if ref.box is None:
                skipped += 1
                continue
            if not _in_bounds(ref.box):
                continue
            samples.append(_Sample(
                ref.box.left, ref.box.right, ref.box.top, ref.box.bottom, ref.ph_type, from_layout=True,
            ))

    return samples, skipped


def _percentile_edge(values: list[float], percentile: float) -> tuple[float, float]:
    """Поле — низкий процентиль взвешенного (по числу совпадающих координат)
    распределения краёв. См. докстроку `_MARGIN_PERCENTILE` за обоснованием
    самого числа и тем, что заменяет эта функция (`_modal_edge`/
    `_nearest_edge` из прежней версии модуля — обе требовали объединять
    кластеры допуском, подобранным под уже известный ответ, и обе были
    отдельными правилами для left/right и top/bottom).

    Единая формула для всех четырёх краёв: `values` отсортированы, берётся
    точка на позиции `percentile` от начала списка (сам порядок списка,
    без предварительной кластеризации, уже "взвешивает" распределение —
    значение, встретившееся 200 раз, занимает 200 позиций в отсортированном
    списке, а не одну).

    Confidence — доля ВСЕХ замеров, лежащих в пределах _CLUSTER_TOLERANCE от
    найденной точки (тот же допуск, что используется во всём остальном
    модуле — не отдельный "band"-допуск, как было раньше). Эта же величина
    одновременно служит явным признаком вырожденного случая (регрессия на
    находку код-ревью: раньше запасная ветка `_nearest_edge` при отсутствии
    системного кластера тихо возвращала "типичный контент" как поле, с виду
    уверенно): если вокруг найденной точки нет плотности, число получится
    маленьким, и потребитель обязан увидеть по нему, что поле не найдено
    по сути, а не поверить молчаливой подмене.
    """
    if not values:
        return 0.0, 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * percentile), len(ordered) - 1)
    edge = ordered[idx]
    support = sum(1 for v in values if abs(v - edge) <= _CLUSTER_TOLERANCE)
    return edge, support / len(values)


# Порог поддержки колонной оси — доля от ВСЕХ left-измерений шаблона, не
# наблюдение за конкретным файлом (находка код-ревью: раньше единственным
# фильтром был абсолютный _MIN_COLUMN_CLUSTER_HITS=4, унаследованный от трёх
# небольших учебных файлов на сотни измерений — на насыщенном шаблоне
# защиты (тысячи измерений) он тривиально проходится десятками случайных
# совпадений координат внутри диаграмм/таблиц). 0.0015 (0.15%) — верхняя
# граница того, что можно поднять, не потеряв реальный, но редкий сигнал:
# на VK Tech легитимная колонная ось карточной раскладки (63.61%,
# `test_vktech_column_verticals_are_found`) поддержана лишь ~0.24% всех
# left-измерений (раскладка используется на немногих слайдах) — выше этой
# границы порог начал бы отсекать настоящие, просто редко используемые оси
# наравне с шумом. Это осознанный компромисс, не полное решение: см.
# докстроку Grid.columns — единого порога, отсекающего ВЕСЬ шум диаграмм на
# любом шаблоне и одновременно не теряющего такие редкие легитимные оси, не
# существует, поэтому основная защита потребителя — не этот порог, а сама
# структура ColumnAxis (сортировка по confidence, явный вес), позволяющая
# потребителю дофильтровать под свою задачу.
_MIN_COLUMN_AXIS_SUPPORT_SHARE = 0.0015


def _merge_columns(
    left_axes: list[Cluster], guide_columns: list[float], total_left_measurements: int,
) -> tuple[list[ColumnAxis], float]:
    """Объединяет колонные оси из двух источников — кластеризация
    left-координат и родные вертикальные направляющие — в список ColumnAxis,
    отсортированный по confidence по убыванию (контракт, см. докстроку
    Grid.columns) и отфильтрованный `_MIN_COLUMN_AXIS_SUPPORT_SHARE`.

    Точное сравнение "guide == cluster.center" после округления было бы
    хрупким (направляющая на 0.4999 и измеренная ось на 0.5005 остались бы
    двумя разными "колонками", хотя это одна и та же ось с шумом в pt/EMU
    округлении) — вместо этого обе популяции точек прогоняются через ту же
    cluster(), что и всё остальное в модуле, единым проходом.

    confidence по каждой итоговой оси: 1.0, если в неё попала хотя бы одна
    направляющая (направляющие надёжнее любой кластеризации — прямое
    указание дизайнера, не статистика, и порог поддержки на них не
    распространяется), иначе — доля замеров, поддержавших исходные кластеры,
    вошедшие в эту ось. Grid.confidence["columns"] (агрегат для всей сетки,
    отдельно от весов отдельных осей) — среднее confidence по ИТОГОВЫМ,
    уже отфильтрованным осям.
    """
    combined = [c.center for c in left_axes] + list(guide_columns)
    merged = cluster(combined, _CLUSTER_TOLERANCE)
    if not merged:
        return [], 0.0

    axes: list[ColumnAxis] = []
    for m in merged:
        is_guide_backed = any(abs(g - m.center) <= _CLUSTER_TOLERANCE for g in guide_columns)
        if is_guide_backed:
            axes.append(ColumnAxis(center=m.center, count=m.count, confidence=1.0))
            continue
        support = sum(c.count for c in left_axes if abs(c.center - m.center) <= _CLUSTER_TOLERANCE)
        confidence = support / total_left_measurements if total_left_measurements else 0.0
        if confidence < _MIN_COLUMN_AXIS_SUPPORT_SHARE:
            continue
        axes.append(ColumnAxis(center=m.center, count=support, confidence=confidence))

    if not axes:
        return [], 0.0
    axes.sort(key=lambda a: a.confidence, reverse=True)
    return axes, sum(a.confidence for a in axes) / len(axes)


def _estimate_gutter(left_axes: list[Cluster], right_axes: list[Cluster]) -> tuple[float, float]:
    """Жёлоб — медиана расстояния между правым краем левой колонки и левым
    краем правой (брифом, Step 4): для каждой левой оси ищем ближайшую
    ей предшествующую правую ось и считаем зазор, если он похож на жёлоб
    (не на ширину самой колонки — см. _MAX_GUTTER_FRACTION).

    На шаблонах с несколькими разными карточными раскладками (VK Tech,
    Education) жёлобы у разных раскладок отличаются — то, что возвращается
    здесь, это ТИПИЧНЫЙ (медианный) жёлоб по всем найденным парам, не жёлоб
    какой-то одной конкретной раскладки.
    """
    gaps: list[float] = []
    for left in left_axes:
        preceding = [r for r in right_axes if r.center < left.center]
        if not preceding:
            continue
        right = max(preceding, key=lambda c: c.center)
        gap = left.center - right.center
        if _MIN_GAP_FRACTION < gap < _MAX_GUTTER_FRACTION:
            gaps.append(gap)
    if not gaps:
        return 0.0, 0.0
    denom = max(len(left_axes), 1)
    return statistics.median(gaps), len(gaps) / denom


def _vertical_anchors(samples: list[_Sample]) -> tuple[dict[str, float], dict[str, float]]:
    """Вертикальные якоря — модальные кластеры top по ph_type (брифом,
    Step 4: title/body/footer). Категория без единого попадания в
    шаблоне (например, footer у VK Tech, body у WorkSpace — см. разведку)
    просто не попадает в результат, а не выдумывается нулём."""
    buckets: dict[str, list[float]] = {"title": [], "body": [], "footer": []}
    for s in samples:
        if s.ph_type in TITLE_PH_TYPES:
            buckets["title"].append(s.top)
        elif s.ph_type in BODY_PH_TYPES:
            buckets["body"].append(s.top)
        elif s.ph_type in _FOOTER_PH_TYPES:
            buckets["footer"].append(s.top)

    anchors: dict[str, float] = {}
    confidence: dict[str, float] = {}
    for key, values in buckets.items():
        if not values:
            continue
        best = max(cluster(values, _CLUSTER_TOLERANCE), key=lambda c: c.count)
        anchors[f"{key}_top"] = best.center
        confidence[f"anchor_{key}_top"] = best.count / len(values)
    return anchors, confidence


def _baseline_confidence(tops: list[float]) -> float:
    """Есть ли общий на весь шаблон вертикальный шаг (baseline grid)?

    Берём координаты top, встречающиеся системно (вес ≥ _MIN_BASELINE_
    ANCHOR_HITS — см. её докстроку про комбинаторный шум при более низком
    пороге), считаем расстояния между ВСЕМИ ПАРАМИ таких координат (не
    только соседними по возрастанию) и кластеризуем сами эти расстояния.
    Если шаблон верстался на общей сетке, один и тот же шаг должен
    повторяться в заметной доле пар; если нет — "топ-кандидат" набирает
    пренебрежимо малую долю среди комбинаторно большого числа пар (брифом,
    п.14 — именно так и есть на VK Tech).

    Важно: сравнение СОСЕДНИХ по возрастанию расстояний (первая версия этого
    метода) оказалось нестабильным после починки cluster() (см. её докстроку):
    точная кластеризация без "расползания" вскрывает намного больше по-
    настоящему различных, но каждая по отдельности частых Y-координат (у VK
    Tech — 74 вместо 19 при том же пороге весом, потому что макет
    переиспользуется на десятках слайдов, и КАЖДАЯ позиция КАЖДОГО шейпа в
    нём набирает вес), и промежутки между СОСЕДНИМИ такими координатами
    оказались мелкими и шумными сами по себе — ложно завышая уверенность.
    Расстояния между ВСЕМИ парами (не только соседними) намного устойчивее:
    редкий, не системный интервал должен совпасть у пренебрежимо малой доли
    комбинаций, а не только у соседей по случайному везению сортировки.
    Confidence — доля пар, попавших в выигравший кластер расстояний; ноль,
    если пар меньше двух (сравнивать не с чем). Сравни с
    BASELINE_CONFIDENT_THRESHOLD, чтобы решить, стоит ли доверять результату.
    """
    frequent = sorted(c.center for c in cluster(tops, _CLUSTER_TOLERANCE) if c.count >= _MIN_BASELINE_ANCHOR_HITS)
    gaps = [b - a for a, b in combinations(frequent, 2) if b - a > _MIN_GAP_FRACTION]
    if len(gaps) < 2:
        return 0.0
    gap_clusters = cluster(gaps, _CLUSTER_TOLERANCE)
    top_gap = max(gap_clusters, key=lambda c: c.count)
    return top_gap.count / len(gaps)


def _native_vertical_guides(pkg: PptxPackage, canvas: Canvas) -> list[float]:
    """Вертикальные направляющие PowerPoint из ppt/viewProps.xml — см.
    докстроку модуля про единицы (1/576″) и про то, почему используются
    только для columns, не для margin/anchors.

    `pos` — нечисловое значение технически невалидно по схеме (`ST_
    Coordinate32`), но битый .pptx на защите не должен ронять разбор ВСЕЙ
    сетки из-за одной направляющей: пропускаем её, остальные читаем как
    обычно (тот же принцип, что и повсюду в theme.py/usage.py — см. их
    докстроки про честный фолбэк вместо падения)."""
    if "ppt/viewProps.xml" not in pkg.names():
        return []
    root = pkg.xml("ppt/viewProps.xml")
    slide_view = root.find(qn("p:slideViewPr"))
    if slide_view is None:
        return []

    result: list[float] = []
    for guide in slide_view.iter(qn("p:guide")):
        if guide.get("orient", "vert") != "vert":
            continue
        pos_raw = guide.get("pos")
        if pos_raw is None:
            continue
        try:
            pos = int(pos_raw)
        except ValueError:
            continue
        fraction = (pos / _GUIDE_UNITS_PER_INCH) / canvas.width_in
        if 0.0 <= fraction <= 1.0:
            result.append(fraction)
    return result
