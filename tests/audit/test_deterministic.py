"""Тесты 24 детерминированных проверок (Task 11, Приложение 1 ТЗ).

Один тест на идентификатор (Step 1 брифа) плюс интеграционные: чистая
колода не даёт ни одной находки, повторный прогон даёт тот же результат.

Большинство фикстур строят дефект НА ВЕРШИНЕ заведомо чистого слайда
(`deck_with`, см. `conftest.py`) — так исключается риск, что сам тестовый
дефект случайно заденет соседнюю проверку. Где нужен слайд с "нуля"
(таблица/график/пустой/растровый/чужой макет) — `blank_deck`/`foreign_deck`
(шаблонный макет без единого плейсхолдера, ничего постороннего не
наследуется) или `foreign_deck` (стоковый `python-pptx`, заведомо не из
`PROFILE.layouts`, нужен T04).

Примеры L01/L02/L07/T03/T06/D05/I04/I06 — из брифа дословно (поведение и
имена), геометрия фикстур — своя, подобранная под реальные намайненные
поля/кегли/цвета VK Tech (см. диагностику в отчёте задачи)."""
from __future__ import annotations
import copy
from pathlib import Path

from lxml import etree
from PIL import Image
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import Finding, run_deterministic
from deckforge.compose.builder import Variant, build_deck
from deckforge.compose.charts import ChartSpec, Series, add_chart
from deckforge.compose.tables import TableSpec, add_table
from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec
from deckforge.template.profile import TemplateProfile

# Не импортируется из `conftest.py` (`from tests.audit.conftest import ...`)
# намеренно: `tests/` — не пакет (нет `tests/__init__.py`, тот же принцип,
# что и в `tests/compose/test_builder.py`, который точно так же заново
# объявляет свои PROFILE/TEMPLATE, а не тянет их из соседнего conftest.py
# через точечный импорт).
TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")
PROFILE = TemplateProfile.from_file(TEMPLATE, cache_dir=None)
CONFIG = AuditConfig.load()


def _ids(findings: list[Finding]) -> set[str]:
    return {f.check_id for f in findings}


def _add_text(slide, left_in, top_in, width_in, height_in, text, *, size_pt=18, color_hex=None, family="Arial"):
    """`family` по умолчанию "Arial", НЕ `None` — без явного `run.font.name`
    `python-pptx` не пишет `a:latin` вовсе, и `_dominant_run_style`
    (`deckforge.audit.deterministic`) молча пропускает run без гарнитуры
    (см. докстроку `_all_run_families`/`_dominant_run_style`: замер/T01/T02/
    T06/I03 читают гарнитуру И кегль ИЗ ОДНОГО run'а, run без `a:latin` для
    них как будто не существует). Найдено этой же задачей — первый прогон
    тестов L03/L04/I03 молча ничего не находил именно по этой причине."""
    box = slide.shapes.add_textbox(Inches(left_in), Inches(top_in), Inches(width_in), Inches(height_in))
    tf = box.text_frame
    tf.word_wrap = True
    run = tf.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(size_pt)
    run.font.name = family
    if color_hex:
        run.font.color.rgb = RGBColor.from_string(color_hex)
    return box


def _make_bulleted(paragraph, char: str = "•") -> None:
    """Тот же приём, что `compose.builder._apply_bullet` — ставит явный
    `a:buChar`, которым и D01, и D02 опознают буллет-абзац."""
    p_pr = paragraph._p.get_or_add_pPr()
    bu_font = etree.SubElement(p_pr, qn("a:buFont"))
    bu_font.set("typeface", "Arial")
    bu_char = etree.SubElement(p_pr, qn("a:buChar"))
    bu_char.set("char", char)


# ---------------------------------------------------------------------------
# L01 — элемент вышел за границы слайда
# ---------------------------------------------------------------------------


def test_L01_catches_shape_outside_the_slide(deck_with):
    path = deck_with(lambda s: s.shapes.add_textbox(Inches(20), Inches(1), Inches(3), Inches(1)))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"L01"}


# ---------------------------------------------------------------------------
# L02 — два блока наложились друг на друга
# ---------------------------------------------------------------------------


def test_L02_ignores_adjacent_blocks_but_catches_real_overlap(deck_with):
    """Геометрия своя (два блока 1.5×1″, кегль 54pt — легитимная ступень
    шкалы VK Tech, не ловит побочный T02 — подобран так, чтобы измеренная
    высота текста была близка к объявленной рамке — калибровка именно
    наезда, не побочного эффекта измеренной площади): нахлёст 0″ даёт 0%
    площади меньшего блока (просто соприкасаются), нахлёст 0.5″ даёт 33% —
    заведомо по разные стороны от порога `overlap_ratio=0.07` (см.
    `config/audit.yaml`)."""
    def make(overlap_in):
        def _fn(slide):
            top = Inches(2.0)
            left1 = Inches(0.3)
            box_w, box_h = Inches(1.5), Inches(1.0)
            t1 = slide.shapes.add_textbox(left1, top, box_w, box_h)
            r1 = t1.text_frame.paragraphs[0].add_run()
            r1.text = "AB"
            r1.font.size = Pt(54)
            left2 = Inches(0.3 + 1.5 - overlap_in)
            t2 = slide.shapes.add_textbox(left2, top, box_w, box_h)
            r2 = t2.text_frame.paragraphs[0].add_run()
            r2.text = "CD"
            r2.font.size = Pt(54)
        return _fn

    adjacent = deck_with(make(0.0))
    overlapping = deck_with(make(0.5))
    assert "L02" not in _ids(run_deterministic(adjacent, PROFILE, CONFIG))
    assert "L02" in _ids(run_deterministic(overlapping, PROFILE, CONFIG))


