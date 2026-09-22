"""Сетка шаблона (поля/колонки/якоря) — тесты дословно из брифа Task 4
(Step 3), .superpowers/sdd/task-4-brief.md.

Как и в test_typography.py, `profile_fixture` в телах тестов брифа нужен как
параметр — иначе pytest не подставит фикстуру и вызов упадёт с NameError;
это единственная правка против буквального текста брифа.

Правки после повторного код-ревью (реверс-инжиниринг порогов под три файла,
см. git log): `grid.columns` — больше не `list[float]`, а `list[ColumnAxis]`
(центр/вес/уверенность, отсортирован по уверенности) — тесты ниже читают
`axis.center`, а не `axis` напрямую.
"""
import io
import zipfile

from conftest import ALL_TEMPLATES

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.template.grid import _CLUSTER_TOLERANCE, _MARGIN_PERCENTILE, _percentile_edge, build_grid, cluster


def test_margins_match_measured_values(profile_fixture):
    """Числа замерены разведкой; допуск 0.5 п.п. Метод вычисления с тех пор
    сменился (процентиль распределения вместо модального кластера с
    допуском, подобранным под эти же числа, — см. историю и докстроку
    _MARGIN_PERCENTILE в grid.py) — WorkSpace и Education по-прежнему
    совпадают с брифом.

    VK Tech — исключение, и не по ошибке метода: численно проверено (см.
    докстроку _MARGIN_PERCENTILE), что заявленные брифом 4.63% недостижимы
    НИКАКИМ методом, выбирающим одну точку распределения — у VK Tech левый
    край бимодален (два самостоятельных кластера ≈3.1% и ≈5.5%, оба
    системные), а 4.63% лежит примерно между ними и получается только
    слиянием этих кластеров вручную подобранным допуском — то есть тем самым
    реверс-инжинирингом, который эта правка убирает. Новое значение (≈3.12%,
    первый систематический кластер от края) — честный ответ на вопрос "с
    какой границы начинается содержание", не подогнанный под старое число."""
    cases = [("VK Tech шаблон.pptx", 0.0312), ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", 0.0351),
             ("Шаблон презентации VK Education.pptx", 0.0540)]
    for name, expected in cases:
        grid = profile_fixture(name).grid
        assert abs(grid.margin_left - expected) < 0.005, name


def test_education_margins_are_symmetric(profile_fixture):
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert abs(grid.margin_left - grid.margin_right) < 0.002


def test_education_has_exact_half_split(profile_fixture):
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert any(abs(axis.center - 0.5) < 0.003 for axis in grid.columns)


def test_columns_are_sorted_by_confidence_descending(profile_fixture):
    """Контракт ColumnAxis (см. докстроку grid.py): потребитель (майнинг
    раскладок) должен получить самые надёжные оси первыми, не рыться в
    произвольном порядке кластеризации."""
    for name in ALL_TEMPLATES:
        grid = profile_fixture(name).grid
        confidences = [axis.confidence for axis in grid.columns]
        assert confidences == sorted(confidences, reverse=True)


def test_columns_below_support_threshold_are_not_returned(profile_fixture):
    """Порог отсечения — доля от всех измерений (см. _MIN_COLUMN_AXIS_
    SUPPORT_SHARE), не наблюдение за файлом: проверяем, что он действительно
    применяется, а не только описан в докстроке. Направляющие (`source ==
    "guide"`) освобождены от этого порога по построению (авторитетны как
    ИСТОЧНИК независимо от статистики, см. докстроку ColumnAxis) — честная
    (Task 11 повторное ревью, находка №1) `confidence` направляющей может
    оказаться и ниже порога, это не повод её терять."""
    from deckforge.template.grid import _MIN_COLUMN_AXIS_SUPPORT_SHARE
    for name in ALL_TEMPLATES:
        grid = profile_fixture(name).grid
        for axis in grid.columns:
            assert axis.confidence >= _MIN_COLUMN_AXIS_SUPPORT_SHARE or axis.source == "guide"


