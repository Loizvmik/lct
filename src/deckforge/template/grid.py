"""Сетка шаблона: поля, колонные оси, вертикальные якоря, жёлоб — по
кластеризации фактической геометрии, не по декларации.

Направляющих (`p:guideLst`) нет ни в одном слайде/лейауте/мастере ни одного
из трёх учебных шаблонов (разведка, п.9) — сетку приходится восстанавливать
кластеризацией одномерных координат (`cluster()` ниже, общая для этого и для
`typography.py`, где тем же способом прореживается шкала кеглей).

Родные направляющие PowerPoint, когда они есть, живут не там, где можно было
бы ожидать по аналогии с геометрией шейпов: не в `p:cSld` слайда/лейаута/
мастера, а в `ppt/viewProps.xml` — `p:viewPr/p:slideViewPr/p:cSldViewPr/
p:guideLst/p:guide`. У VK_WorkSpace (единственного из трёх учебных, не
Google-, а, видимо, PowerPoint-экспорта — направляющие в Google Slides не
экспортируются вовсе) этот список непустой, как и у контрольного ЛЦТ2026.
Единицы `p:guide/@pos` — не EMU, как всюду в DrawingML-геометрии, а твипы
(1/1440 дюйма) — историческое наследие бинарного .ppt, сохранённое в разметке
view-свойств. Подтверждено сопоставлением с размером холста: и у WorkSpace, и
у ЛЦТ2026 есть направляющая `pos="2160"` — это 2160/1440 = 1.5″, что для
холста 13.333″×7.5″ (у обоих) даёт РОВНО 20% высоты — совпадение, которое
случайным быть не может.

Из направляющих используются только ВЕРТИКАЛЬНЫЕ (`orient="vert"`, это и
дефолт по схеме) — они однозначно колонная ось по смыслу. Горизонтальные
направляющие и margin/anchors направляющими намеренно не переопределяются:
без явного признака в разметке нельзя отличить направляющую-поле от
направляющей-колонки от произвольной линии дизайнера, а измеренное
кластеризацией поле VK_WorkSpace (3.51% по слайдам) не совпадает ни с одной
из его направляющих (13.3% и 20%) — переопределение полей направляющими
сломало бы именно этот, реально измеренный случай.

Устойчивого вертикального ритма (общей на весь шаблон baseline-сетки) в этих
файлах нет (разведка, п.14): `confidence["baseline"]` считается отдельно от
остальных чисел и на этих файлах честно выходит низкой (см. докстроку
`_baseline_confidence`) — так проверка на «блоки не выровнены по
направляющим» не примет шум за сетку.
"""
from __future__ import annotations
import statistics
from collections import Counter
from dataclasses import dataclass, field

from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import walk_shapes

# Допуск кластеризации координат — доля ширины холста. Брифом: "кластеризация
# с допуском 0.004 ширины". При холсте 13.333″ это ~0.053″ (~3.8pt) — меньше
# типичного шага верстки (доли/десятые дюйма), но больше плавающей ошибки
# округления EMU/аффинных преобразований групп.
CLUSTER_TOLERANCE = 0.004

# Поле — модальный (самый частый) кластер left-/right-координат, вес не ниже
# 5 попаданий (брифом, Step 4).
MIN_MARGIN_CLUSTER_HITS = 5

# Верхнее/нижнее поле требуют другого порога, не 5: по вертикали контент
# намного разнороднее, чем по горизонтали (заголовок/тело/футер сидят на
# разных уровнях, почти ничего не выравнивается по одной и той же строке),
# и "самый частый" top/bottom на этих файлах — случайная позиция обычного
# контента посередине слайда, а не поле (проверено разведкой: при пороге 5
# модальным становится единичный вылетающий к самому краю декоративный шейп).
# Порог 10 отсекает такие случайные кластеры и оставляет только
# действительно системно повторяющуюся позицию — см. build_grid.
MIN_VERTICAL_MARGIN_HITS = 10

# Колонная ось — кластер left-координат (за вычетом полей) с числом попаданий
# не ниже 4 (брифом, Step 4).
MIN_COLUMN_CLUSTER_HITS = 4