def test_L02_does_not_flag_text_on_its_own_plate(deck_with):
    def _fn(slide):
        plate = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.3), Inches(1.3), Inches(3.0), Inches(1.0))
        plate.fill.solid()
        plate.fill.fore_color.rgb = RGBColor.from_string(next(iter(PROFILE.palette_roles.values())).lstrip("#"))
        plate.line.fill.background()
        _add_text(slide, 0.5, 1.5, 2.5, 0.6, "Текст на своей плашке", size_pt=18)
    path = deck_with(_fn)
    assert "L02" not in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_L02_does_flag_text_on_top_of_a_chart(deck_with):
    def _fn(slide):
        add_chart(
            slide, Box(0.05, 0.2, 0.35, 0.5),
            ChartSpec(kind="bar", categories=["A", "B", "C"], series=[Series("S1", [1, 2, 3])]),
            PROFILE,
        )
        _add_text(slide, 1.0, 1.5, 1.5, 0.5, "Поверх графика", size_pt=14)
    path = deck_with(_fn)
    assert "L02" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# L03 — текст не поместился в свою рамку
# ---------------------------------------------------------------------------


def test_L03_catches_text_overflowing_its_frame(deck_with):
    """`family="Play"` (родная гарнитура шаблона) и умеренный (не
    экстремальный) объём текста — иначе тест ловил бы ПОБОЧНЫЙ T01 (Arial —
    не гарнитура шаблона) и/или L04 (при достаточно длинном тексте
    переполнение проезжает весь остаток холста и упирается в нижний край) —
    находка код-ревью (Task 11 повторное ревью, находка №4)."""
    path = deck_with(lambda s: _add_text(
        s, 0.3, 1.3, 2.0, 0.3, ("слово " * 5).strip(), size_pt=24, family="Play",
    ))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"L03"}


def test_L03_accounts_for_bodyPr_insets_on_a_foreign_file(deck_with):
    """Регрессия на находку код-ревью (Task 11 повторное ревью, находка №2):
    `_effective_box`/L03/L04/D05/L02 раньше мерили текст по ПОЛНОЙ ширине/
    высоте рамки, не вычитая внутренние поля рамки (`a:bodyPr` lIns/tIns/
    rIns/bIns). Для НАШЕЙ сборки это было верно (она обнуляет поля нарочно,
    см. `compose/builder.py::_draw_slot`), но аудит применяется к
    ПРОИЗВОЛЬНОМУ чужому файлу, где поля не нулевые — `python-pptx`
    (`_add_text` здесь их не трогает, как и любой сторонний генератор,
    который на них не завязан) ставит дефолт OOXML (0.1″ слева/справа,
    0.05″ сверху/снизу).

    Числа — буквально пример отчёта задачи: без учёта полей текст меряется
    как 5 строк/1.67″ (влезает в рамку высотой 1.8″), с учётом полей — как
    6 строк/2.0″ (не влезает в доступные 1.8-0.1=1.7″ высоты, при том что
    ширина для переноса тоже уже — 2.3-0.2=2.1″, из-за чего "заявок" уходит
    на отдельную строку).

    `family="Arial"` — ЯВНО (не дефолт `_add_text`), потому что расчёт выше
    калиброван по метрикам ИМЕННО Arial; смена гарнитуры сдвинула бы разбор
    по строкам и молча сломала бы калибровку. Arial здесь не гарнитура
    шаблона (VK Tech — "Play") — заодно и корректно, сам сценарий "чужой
    файл": T01 в наборе находок ниже ожидаем и честно задокументирован, не
    маскируется вхождением."""
    text = "Автоматическая маршрутизация заявок клиентов в очередь поддержки"
    path = deck_with(lambda s: _add_text(s, 0.3, 1.3, 2.3, 1.8, text, size_pt=20, family="Arial"))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"L03", "T01"}


# ---------------------------------------------------------------------------
# L04 — текст обрезан краем слайда
# ---------------------------------------------------------------------------


def test_L04_catches_text_clipped_by_the_slide_edge(deck_with):
    """`family="Play"` — иначе тест ловил бы побочный T01 (Task 11 повторное
    ревью, находка №4). L03 в наборе находок ОЖИДАЕМО, не маскируется:
    L04 структурно требует того же условия, что и L03 (текст выше
    объявленной рамки), плюс ещё то, что переполнение упирается в нижний
    край слайда — L04 "сам по себе", без L03 рядом, здесь получить нельзя."""
    def _fn(slide):
        _add_text(slide, 0.3, 5.3, 2.0, 0.2, "слово " * 30, size_pt=24, family="Play")
    path = deck_with(_fn)
    ids = _ids(run_deterministic(path, PROFILE, CONFIG))
    assert ids == {"L03", "L04"}