def test_guide_backed_axis_keeps_honest_confidence_not_forced_to_one():
    """Task 11 повторное ревью, находка №1 — раньше направляющая получала
    `confidence=1.0` НЕЗАВИСИМО от реальной поддержки, и такая ось обгоняла
    по 'уверенности' кластерную ось, реально поддержанную на порядки большим
    числом измерений (ровно так L05 ложно срабатывал на настоящих колонках
    сетки — см. `tests/audit/test_deterministic.py::
    test_L05_does_not_flag_a_block_aligned_to_a_well_supported_cluster_axis`).
    Направляющая без единого подтверждающего её кластера (`guide_columns`
    не встретила ни одного близкого `left_axes`) — легитимный случай
    (дизайнер провёл линию, но по ней почти никто не выровнен), и её
    `confidence` обязана честно отражать это (низкая или нулевая), а не
    прикидываться единицей."""
    from deckforge.template.grid import Cluster, _MIN_COLUMN_AXIS_SUPPORT_SHARE, _merge_columns

    well_supported = Cluster(center=0.42, count=470, members=())
    axes, _ = _merge_columns([well_supported], guide_columns=[0.10], total_left_measurements=10_000)

    guide_axis = next(a for a in axes if a.source == "guide")
    cluster_axis = next(a for a in axes if a.source == "cluster")

    assert guide_axis.center == 0.10
    assert guide_axis.confidence == 0.0  # ни один кластер её не поддержал — честный ноль, не 1.0
    assert cluster_axis.confidence == 470 / 10_000
    # Сортировка по честной confidence — настоящая массовая ось теперь первая,
    # а не направляющая просто по факту происхождения.
    assert axes[0] is cluster_axis
    assert cluster_axis.confidence > _MIN_COLUMN_AXIS_SUPPORT_SHARE > guide_axis.confidence


def test_title_anchor_is_found(profile_fixture):
    for name, expected in [("VK Tech шаблон.pptx", 0.055),
                           ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", 0.0619),
                           ("Шаблон презентации VK Education.pptx", 0.1009)]:
        assert abs(profile_fixture(name).grid.anchors["title_top"] - expected) < 0.008, name


def test_vertical_rhythm_is_reported_as_low_confidence(profile_fixture):
    """Глобального baseline grid в этих файлах нет. Парсер обязан сказать об этом,
    а не выдать шум за сетку."""
    grid = profile_fixture("VK Tech шаблон.pptx").grid
    assert grid.confidence["baseline"] < 0.3


# --- дополнительные тесты на утверждения, явно сформулированные текстом
# брифа (разведка, п.9-15), которых бриф не дал в виде готового кода ---

def test_no_guide_list_in_google_export_templates(profile_fixture):
    """p:guideLst отсутствует в обоих чистых Google-экспортах среди трёх
    учебных шаблонов (брифом, п.9; VK_WorkSpace — исключение, у него
    guideLst непустой, см. докстроку grid.py) — сетка обязана быть
    восстановлена кластеризацией, не направляющими."""
    for name in ("VK Tech шаблон.pptx", "Шаблон презентации VK Education.pptx"):
        assert profile_fixture(name).grid.native_guides_used is False


def test_vktech_column_verticals_are_found(profile_fixture):
    """VK Tech: карточные раскладки в 3-4 колонки дают вертикали на 63.61%
    и 81.56% (брифом, п.13). Ось на 63.61% редка (поддержка ~0.24% всех
    left-измерений — карточная раскладка встречается на немногих слайдах),
    но легитимна: порог поддержки не должен её отсекать."""
    grid = profile_fixture("VK Tech шаблон.pptx").grid
    centers = [axis.center for axis in grid.columns]
    assert any(abs(c - 0.6361) < 0.006 for c in centers)
    assert any(abs(c - 0.8156) < 0.006 for c in centers)


def test_no_column_axis_coincides_with_margin(profile_fixture):
    """Регрессия на найденное рассогласование допусков (код-ревью): раньше
    margin_left консолидировался широким допуском (0.025), а из списка
    колонных осей (построенного из тех же left-координат) исключался узким
    (0.004) — кластер мог остаться и полем, и осью одновременно. После
    починки margin_left и исключение колонных осей у поля используют один и
    тот же допуск (_CLUSTER_TOLERANCE) и одно и то же итоговое значение.

    Проверяем только margin_left: colonns строятся из LEFT-координат, у
    margin_right (RIGHT-координаты) нет того же контура вычисления и
    сравнивать оси по left с полем по right — сравнение из разных
    популяций точек, не регрессия на найденную находку."""
    for name in ALL_TEMPLATES:
        grid = profile_fixture(name).grid
        for axis in grid.columns:
            assert abs(axis.center - grid.margin_left) > _CLUSTER_TOLERANCE


def test_education_body_anchor_matches_measured_value(profile_fixture):
    """Верх основного текста у Education — 25.86% (брифом, п.15)."""
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert abs(grid.anchors["body_top"] - 0.2586) < 0.008