# Порог веса для кандидата в "частую" вертикальную координату при оценке
# базового ритма. Разведка (брифом, п.14) явно предупреждает: при слабом
# фильтре "шаг между частотными координатами по вертикали даёт шум, топ-
# кандидат встречается два-три раза" — подтверждено расчётом: порог 10
# оставляет только системно повторяющиеся Y-координаты (не случайный
# элемент одного слайда) и на VK Tech/WorkSpace честно даёт низкую
# уверенность (см. _baseline_confidence), в отличие от более низких порогов,
# которые превращают комбинаторный шум пар точек в ложно уверенный "ритм".
MIN_BASELINE_ANCHOR_HITS = 10

# Минимальный интервал между двумя координатами/кеглями, чтобы считать их
# РАЗНЫМИ ступенями/якорями, а не дублями одного и того же элемента с
# плавающим джиттером округления.
MIN_GAP_FRACTION = 0.005

# Верхняя граница правдоподобного жёлоба между колонками — эвристика:
# на трёх учебных шаблонах измеренные жёлобы не превышают ~6% ширины
# (разведка, п.13), берём вдвое больший запас, чтобы отсечь случаи, когда
# по ошибке в "жёлоб" попадает ширина самой колонки, а не зазор между ними.
MAX_GUTTER_FRACTION = 0.12

# Твипы (1/1440 дюйма) — единица p:guide/@pos, см. докстроку модуля.
TWIPS_PER_INCH = 1440

TITLE_PH_TYPES = frozenset({"title", "ctrTitle"})
BODY_PH_TYPES = frozenset({"body", "subTitle"})
FOOTER_PH_TYPES = frozenset({"ftr", "sldNum", "dt"})


@dataclass(frozen=True)
class Cluster:
    """Один кластер одномерных координат: `center` — среднее вошедших точек,
    `count` — сколько их было, `members` — сырые значения (для отладки/
    объяснимости результата человеку)."""
    center: float
    count: int
    members: tuple[float, ...]