# ---------------------------------------------------------------------------
# L05 — блоки не выровнены по направляющим макета
# ---------------------------------------------------------------------------


def test_L05_catches_a_block_off_the_template_grid(deck_with):
    """left=0.19/right=0.32 доли холста — намеренно между всеми "достойными"
    осями VK Tech (направляющие/кластеры с поддержкой ≥1% измерений, см.
    `_check_L05`/`config.layout.grid_axis_min_support_share`), с запасом
    заметно больше `grid_tolerance`. Высота рамки (0.7″) — с запасом на
    внутренние поля рамки (Task 11 повторное ревью, находка №2): без
    запаса тест ловил бы ПОБОЧНЫЙ L03, а не только L05."""
    def _fn(slide):
        _add_text(slide, 1.9, 1.3, 1.3, 0.7, "Смещённый блок", size_pt=14)
    path = deck_with(_fn)
    assert "L05" in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_L05_does_not_flag_a_block_aligned_to_a_well_supported_cluster_axis(deck_with):
    """Регрессия на находку код-ревью (Task 11 повторное ревью, находка №1).
    Раньше L05 брал только ТОП-4 оси `Grid.columns` ПО СПИСКУ (тот
    отсортирован по confidence), а направляющие (`p:guide`) получали
    confidence=1.0 НЕЗАВИСИМО от реальной поддержки — четыре направляющие с
    поддержкой в два измерения каждая занимали все четыре места, вытесняя
    кластерную ось с поддержкой в 470 измерений (типичная ситуация
    контрольного/насыщенного шаблона, см. отчёт задачи: "и оба на
    контрольной — это блоки, выровненные по настоящим колонкам сетки"), и
    блок, реально выровненный по НЕЙ, ложно считался не выровненным ни по
    чему.

    Синтетика (не наблюдение за конкретным файлом): 4 направляющие с
    крошечной поддержкой и одна кластерная ось с массовой — воспроизводит
    структуру находки напрямую, без привязки к тому, сколько именно осей
    наберётся на живом VK Tech. Координаты осей (0.10/0.35, не "круглые"
    0.1/0.4) и позиции блоков подобраны так, чтобы не задевать ГЕОМЕТРИЮ
    собственного содержимого `CLEAN_SPEC` (заголовок сверху, буллеты
    справа, см. `conftest.py`) — иначе тест ловил бы ПОБОЧНЫЙ L02, не
    только L05/его отсутствие; `family="Play"` (родная гарнитура шаблона)
    — по той же причине, чтобы не ловить побочный T01."""
    from deckforge.template.profile import ColumnAxisModel

    noisy_guides = [
        ColumnAxisModel(center=0.10 + i * 0.001, count=2, confidence=1.0, source="guide")
        for i in range(4)
    ]
    real_axis = ColumnAxisModel(center=0.35, count=470, confidence=0.05, source="cluster")
    grid = PROFILE.grid.model_copy(update={"columns": [*noisy_guides, real_axis]})
    profile = PROFILE.model_copy(update={"grid": grid})

    # На настоящей массовой оси (0.35 доли холста = 3.5″ на холсте 10″) —
    # не должен флагаться, несмотря на то что она не входит в "первые
    # четыре" направляющие по старому (сломанному) критерию.
    on_real_axis = deck_with(lambda s: _add_text(s, 3.5, 1.3, 0.6, 0.7, "На оси", size_pt=14, family="Play"))
    assert _ids(run_deterministic(on_real_axis, profile, CONFIG)) == set()

    # На направляющей с крошечной поддержкой — тоже не должен флагаться:
    # направляющая авторитетна как ИСТОЧНИК независимо от статистики
    # (см. докстроку `ColumnAxis` в template/grid.py).
    on_guide = deck_with(lambda s: _add_text(s, 1.0, 1.3, 1.5, 0.7, "На направляющей", size_pt=14, family="Play"))
    assert _ids(run_deterministic(on_guide, profile, CONFIG)) == set()

    # Контроль: блок, не выровненный НИ ПО ЧЕМУ из этого набора, обязан
    # по-прежнему флагаться — правка не превратила L05 в проверку, которая
    # никогда не срабатывает.
    nowhere = deck_with(lambda s: _add_text(s, 2.5, 1.3, 1.5, 0.7, "Нигде", size_pt=14, family="Play"))
    assert _ids(run_deterministic(nowhere, profile, CONFIG)) == {"L05"}


# ---------------------------------------------------------------------------
# L06 — контент заходит в поля у краёв
# ---------------------------------------------------------------------------