def _package_from_slide_xml(slide_xml: str) -> PptxPackage:
    """Синтетический пакет из одного slide1.xml — тот же приём, что и в
    tests/template/test_usage.py (_usage_from_slide_xml), для граничных
    случаев OOXML, которых нет ни в одном из трёх реальных шаблонов."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>',
        )
        zf.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r"))


_SLIDE_WITHOUT_XFRM = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="NoXfrm"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr/>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_skipped_shapes_without_box_are_counted():
    """Шейп без a:xfrm (box=None) для сетки не годится, но должен быть
    учтён, а не потеряться молча — на трёх реальных шаблонах такого шейпа
    нет вовсе (skipped_no_box==0 у всех), нужна синтетика с известным ответом."""
    pkg = _package_from_slide_xml(_SLIDE_WITHOUT_XFRM)
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    grid = build_grid(pkg, canvas)
    assert grid.skipped_no_box == 1


_GUIDE_WITH_MALFORMED_POS = """<?xml version="1.0" encoding="UTF-8"?>
<p:viewPr xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:slideViewPr><p:cSldViewPr><p:guideLst>
    <p:guide pos="not-a-number"/>
    <p:guide pos="3591"/>
  </p:guideLst></p:cSldViewPr></p:slideViewPr>
</p:viewPr>
"""


def test_malformed_guide_pos_is_skipped_not_fatal():
    """`p:guide/@pos` нечисловым не должен ронять разбор ВСЕЙ сетки —
    невалидная направляющая пропускается, остальные читаются как обычно
    (adversarial-reviewer, Task 4 повторное ревью)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>',
        )
        zf.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )
        zf.writestr("ppt/viewProps.xml", _GUIDE_WITH_MALFORMED_POS)
    buf.seek(0)
    pkg = PptxPackage(zipfile.ZipFile(buf, "r"))
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    grid = build_grid(pkg, canvas)  # не должен бросить ValueError
    assert grid.native_guides_used is True
    assert len(grid.columns) == 1


def test_cluster_span_is_bounded_by_tolerance():
    """Регрессия на CRITICAL-находку adversarial-reviewer (Task 4 повторное
    ревью): раньше cluster() сравнивал каждую точку с ПОСЛЕДНЕЙ добавленной
    (классический single-linkage), из-за чего плотная цепочка точек
    (каждая ближе допуска только к соседней) могла "расползтись" на
    произвольно большой суммарный диапазон — на контрольном ЛЦТ2026 (не в
    тестах) это давало margin_left+margin_right>1, физически невозможную
    геометрию, с виду достоверную (confidence 0.4/0.9). Синтетика ниже не
    зависит от реального файла: 1000 точек с шагом чуть меньше допуска дают
    суммарный диапазон ~1.0 при допуске 0.01 — старая реализация схлопнула
    бы их в один кластер шириной ~1.0; новая обязана порезать на кластеры
    шириной не больше tolerance."""
    tol = 0.01
    step = tol * 0.9
    values = [i * step for i in range(1000)]
    clusters = cluster(values, tol)
    for c in clusters:
        assert max(c.members) - min(c.members) <= tol + 1e-9, c


def test_percentile_edge_finds_synthetic_margin_over_decorative_noise():
    """Синтетика с известным правильным ответом, независимая от трёх учебных
    файлов (главная находка повторного ревью: раньше порог вычислялся из
    заранее известного ответа на VK Tech, и единственный тест сверял с теми
    же числами, из которых он выведен).

    90 точек контента стартуют на 0.30 (основной текст — систематическая,
    самая массовая позиция), 10 точек — декоративный слой (иконки/бейджи),
    случайно разбросанный ближе к краю (0.05-0.12), не образующий
    собственного плотного кластера. Правильный ответ — поле на 0.30, не
    декоративный шум у края: алгоритм обязан пройти мимо разреженного
    декора и найти границу, где начинается систематическая масса контента."""
    decorative = [0.05, 0.12, 0.07, 0.10, 0.06, 0.11, 0.08, 0.09, 0.05, 0.12]
    # Шаг заметно меньше _CLUSTER_TOLERANCE (0.004) — весь диапазон 90 точек
    # укладывается в один кластер, как и требуется для честного "почти одна
    # и та же величина, с шумом округления", а не растянутый диапазон.
    content = [0.30 + i * 0.00002 for i in range(90)]
    values = decorative + content
    edge, confidence = _percentile_edge(values, _MARGIN_PERCENTILE)
    assert abs(edge - 0.30) < 0.01
    assert confidence > 0.8


