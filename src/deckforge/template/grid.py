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

# Поле — модальный (самый частый) кластер left-/right-координат, вес не ниже
# 5 попаданий (брифом, Step 4).
_MIN_MARGIN_CLUSTER_HITS = 5

# Верхнее/нижнее поле требуют другого порога, не 5, И другого правила отбора
# (см. _nearest_edge, не _modal_edge): по вертикали контент намного
# разнороднее, чем по горизонтали (заголовок/тело/футер сидят на разных
# уровнях, почти ничего не выравнивается по одной и той же строке), и
# "самый частый" top/bottom на этих файлах — случайная позиция обычного
# контента посередине слайда, а не поле (проверено разведкой: при пороге 5
# модальным становится единичный вылетающий к самому краю декоративный
# шейп). Порог 10 отсекает такие случайные кластеры и оставляет только
# действительно системно повторяющуюся позицию.
_MIN_VERTICAL_MARGIN_HITS = 10

# Колонная ось — кластер left-координат (за вычетом полей) с числом попаданий
# не ниже 4 (брифом, Step 4).
_MIN_COLUMN_CLUSTER_HITS = 4

# Верхняя граница правдоподобного поля — четверть ширины/высоты холста, щедрый
# запас. Нужна, потому что чистая кластеризация с допуском 0.004 (без верхней
# границы позиции) на VK Tech даёт МОДАЛЬНЫМ не поле, а часто повторяющуюся
# координату где-то в середине слайда (карточка/колонка, у которой шейпов
# просто больше, чем у отступа) — 180 попаданий на left≈0.469 против 136 у
# реального отступа. Поле по определению около края, не где угодно с
# наибольшим числом совпадений — без этой границы _modal_edge выбрал бы
# содержательно неверную величину, пусть и с честным числом попаданий.
_MAX_MARGIN_CANDIDATE_FRACTION = 0.25

# Второй, более широкий допуск — специально для консолидации поля (не для
# columns/anchors/baseline, там строго _CLUSTER_TOLERANCE=0.004 брифом).
# На VK Tech отступ на практике не одно значение, а два близких по смыслу,
# но не по величине в пределах 0.004: у карточек и у текстовых блоков left
# отличается на ~0.024 (кластеры 0.0312 и 0.0554, оба веса 136) — оба
# "про одно и то же поле", просто разные типы контента вставлены с чуть
# разным отступом. Первый проход cluster() с допуском 0.004 (обязательным
# по брифу) их не объединяет; второй проход по ЦЕНТРАМ первого прохода (с
# повтором каждого центра по числу его попаданий — то есть с сохранением
# веса) укрупняет допуском 0.025 (> необходимого зазора 0.0243, с запасом) —
# и это даёт margin_left VK Tech ≈4.4% вместо ложных 3.1% или 5.5%
# по отдельности, что и совпадает с измеренным (по брифу) 4.63%.
_MARGIN_BAND_TOLERANCE = 0.025

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

# Верхняя граница правдоподобного жёлоба между колонками — эвристика:
# на трёх учебных шаблонах измеренные жёлобы не превышают ~6% ширины
# (разведка, п.13), берём вдвое больший запас, чтобы отсечь случаи, когда
# по ошибке в "жёлоб" попадает ширина самой колонки, а не зазор между ними.
_MAX_GUTTER_FRACTION = 0.12

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

    margin_left, margin_left_conf = _modal_edge(lefts, _MIN_MARGIN_CLUSTER_HITS)
    margin_right, margin_right_conf = _modal_edge(right_margins, _MIN_MARGIN_CLUSTER_HITS)
    margin_top, margin_top_conf = _nearest_edge(tops, _MIN_VERTICAL_MARGIN_HITS)
    margin_bottom, margin_bottom_conf = _nearest_edge(bottom_margins, _MIN_VERTICAL_MARGIN_HITS)

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