def test_L06_catches_content_inside_the_margin_band(deck_with):
    """left=0.25″ подобран так, чтобы попасть ОДНОВРЕМЕННО в обе зоны:
    строго внутри поля (< margin_left-tol, ловит L06) и достаточно близко к
    margin_left, чтобы L05 считал блок выровненным по нему (в пределах
    grid_tolerance) — иначе тест ловил бы ПОБОЧНЫЙ L05 (край слайда
    структурно редко совпадает хоть с одной направляющей сетки). Ширина/
    высота рамки (1.3″×0.7″) и `family="Play"` — чтобы не ловить побочные
    L03/T01 (Task 11 повторное ревью, находка №4)."""
    def _fn(slide):
        _add_text(slide, 0.25, 1.3, 1.3, 0.7, "У самого края", size_pt=14, family="Play")
    path = deck_with(_fn)
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"L06"}


# ---------------------------------------------------------------------------
# L07 — картинка растянута, пропорции нарушены
# ---------------------------------------------------------------------------


def test_L07_catches_stretched_picture(deck_with, tmp_path):
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (800, 600), color=(50, 60, 70)).save(img_path)

    def _fn(slide):
        slide.shapes.add_picture(str(img_path), Inches(0.3), Inches(1.3), Inches(3.0), Inches(0.6))
    path = deck_with(_fn)
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"L07"}


# ---------------------------------------------------------------------------
# T01 — шрифт не из шаблона / гарнитур больше двух
# ---------------------------------------------------------------------------


def test_T01_catches_a_font_outside_the_template(deck_with):
    """Рамка выше, чем в исходной находке (1.0″, не 0.6″) — 0.6″ было
    тесно даже без учёта полей рамки, и находка код-ревью (Task 11
    повторное ревью, находка №4) поймала здесь побочный L03."""
    path = deck_with(lambda s: _add_text(
        s, 0.3, 1.3, 2.5, 1.0, "Инородный шрифт", size_pt=20, family="Comic Sans MS",
    ))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"T01"}


def test_T01_catches_more_than_two_font_families_on_one_slide(deck_with):
    def _fn(slide):
        for i, family in enumerate(("Arial", "Georgia", "Verdana")):
            _add_text(slide, 0.3, 1.3 + i * 0.5, 2.5, 0.4, f"Текст {i}", size_pt=14, family=family)
    path = deck_with(_fn)
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"T01"}


def test_T01_catches_a_foreign_font_inside_a_table_cell(blank_deck):
    """Регрессия на находку код-ревью (Task 11 повторное ревью, находка №6):
    T01 раньше не заглядывал внутрь таблиц вовсе (`graphic_frame` исключён
    из `_text_shapes`) — гарнитура вне шаблона в ячейке проходила молча."""
    slide = blank_deck.add_slide()
    shp = add_table(slide, Box(0.05, 0.1, 0.85, 0.3), TableSpec(header=["A", "B"], rows=[["1", "2"]]), PROFILE)
    shp.table.cell(0, 0).text_frame.paragraphs[0].runs[0].font.name = "Comic Sans MS"
    path = blank_deck.save_as("table_font.pptx")
    assert "T01" in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_T01_catches_a_foreign_font_inside_a_chart_axis_title(blank_deck):
    """Та же находка №6 — заголовок оси графика (буквальный `c:rich`)."""
    slide = blank_deck.add_slide()
    spec = ChartSpec(
        kind="bar", categories=["A", "B", "C"], series=[Series("S1", [1, 2, 3])],
        axis_titles=("Категория", "Значение"),
    )
    shp = add_chart(slide, Box(0.05, 0.1, 0.85, 0.6), spec, PROFILE)
    shp.chart.category_axis.axis_title.text_frame.paragraphs[0].runs[0].font.name = "Comic Sans MS"
    path = blank_deck.save_as("chart_font.pptx")
    assert "T01" in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_T01_is_silent_on_a_clean_table_and_chart(blank_deck):
    """Пара к обоим тестам выше — сам факт наличия таблицы/графика с
    родными шрифтами шаблона не должен давать находку."""
    slide = blank_deck.add_slide()
    add_table(slide, Box(0.05, 0.1, 0.4, 0.3), TableSpec(header=["A", "B"], rows=[["1", "2"]]), PROFILE)
    spec = ChartSpec(
        kind="bar", categories=["A", "B", "C"], series=[Series("S1", [1, 2, 3])],
        axis_titles=("Категория", "Значение"),
    )
    add_chart(slide, Box(0.5, 0.1, 0.4, 0.6), spec, PROFILE)
    path = blank_deck.save_as("table_chart_clean.pptx")
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == set()


# ---------------------------------------------------------------------------
# T02 — кегль не из типографической шкалы шаблона
# ---------------------------------------------------------------------------


def test_T02_catches_an_off_scale_font_size(deck_with):
    """37pt — намеренно между соседними легитимными размерами VK Tech
    (24.0 и 54.0, см. диагностику отчёта задачи), заведомо дальше допуска
    `size_tolerance_pt=0.5`. Рамка 3.0″×1.5″ и `family="Play"` — с запасом
    под 37pt (иначе, как и выше, ловится побочный L03/T01; Task 11
    повторное ревью, находка №4)."""
    path = deck_with(lambda s: _add_text(s, 0.3, 1.3, 3.0, 1.5, "Кегль не по шкале", size_pt=37, family="Play"))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"T02"}