def test_percentile_edge_reports_low_confidence_when_no_real_margin_exists():
    """Регрессия на находку код-ревью (п.6): на полностью шумной геометрии
    без единого системного кластера у края старая запасная ветка
    (`_nearest_edge`, наиболее частый кластер без учёта близости к краю)
    могла тихо вернуть "типичную позицию контента" с виду уверенно. Новая
    функция обязана явно просигналить низкой confidence, что найденная
    точка не похожа на поле, а не выдать шум за него.

    Требование 4 повторной правки (см. docstring _percentile_edge): когда
    рядом с точкой процентиля нет НИ ОДНОГО весомого кластера, функция
    обязана вернуть саму точку процентиля как есть (не придумывать
    значение) — и не поднимать уверенность."""
    # 200 точек, равномерно раскиданных по всему холсту — ни одного
    # системного повторения на расстоянии допуска кластеризации, у каждой
    # свой одиночный "кластер" весом 1/200 = 0.5%, ниже
    # _MIN_MARGIN_CLUSTER_SUPPORT_SHARE (1%) — ни один, включая кластер
    # самой точки процентиля, не проходит порог значимости.
    values = [i * 0.005 for i in range(200)]
    edge, confidence = _percentile_edge(values, _MARGIN_PERCENTILE)
    expected_point = sorted(values)[int(len(values) * _MARGIN_PERCENTILE)]
    assert edge == expected_point
    assert confidence < 0.1


def test_percentile_edge_returns_nearest_significant_cluster_center():
    """Главная находка этой правки (см. docstring _percentile_edge и
    `_MIN_MARGIN_CLUSTER_SUPPORT_SHARE`): процентиль находит ОБЛАСТЬ, но
    сама точка процентиля может провалиться в промежуток МЕЖДУ двумя
    настоящими кластерами — тогда значением поля должен стать центр
    БЛИЖАЙШЕГО к этой точке весомого кластера, а не сама точка и не более
    тяжёлый, но далёкий кластер.

    Синтетика с известным ответом (1000 измерений — достаточно, чтобы вес
    одной случайной точки (0.1%) был далеко и безопасно ниже порога
    значимости (1%), не впритык к нему): `cluster_a` — весомый кластер
    (9% всех измерений) около 0.05; `between` — пятнадцать ОДИНОЧНЫХ точек
    (каждая своим кластером веса 0.1%, ниже порога), ни одна сама по себе
    не образует кластера; `cluster_b` — заметно более тяжёлый (50%) кластер
    около 0.30, но заметно дальше от точки процентиля; `far_bulk` —
    заполнитель вдали от обоих кандидатов, нужен только чтобы 10-й
    процентиль (позиция 100 из 1000) попал ровно на одну из точек `between`
    (0.11) — между `cluster_a` и `cluster_b`, заметно ближе к `cluster_a`.

    Правильный ответ — центр `cluster_a` (~0.05): он ближе к точке
    процентиля, чем `cluster_b`, хотя и легче. Если бы алгоритм по-прежнему
    возвращал саму точку процентиля (0.11) или самый тяжёлый кластер
    (`cluster_b`, ~0.30), тест бы упал."""
    cluster_a = [0.05 + i * 0.00003 for i in range(90)]
    between = [0.06 + i * 0.005 for i in range(15)]
    cluster_b = [0.30 + i * 0.000005 for i in range(500)]
    far_bulk = [0.50 + i * 0.001 for i in range(395)]
    values = cluster_a + between + cluster_b + far_bulk
    assert len(values) == 1000
    assert sorted(values) == values  # проверка конструкции синтетики

    edge, confidence = _percentile_edge(values, _MARGIN_PERCENTILE)

    assert abs(edge - 0.05) < 0.005, edge  # центр cluster_a, не точка и не cluster_b
    assert abs(edge - 0.11) > 0.01  # не сама точка процентиля
    assert abs(edge - 0.30) > 0.1  # не более тяжёлый, но далёкий кластер
    assert abs(confidence - 0.09) < 0.01  # вес выбранного кластера (90/1000), не 1/1000


def test_every_reported_number_carries_a_confidence_score(profile_fixture):
    """«Каждое число... должно нести меру уверенности» — margin_left/right/
    top/bottom, columns, gutter и baseline обязаны иметь запись в confidence."""
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    for key in ("margin_left", "margin_right", "margin_top", "margin_bottom",
                "columns", "gutter", "baseline"):
        assert key in grid.confidence
        assert 0.0 <= grid.confidence[key] <= 1.0