def cluster(values: list[float], tolerance: float) -> list[Cluster]:
    """Кластеризация одномерных координат: одиночная связь (single-linkage)
    вдоль отсортированной прямой — значение присоединяется к текущему
    кластеру, если отстоит от его ПОСЛЕДНЕЙ (не первой и не средней) точки не
    дальше `tolerance`. Разрыв цепочки шире допуска начинает новый кластер.

    Общая для grid.py (поля/колонны/якоря, допуск в долях ширины холста) и
    typography.py (прореживание шкалы кеглей, допуск в pt) — оба места решают
    одну и ту же задачу: "точки ближе X друг к другу — одно и то же значение
    с шумом, не разные".
    """
    if not values:
        return []
    ordered = sorted(values)
    groups: list[list[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        if v - groups[-1][-1] <= tolerance:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [Cluster(center=sum(g) / len(g), count=len(g), members=tuple(g)) for g in groups]


@dataclass(frozen=True)
class Grid:
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    columns: list[float]
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
    # True, если колонные оси взяты из родных p:guide, а не из кластеризации
    # (см. докстроку модуля и _native_vertical_guides).
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

    margin_left, margin_left_conf = _modal_edge(lefts, MIN_MARGIN_CLUSTER_HITS)
    margin_right, margin_right_conf = _modal_edge(right_margins, MIN_MARGIN_CLUSTER_HITS)
    margin_top, margin_top_conf = _nearest_edge(tops, MIN_VERTICAL_MARGIN_HITS)
    margin_bottom, margin_bottom_conf = _nearest_edge(bottom_margins, MIN_VERTICAL_MARGIN_HITS)

    left_axes = [
        c for c in cluster(lefts, CLUSTER_TOLERANCE)
        if c.count >= MIN_COLUMN_CLUSTER_HITS and abs(c.center - margin_left) > CLUSTER_TOLERANCE
    ]
    right_axes = [
        c for c in cluster(rights, CLUSTER_TOLERANCE)
        if c.count >= MIN_COLUMN_CLUSTER_HITS and abs(c.center - (1 - margin_right)) > CLUSTER_TOLERANCE
    ]

    guide_columns = _native_vertical_guides(pkg, canvas)
    columns = sorted({round(c.center, 6) for c in left_axes} | set(guide_columns))
    columns_confidence = (
        1.0 if guide_columns
        else (sum(c.count for c in left_axes) / len(lefts) if left_axes and lefts else 0.0)
    )

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
            samples.append(_Sample(
                ref.box.left, ref.box.right, ref.box.top, ref.box.bottom,
                ref.ph_type if ref.is_placeholder else None, from_layout=False,
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


def _modal_edge(values: list[float], min_hits: int) -> tuple[float, float]:
    """Поле — самый частый (модальный) кластер, вес не ниже `min_hits`
    (брифом, Step 4). Confidence — доля всех замеров, попавших в этот
    кластер: поле по 200 совпадающим координатам и поле по 5 — разные по
    надёжности вещи (общее требование задачи), и это число их различает.
    """
    if not values:
        return 0.0, 0.0
    clusters = cluster(values, CLUSTER_TOLERANCE)
    eligible = [c for c in clusters if c.count >= min_hits]
    pool = eligible if eligible else clusters
    best = max(pool, key=lambda c: c.count)
    return best.center, best.count / len(values)


def _nearest_edge(values: list[float], min_hits: int) -> tuple[float, float]:
    """Вертикальное поле — не модальный, а БЛИЖАЙШИЙ К КРАЮ (наименьший)
    кластер среди тех, что набрали не меньше `min_hits` попаданий (см.
    докстроку MIN_VERTICAL_MARGIN_HITS о том, почему "самый частый" здесь не
    работает: по вертикали модальная координата — обычно контент
    посередине слайда, а не поле).
    """
    if not values:
        return 0.0, 0.0
    clusters = cluster(values, CLUSTER_TOLERANCE)
    eligible = [c for c in clusters if c.count >= min_hits]
    if not eligible:
        # Ни один кластер не набрал системного веса — сигнал слабый по сути
        # (не просто "порог чуть завышен"), берём модальный как честную
        # низкоуверенную оценку, а не отказываемся от числа вовсе.
        best = max(clusters, key=lambda c: c.count)
        return best.center, best.count / len(values)
    best = min(eligible, key=lambda c: c.center)
    return best.center, best.count / len(values)


def _estimate_gutter(left_axes: list[Cluster], right_axes: list[Cluster]) -> tuple[float, float]:
    """Жёлоб — медиана расстояния между правым краем левой колонки и левым
    краем правой (брифом, Step 4): для каждой левой оси ищем ближайшую
    ей предшествующую правую ось и считаем зазор, если он похож на жёлоб
    (не на ширину самой колонки — см. MAX_GUTTER_FRACTION).

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
        if MIN_GAP_FRACTION < gap < MAX_GUTTER_FRACTION:
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
        elif s.ph_type in FOOTER_PH_TYPES:
            buckets["footer"].append(s.top)

    anchors: dict[str, float] = {}
    confidence: dict[str, float] = {}
    for key, values in buckets.items():
        if not values:
            continue
        best = max(cluster(values, CLUSTER_TOLERANCE), key=lambda c: c.count)
        anchors[f"{key}_top"] = best.center
        confidence[f"anchor_{key}_top"] = best.count / len(values)
    return anchors, confidence


def _baseline_confidence(tops: list[float]) -> float:
    """Есть ли общий на весь шаблон вертикальный шаг (baseline grid)?

    Берём координаты top, встречающиеся системно (вес ≥ MIN_BASELINE_ANCHOR_
    HITS — см. её докстроку про комбинаторный шум при более низком пороге),
    считаем разности между СОСЕДНИМИ по возрастанию значениями и кластеризуем
    сами эти разности. Если шаблон верстался на общей сетке, один и тот же
    шаг должен повторяться в заметной доле промежутков; если нет —
    "топ-кандидат" набирает пару-тройку случайных совпадений из полутора-двух
    десятков (брифом, п.14 — именно так и есть на VK Tech). Confidence —
    доля промежутков, попавших в выигравший кластер разностей; ноль, если
    промежутков меньше двух (сравнивать не с чем).
    """
    frequent = sorted(c.center for c in cluster(tops, CLUSTER_TOLERANCE) if c.count >= MIN_BASELINE_ANCHOR_HITS)
    gaps = [b - a for a, b in zip(frequent, frequent[1:]) if b - a > MIN_GAP_FRACTION]
    if len(gaps) < 2:
        return 0.0
    gap_clusters = cluster(gaps, CLUSTER_TOLERANCE)
    top_gap = max(gap_clusters, key=lambda c: c.count)
    return top_gap.count / len(gaps)


def _native_vertical_guides(pkg: PptxPackage, canvas: Canvas) -> list[float]:
    """Вертикальные направляющие PowerPoint из ppt/viewProps.xml — см.
    докстрику модуля про единицы (твипы) и про то, почему используются
    только для columns, не для margin/anchors."""
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
        fraction = (int(pos_raw) / TWIPS_PER_INCH) / canvas.width_in
        if 0.0 <= fraction <= 1.0:
            result.append(fraction)
    return result