def test_T02_catches_an_off_scale_font_size_inside_a_table_cell(blank_deck):
    """Регрессия на находку код-ревью (Task 11 повторное ревью, находка №6)
    — ИМЕННО в этом месте (таблицы/графики) нашёлся реальный денормированный
    кегль (Task 10 фикс, см. коммит "денормировать кегль шкалы шаблона в
    графиках/таблицах/схемах") — T02 раньше физически не мог его увидеть,
    таблицы/графики были вне поля зрения проверки."""
    slide = blank_deck.add_slide()
    shp = add_table(slide, Box(0.05, 0.1, 0.85, 0.3), TableSpec(header=["A", "B"], rows=[["1", "2"]]), PROFILE)
    shp.table.cell(0, 0).text_frame.paragraphs[0].runs[0].font.size = Pt(37)
    path = blank_deck.save_as("table_size.pptx")
    assert "T02" in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_T02_catches_an_off_scale_font_size_inside_a_chart_axis_title(blank_deck):
    slide = blank_deck.add_slide()
    spec = ChartSpec(
        kind="bar", categories=["A", "B", "C"], series=[Series("S1", [1, 2, 3])],
        axis_titles=("Категория", "Значение"),
    )
    shp = add_chart(slide, Box(0.05, 0.1, 0.85, 0.6), spec, PROFILE)
    shp.chart.category_axis.axis_title.text_frame.paragraphs[0].runs[0].font.size = Pt(37)
    path = blank_deck.save_as("chart_size.pptx")
    assert "T02" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# T03 — цвет не из палитры шаблона
# ---------------------------------------------------------------------------


def test_T03_catches_color_outside_the_palette(deck_with):
    """Рамка 3.0″×1.0″ и `family="Play"` — с запасом под кегль (иначе
    побочный L03/T01, Task 11 повторное ревью, находка №4)."""
    path = deck_with(lambda s: _add_text(
        s, 0.3, 1.3, 3.0, 1.0, "Цвет вне палитры", size_pt=20, color_hex="FF00FF", family="Play",
    ))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"T03"}


def test_T03_catches_a_color_outside_the_palette_inside_a_table_cell(blank_deck):
    """Регрессия на находку код-ревью (Task 11 повторное ревью, находка №6)."""
    slide = blank_deck.add_slide()
    shp = add_table(slide, Box(0.05, 0.1, 0.85, 0.3), TableSpec(header=["A", "B"], rows=[["1", "2"]]), PROFILE)
    shp.table.cell(0, 0).text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor.from_string("FF00FF")
    path = blank_deck.save_as("table_color.pptx")
    assert "T03" in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_T03_catches_a_color_outside_the_palette_inside_a_chart_axis_title(blank_deck):
    slide = blank_deck.add_slide()
    spec = ChartSpec(
        kind="bar", categories=["A", "B", "C"], series=[Series("S1", [1, 2, 3])],
        axis_titles=("Категория", "Значение"),
    )
    shp = add_chart(slide, Box(0.05, 0.1, 0.85, 0.6), spec, PROFILE)
    shp.chart.category_axis.axis_title.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor.from_string("FF00FF")
    path = blank_deck.save_as("chart_color.pptx")
    assert "T03" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# T04 — слайд собран не на макете из шаблона
# ---------------------------------------------------------------------------


def test_T04_catches_a_slide_not_built_on_a_template_layout(foreign_deck):
    path = foreign_deck(lambda s: _add_text(s, 1.0, 1.0, 2.0, 1.0, "Чужой макет", size_pt=18))
    assert "T04" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# T05 — логотип или колонтитул сдвинуты с положенного места
# ---------------------------------------------------------------------------


def test_T05_catches_the_logo_moved_from_its_place(deck_with, tmp_path):
    logo = PROFILE.assets.logo
    assert logo is not None, "у VK Tech обязан быть распознанный логотип — иначе тест ничего не проверяет"
    with PptxPackage.open(TEMPLATE) as pkg:
        logo_bytes = pkg.part(logo.part_name)
    img_path = tmp_path / "logo.png"
    img_path.write_bytes(logo_bytes)

    def _fn(slide):
        # Заведомо не namайненное место (logo_placements — верхний край
        # титульных/секционных макетов, см. диагностику отчёта задачи).
        slide.shapes.add_picture(str(img_path), Inches(3.0), Inches(3.0), Inches(0.6), Inches(0.21))
    path = deck_with(_fn)
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"T05"}


# ---------------------------------------------------------------------------
# T06 — контраст текста к фону ниже 4.5:1
# ---------------------------------------------------------------------------


def test_T06_measures_contrast_against_the_plate_under_the_text(deck_with):
    """Фон берётся тот, что реально под текстом: своя заливка → объемлющая
    плашка → подложка слайда → фон макета (бриф дословно)."""
    def with_plate(text_hex, plate_hex):
        def _fn(slide):
            plate = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.3), Inches(1.3), Inches(3.0), Inches(1.0))
            plate.fill.solid()
            plate.fill.fore_color.rgb = RGBColor.from_string(plate_hex)
            plate.line.fill.background()
            _add_text(slide, 0.5, 1.5, 2.5, 0.6, "Текст на плашке", size_pt=20, color_hex=text_hex)
        return _fn

    light_on_dark = deck_with(with_plate("FFFFFF", "000000"))
    light_on_light = deck_with(with_plate("FFFFFF", "F5F5F5"))
    assert "T06" not in _ids(run_deterministic(light_on_dark, PROFILE, CONFIG))
    assert "T06" in _ids(run_deterministic(light_on_light, PROFILE, CONFIG))