def _modal_edge(values: list[float], min_hits: int) -> tuple[float, float]:
    """Поле — самый частый (модальный) кластер среди кандидатов БЛИЗКО К
    КРАЮ (см. _MAX_MARGIN_CANDIDATE_FRACTION), вес не ниже `min_hits`
    (брифом, Step 4). Confidence — доля всех замеров, попавших в этот
    кластер: поле по 200 совпадающим координатам и поле по 5 — разные по
    надёжности вещи (общее требование задачи), и это число их различает.

    Двухпроходная кластеризация (см. _MARGIN_BAND_TOLERANCE): первый проход
    — строго допуском брифа (0.004), второй — переклассеризация центров
    первого прохода (с сохранением веса) более широким допуском, чтобы
    объединить несколько близких по смыслу, но не склеенных первым допуском
    отступов (разные типы контента с чуть разным inset) в одну содержательную
    величину поля.
    """
    if not values:
        return 0.0, 0.0
    near_edge = [c for c in cluster(values, _CLUSTER_TOLERANCE) if c.center <= _MAX_MARGIN_CANDIDATE_FRACTION]
    if not near_edge:
        return 0.0, 0.0

    expanded: list[float] = []
    for c in near_edge:
        expanded.extend([c.center] * c.count)
    bands = cluster(expanded, _MARGIN_BAND_TOLERANCE)

    eligible = [b for b in bands if b.count >= min_hits]
    pool = eligible if eligible else bands
    best = max(pool, key=lambda b: b.count)
    return best.center, best.count / len(values)


def _nearest_edge(values: list[float], min_hits: int) -> tuple[float, float]:
    """Вертикальное поле — не модальный, а БЛИЖАЙШИЙ К КРАЮ (наименьший)
    кластер среди тех, что набрали не меньше `min_hits` попаданий (см.
    докстроку _MIN_VERTICAL_MARGIN_HITS о том, почему "самый частый" здесь
    не работает: по вертикали модальная координата — обычно контент
    посередине слайда, а не поле). Это намеренное отклонение от правила
    брифа "поле = модальный кластер", распространённого им явно только на
    left/right (Step 4); для top/bottom модальный кластер систематически
    указывает не на поле, а на позицию контента — см. разведку в
    scratchpad/probe_topbottom*.py.
    """
    if not values:
        return 0.0, 0.0
    clusters = cluster(values, _CLUSTER_TOLERANCE)
    eligible = [c for c in clusters if c.count >= min_hits]
    if not eligible:
        # Ни один кластер не набрал системного веса — сигнал слабый по сути
        # (не просто "порог чуть завышен"), берём модальный как честную
        # низкоуверенную оценку, а не отказываемся от числа вовсе.
        best = max(clusters, key=lambda c: c.count)
        return best.center, best.count / len(values)
    best = min(eligible, key=lambda c: c.center)
    return best.center, best.count / len(values)


def _merge_columns(
    left_axes: list[Cluster], guide_columns: list[float], total_left_measurements: int,
) -> tuple[list[float], float]:
    """Объединяет колонные оси из двух источников — кластеризация
    left-координат и родные вертикальные направляющие — в один список.

    Точное сравнение "guide == cluster.center" после округления было бы
    хрупким (направляющая на 0.4999 и измеренная ось на 0.5005 остались бы
    двумя разными "колонками", хотя это одна и та же ось с шумом в pt/EMU
    округлении) — вместо этого обе популяции точек прогоняются через ту же
    cluster(), что и всё остальное в модуле, единым проходом.

    confidence по каждой итоговой оси: 1.0, если в неё попала хотя бы одна
    направляющая (направляющие надёжнее любой кластеризации — прямое
    указание дизайнера, не статистика), иначе — доля замеров, поддержавших
    исходные кластеры, вошедшие в эту ось. Итоговое число — среднее по всем
    осям (единственное поле confidence["columns"] в контракте Grid, не
    словарь по каждой оси).
    """
    combined = [c.center for c in left_axes] + list(guide_columns)
    merged = cluster(combined, _CLUSTER_TOLERANCE)
    if not merged:
        return [], 0.0

    axis_confidences: list[float] = []
    for m in merged:
        is_guide_backed = any(abs(g - m.center) <= _CLUSTER_TOLERANCE for g in guide_columns)
        if is_guide_backed:
            axis_confidences.append(1.0)
            continue
        support = sum(c.count for c in left_axes if abs(c.center - m.center) <= _CLUSTER_TOLERANCE)
        axis_confidences.append(support / total_left_measurements if total_left_measurements else 0.0)

    columns = sorted(m.center for m in merged)
    return columns, sum(axis_confidences) / len(axis_confidences)


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