def test_T06_catches_low_contrast_inside_a_chart_axis_title(blank_deck):
    """Регрессия на находку код-ревью (Task 11 повторное ревью, находка №6)
    — низкий контраст заголовка оси графика (почти цвет фона слайда) раньше
    T06 не видел, так как графики были вне поля зрения всех проверок
    T01-T06 (см. `test_T01_catches_a_foreign_font_inside_a_chart_axis_title`
    и докстроку раздела "Текст внутри таблиц и графиков" в deterministic.py)."""
    slide = blank_deck.add_slide()
    spec = ChartSpec(
        kind="bar", categories=["A", "B", "C"], series=[Series("S1", [1, 2, 3])],
        axis_titles=("Категория", "Значение"),
    )
    shp = add_chart(slide, Box(0.05, 0.1, 0.85, 0.6), spec, PROFILE)
    shp.chart.category_axis.axis_title.text_frame.paragraphs[0].runs[0].font.color.rgb = RGBColor.from_string("F5F5F5")
    path = blank_deck.save_as("chart_contrast.pptx")
    assert "T06" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# D01/D02 — плотность буллетов
# ---------------------------------------------------------------------------


def test_D01_catches_more_than_six_bullets(deck_with):
    def _fn(slide):
        box = slide.shapes.add_textbox(Inches(0.3), Inches(1.3), Inches(3.5), Inches(3.5))
        tf = box.text_frame
        tf.word_wrap = True
        for i in range(8):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = p.add_run()
            run.text = f"Пункт номер {i}"
            run.font.size = Pt(14)
            _make_bulleted(p)
    path = deck_with(_fn)
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"D01"}


def test_D02_catches_a_bullet_longer_than_the_limit(deck_with):
    def _fn(slide):
        box = slide.shapes.add_textbox(Inches(0.3), Inches(1.3), Inches(3.5), Inches(1.5))
        tf = box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        run = p.add_run()
        run.text = " ".join(f"слово{i}" for i in range(20))
        run.font.size = Pt(12)
        _make_bulleted(p)
    path = deck_with(_fn)
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"D02"}


# ---------------------------------------------------------------------------
# D03 — таблица больше 7 строк или 5 колонок
# ---------------------------------------------------------------------------


def test_D03_catches_an_oversized_table(blank_deck):
    slide = blank_deck.add_slide()
    header = [f"Кол{i}" for i in range(6)]
    rows = [[str(i * 6 + j) for j in range(6)] for i in range(8)]
    add_table(slide, Box(0.05, 0.1, 0.85, 0.7), TableSpec(header=header, rows=rows), PROFILE)
    path = blank_deck.save_as("table.pptx")
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"D03"}


# ---------------------------------------------------------------------------
# D04 — больше 5 серий на диаграмме
# ---------------------------------------------------------------------------


def test_D04_catches_too_many_chart_series(blank_deck):
    slide = blank_deck.add_slide()
    spec = ChartSpec(
        kind="bar", categories=["A", "B", "C"],
        series=[Series(f"S{i}", [1, 2, 3]) for i in range(7)],
        axis_titles=("Категория", "Значение"),
    )
    add_chart(slide, Box(0.1, 0.15, 0.75, 0.6), spec, PROFILE)
    path = blank_deck.save_as("chart.pptx")
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"D04"}


# ---------------------------------------------------------------------------
# D05 — слайд заполнен меньше четверти или больше трёх четвертей
# ---------------------------------------------------------------------------


def _bullets_deck(n_bullets: int, words_each: int) -> Path:
    """Колода из одного слайда с заданным числом буллетов.

    Декоративные картинки из профиля вырезаны по той же причине, что и в
    `conftest.clean_deck_path`: проверяется ПЛОТНОСТЬ СОДЕРЖАНИЯ, а
    фирменная графика шаблона (её сборка переносит на слайд с 25 сентября
    2026) сама по себе заполняет холст и делает «слишком пустой» слайд
    непустым. Для продукта это верно — слайд с крупным орнаментом
    действительно не выглядит пустым, — но здесь измеряется другое."""
    items = [" ".join(f"слово{j}" for j in range(words_each)) for _ in range(n_bullets)]
    spec = DeckSpec(
        title="D05", language="ru",
        slides=[SlideSpec(index=0, kind="bullets", headline="Заголовок", blocks=[BulletBlock(items=items)])],
    )
    profile = PROFILE.model_copy(update={
        "patterns": [
            p.model_copy(update={"decor": [d for d in p.decor if d.kind != "picture"]})
            for p in PROFILE.patterns
        ],
    })
    return build_deck(spec, profile, TEMPLATE, Variant.dense)


def test_D05_flags_both_empty_and_overstuffed_slides(blank_deck, clean_deck_path):
    """"Слишком пусто" — через `build_deck` (один короткий буллет: реальная
    сборка сама не тянет фон под содержание, ей нечем). "Слишком плотно" —
    напрямую: сборка АКТИВНО избегает переполнения (тот же порог D05
    участвует в ранжировании паттерна, см. `builder._fill_badness`) и
    почти никогда не выбирает раскладку, где реальное содержание залило бы
    больше 75% холста, — единственный надёжный способ воспроизвести именно
    ЭТОТ симптом для теста — закрашенная фигура заведомой площади, площадь
    которой D05 считает НАПРЯМУЮ (декларированным боксом, не через
    textfit)."""
    sparse = run_deterministic(_bullets_deck(1, 3), PROFILE, CONFIG)

    slide = blank_deck.add_slide()
    plate = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.3), Inches(0.15), Inches(9.4), Inches(5.3))
    plate.fill.solid()
    plate.fill.fore_color.rgb = RGBColor.from_string(next(iter(PROFILE.palette_roles.values())).lstrip("#"))
    plate.line.fill.background()
    overstuffed = run_deterministic(blank_deck.save_as("overstuffed.pptx"), PROFILE, CONFIG)

    # `clean_deck_path` уже подобран (см. `conftest.CLEAN_SPEC`) так, чтобы
    # попадать в [0.25, 0.75] — используем его же как образец "нормально".
    normal = run_deterministic(clean_deck_path, PROFILE, CONFIG)
    assert "D05" in _ids(sparse)
    assert "D05" in _ids(overstuffed)
    assert "D05" not in _ids(normal)


# ---------------------------------------------------------------------------
# I01 — файл не открывается
# ---------------------------------------------------------------------------


def test_I01_catches_a_file_that_does_not_open(tmp_path):
    bad = tmp_path / "broken.pptx"
    bad.write_bytes(b"this is not a zip archive at all")
    assert _ids(run_deterministic(bad, PROFILE, CONFIG)) == {"I01"}


# ---------------------------------------------------------------------------
# I02 — остался текст-заглушка
# ---------------------------------------------------------------------------


def test_I02_catches_placeholder_text(deck_with):
    """Рамка 3.5″×1.0″ и `family="Play"` — с запасом под кегль (иначе
    побочный L03/T01, Task 11 повторное ревью, находка №4)."""
    path = deck_with(lambda s: _add_text(
        s, 0.3, 1.3, 3.5, 1.0, "Lorem ipsum dolor sit amet", size_pt=18, family="Play",
    ))
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"I02"}


# ---------------------------------------------------------------------------
# I03 — пустой слайд или слайд с одним заголовком
# ---------------------------------------------------------------------------


def test_I03_catches_a_completely_empty_slide(blank_deck):
    """D05 в наборе находок ОЖИДАЕМО, не маскируется: слайд без единого
    объекта структурно не может занимать четверть и больше площади холста
    — "пусто" по I03 и "заполнено < 25%" по D05 — один и тот же слайд,
    увиденный двумя проверками с разных сторон, не побочный эффект теста."""
    blank_deck.add_slide()
    path = blank_deck.save_as("empty.pptx")
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"D05", "I03"}


def test_I03_catches_a_slide_with_only_a_small_text_block(blank_deck):
    """12pt — заведомо ниже заголовочного кегля шаблона (h2 денормирован
    до 19.875pt на VK Tech, см. диагностику отчёта задачи): мелкий
    одинокий текст — забытое содержание, не титульный слайд-разделитель.
    `family="Play"` — иначе побочный T01 (Task 11 повторное ревью,
    находка №4); D05 в наборе находок ОЖИДАЕМО, см. комментарий у
    `test_I03_catches_a_completely_empty_slide` — та же логика: слайд
    почти без содержания структурно заодно и "заполнен меньше четверти"."""
    slide = blank_deck.add_slide()
    _add_text(slide, 0.3, 1.3, 2.0, 0.4, "мелкая подпись", size_pt=12, family="Play")
    path = blank_deck.save_as("almost_empty.pptx")
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"D05", "I03"}


# ---------------------------------------------------------------------------
# I04 — слайд оказался картинкой, а не редактируемыми объектами
# ---------------------------------------------------------------------------


def test_I04_flags_a_slide_that_is_one_big_picture(blank_deck, tmp_path):
    img_path = tmp_path / "shot.png"
    Image.new("RGB", (800, 450), color=(10, 20, 30)).save(img_path)
    slide = blank_deck.add_slide()
    slide.shapes.add_picture(str(img_path), Inches(0.05), Inches(0.05), Inches(9.9), Inches(5.55))
    path = blank_deck.save_as("raster.pptx")
    # D05 в наборе находок ОЖИДАЕМО, не маскируется: картинка, крупная
    # достаточно, чтобы I04 счёл слайд "выгруженным целиком растром" (порог
    # `single_picture_coverage=0.9`), по построению заполняет больше трёх
    # четвертей холста — то же свойство геометрии, увиденное двумя разными
    # проверками, не побочный эффект теста.
    #
    # L06 (поля) здесь БОЛЬШЕ НЕ ожидается: с 25 сентября 2026 картинка без
    # текста из проверки полей исключена — оформление выпускают за поля
    # намеренно, «под обрез» (см. `_check_L06`). Слайд, выгруженный целиком
    # растром, ловит I04, и это правильная проверка для такого брака;
    # ругать его ещё и за поля значило бы ругать всякий фон.
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"I04", "D05"}


# ---------------------------------------------------------------------------
# I05 — у диаграммы нет подписей осей, единиц или легенды
# ---------------------------------------------------------------------------


def test_I05_catches_a_chart_without_axis_labels_or_legend(blank_deck):
    slide = blank_deck.add_slide()
    chart_data = CategoryChartData()
    chart_data.categories = ["A", "B", "C"]
    chart_data.add_series("S1", (1, 2, 3))
    chart_data.add_series("S2", (2, 3, 1))
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(1), Inches(6), Inches(3), chart_data,
    )
    path = blank_deck.save_as("bare_chart.pptx")
    assert _ids(run_deterministic(path, PROFILE, CONFIG)) == {"I05"}


# ---------------------------------------------------------------------------
# I06 — два слайда дублируют друг друга
# ---------------------------------------------------------------------------


def test_I06_flags_duplicate_slides():
    slide = SlideSpec(
        index=0, kind="bullets", headline="Где уходит время на согласование заявок",
        blocks=[BulletBlock(items=[
            "Ожидание первого согласующего — медиана 18 часов по данным марта-мая",
            "Ожидание второго согласующего — медиана 11 часов, часто дольше вечером",
        ])],
    )
    dup = copy.deepcopy(slide)
    dup.index = 1
    spec = DeckSpec(title="Дубликат", language="ru", slides=[slide, dup])
    path = build_deck(spec, PROFILE, TEMPLATE, Variant.dense)
    findings = run_deterministic(path, PROFILE, CONFIG)
    assert "I06" in _ids(findings)
    # Автопочинки для I06 нет (удаление слайда по совпадению текста
    # опасно — ложное срабатывание убьёт настоящий слайд), находка должна
    # честно говорить об этом, не обещать fixable=True без фиксера.
    i06 = next(f for f in findings if f.check_id == "I06")
    assert i06.fixable is False


def test_I06_is_silent_on_two_genuinely_different_slides():
    """Пара к тесту выше (Task 11 повторное ревью, находка №4) — два
    слайда с РАЗНЫМ текстом (не просто разными заголовками при том же
    теле, а полностью другим содержанием) не должны считаться дубликатом
    друг друга."""
    slide_a = SlideSpec(
        index=0, kind="bullets", headline="Где уходит время на согласование заявок",
        blocks=[BulletBlock(items=[
            "Ожидание первого согласующего — медиана 18 часов по данным марта-мая",
            "Ожидание второго согласующего — медиана 11 часов, часто дольше вечером",
        ])],
    )
    slide_b = SlideSpec(
        index=1, kind="bullets", headline="Сколько стоит повторное согласование",
        blocks=[BulletBlock(items=[
            "Каждая повторная заявка добавляет в среднем 4 часа к общему циклу",
            "Стоимость часа согласующего в пересчёте на нагрузку — около 3100 рублей",
        ])],
    )
    spec = DeckSpec(title="Не дубликат", language="ru", slides=[slide_a, slide_b])
    path = build_deck(spec, PROFILE, TEMPLATE, Variant.dense)
    assert "I06" not in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# Интеграционные: чистая колода и детерминизм
# ---------------------------------------------------------------------------


def test_clean_deck_produces_no_findings(clean_deck_path):
    assert run_deterministic(clean_deck_path, PROFILE, CONFIG) == []


def test_same_file_gives_the_same_result_twice(clean_deck_path, deck_with):
    path = deck_with(lambda s: _add_text(s, 0.3, 1.3, 2.0, 0.4, "Стабильность результата", size_pt=14))
    first = run_deterministic(path, PROFILE, CONFIG)
    second = run_deterministic(path, PROFILE, CONFIG)
    assert first == second


def test_T03_accepts_the_templates_own_decor_colours(blank_deck):
    """Палитра ролей и тема собраны по ТЕКСТУ, а фирменные плашки часто
    залиты цветом, которого ни в одной роли нет: у ЛЦТ2026 это #FD0C50 и
    #E4EAE9. С переносом декора шаблона на слайд проверка начала ругаться
    на замысел самого шаблона — семь находок на двенадцать слайдов, все про
    его собственные цвета."""
    decor_colour = next(
        (d.fill_hex for p in PROFILE.patterns for d in p.decor if d.fill_hex),
        None,
    )
    if decor_colour is None:
        return  # у этого шаблона нет декора с разрешённым цветом — проверять нечего

    slide = blank_deck.add_slide()
    plaque = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.5), Inches(1.0), Inches(3.0), Inches(2.0))
    plaque.fill.solid()
    plaque.fill.fore_color.rgb = RGBColor.from_string(decor_colour.lstrip("#"))
    plaque.line.fill.background()

    ids = _ids(run_deterministic(blank_deck.save_as("decor-colour.pptx"), PROFILE, CONFIG))

    assert "T03" not in ids, "цвет декора самого шаблона не должен считаться чужим"
