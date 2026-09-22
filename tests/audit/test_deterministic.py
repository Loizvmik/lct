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
PROFILE = TemplateProfile.from_file(TEMPLATE)
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
    """Геометрия своя (два блока 1.5×1″, кегль 60pt подобран так, чтобы
    измеренная высота текста совпадала с объявленной рамкой — калибровка
    именно наезда, не побочного эффекта измеренной площади): нахлёст 0″
    даёт 0% площади меньшего блока (просто соприкасаются), нахлёст 0.5″
    даёт 33% — заведомо по разные стороны от порога `overlap_ratio=0.07`
    (см. `config/audit.yaml`)."""
    def make(overlap_in):
        def _fn(slide):
            top = Inches(2.0)
            left1 = Inches(0.3)
            box_w, box_h = Inches(1.5), Inches(1.0)
            t1 = slide.shapes.add_textbox(left1, top, box_w, box_h)
            r1 = t1.text_frame.paragraphs[0].add_run()
            r1.text = "AB"
            r1.font.size = Pt(60)
            left2 = Inches(0.3 + 1.5 - overlap_in)
            t2 = slide.shapes.add_textbox(left2, top, box_w, box_h)
            r2 = t2.text_frame.paragraphs[0].add_run()
            r2.text = "CD"
            r2.font.size = Pt(60)
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
    path = deck_with(lambda s: _add_text(
        s, 0.3, 1.3, 2.0, 0.3, "слово " * 40, size_pt=24,
    ))
    assert "L03" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# L04 — текст обрезан краем слайда
# ---------------------------------------------------------------------------


def test_L04_catches_text_clipped_by_the_slide_edge(deck_with):
    def _fn(slide):
        _add_text(slide, 0.3, 5.3, 2.0, 0.2, "слово " * 30, size_pt=24)
    path = deck_with(_fn)
    ids = _ids(run_deterministic(path, PROFILE, CONFIG))
    assert "L04" in ids
    assert "L01" not in ids  # рамка фигуры сама по себе в холсте — это симптом переполнения, не L01


# ---------------------------------------------------------------------------
# L05 — блоки не выровнены по направляющим макета
# ---------------------------------------------------------------------------


def test_L05_catches_a_block_off_the_template_grid(deck_with):
    def _fn(slide):
        _add_text(slide, 1.73, 1.3, 1.5, 0.4, "Смещённый блок", size_pt=14)
    path = deck_with(_fn)
    assert "L05" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# L06 — контент заходит в поля у краёв
# ---------------------------------------------------------------------------


def test_L06_catches_content_inside_the_margin_band(deck_with):
    def _fn(slide):
        _add_text(slide, 0.02, 1.3, 1.0, 0.4, "У самого края", size_pt=14)
    path = deck_with(_fn)
    assert "L06" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# L07 — картинка растянута, пропорции нарушены
# ---------------------------------------------------------------------------


def test_L07_catches_stretched_picture(deck_with, tmp_path):
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (800, 600), color=(50, 60, 70)).save(img_path)

    def _fn(slide):
        slide.shapes.add_picture(str(img_path), Inches(0.3), Inches(1.3), Inches(3.0), Inches(0.6))
    path = deck_with(_fn)
    assert "L07" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# T01 — шрифт не из шаблона / гарнитур больше двух
# ---------------------------------------------------------------------------


def test_T01_catches_a_font_outside_the_template(deck_with):
    path = deck_with(lambda s: _add_text(
        s, 0.3, 1.3, 2.5, 0.6, "Инородный шрифт", size_pt=20, family="Comic Sans MS",
    ))
    assert "T01" in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_T01_catches_more_than_two_font_families_on_one_slide(deck_with):
    def _fn(slide):
        for i, family in enumerate(("Arial", "Georgia", "Verdana")):
            _add_text(slide, 0.3, 1.3 + i * 0.5, 2.5, 0.4, f"Текст {i}", size_pt=14, family=family)
    path = deck_with(_fn)
    assert "T01" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# T02 — кегль не из типографической шкалы шаблона
# ---------------------------------------------------------------------------


def test_T02_catches_an_off_scale_font_size(deck_with):
    """37pt — намеренно между соседними легитимными размерами VK Tech
    (24.0 и 54.0, см. диагностику отчёта задачи), заведомо дальше допуска
    `size_tolerance_pt=0.5`."""
    path = deck_with(lambda s: _add_text(s, 0.3, 1.3, 2.5, 0.7, "Кегль не по шкале", size_pt=37))
    assert "T02" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# T03 — цвет не из палитры шаблона
# ---------------------------------------------------------------------------


def test_T03_catches_color_outside_the_palette(deck_with):
    path = deck_with(lambda s: _add_text(s, 0.3, 1.3, 2.5, 0.6, "Цвет вне палитры", size_pt=20, color_hex="FF00FF"))
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
    assert "T05" in _ids(run_deterministic(path, PROFILE, CONFIG))


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
    assert "D01" in _ids(run_deterministic(path, PROFILE, CONFIG))


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
    assert "D02" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# D03 — таблица больше 7 строк или 5 колонок
# ---------------------------------------------------------------------------


def test_D03_catches_an_oversized_table(blank_deck):
    slide = blank_deck.add_slide()
    header = [f"Кол{i}" for i in range(6)]
    rows = [[str(i * 6 + j) for j in range(6)] for i in range(8)]
    add_table(slide, Box(0.05, 0.1, 0.85, 0.7), TableSpec(header=header, rows=rows), PROFILE)
    path = blank_deck.save_as("table.pptx")
    assert "D03" in _ids(run_deterministic(path, PROFILE, CONFIG))


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
    assert "D04" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# D05 — слайд заполнен меньше четверти или больше трёх четвертей
# ---------------------------------------------------------------------------


def _bullets_deck(n_bullets: int, words_each: int) -> Path:
    items = [" ".join(f"слово{j}" for j in range(words_each)) for _ in range(n_bullets)]
    spec = DeckSpec(
        title="D05", language="ru",
        slides=[SlideSpec(index=0, kind="bullets", headline="Заголовок", blocks=[BulletBlock(items=items)])],
    )
    return build_deck(spec, PROFILE, TEMPLATE, Variant.dense)


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
    path = deck_with(lambda s: _add_text(s, 0.3, 1.3, 3.0, 0.6, "Lorem ipsum dolor sit amet", size_pt=18))
    assert "I02" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# I03 — пустой слайд или слайд с одним заголовком
# ---------------------------------------------------------------------------


def test_I03_catches_a_completely_empty_slide(blank_deck):
    blank_deck.add_slide()
    path = blank_deck.save_as("empty.pptx")
    assert "I03" in _ids(run_deterministic(path, PROFILE, CONFIG))


def test_I03_catches_a_slide_with_only_a_small_text_block(blank_deck):
    """12pt — заведомо ниже заголовочного кегля шаблона (h2 денормирован
    до 19.875pt на VK Tech, см. диагностику отчёта задачи): мелкий
    одинокий текст — забытое содержание, не титульный слайд-разделитель."""
    slide = blank_deck.add_slide()
    _add_text(slide, 0.3, 1.3, 2.0, 0.4, "мелкая подпись", size_pt=12)
    path = blank_deck.save_as("almost_empty.pptx")
    assert "I03" in _ids(run_deterministic(path, PROFILE, CONFIG))


# ---------------------------------------------------------------------------
# I04 — слайд оказался картинкой, а не редактируемыми объектами
# ---------------------------------------------------------------------------


def test_I04_flags_a_slide_that_is_one_big_picture(blank_deck, tmp_path):
    img_path = tmp_path / "shot.png"
    Image.new("RGB", (800, 450), color=(10, 20, 30)).save(img_path)
    slide = blank_deck.add_slide()
    slide.shapes.add_picture(str(img_path), Inches(0.05), Inches(0.05), Inches(9.9), Inches(5.55))
    path = blank_deck.save_as("raster.pptx")
    assert "I04" in _ids(run_deterministic(path, PROFILE, CONFIG))


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
    assert "I05" in _ids(run_deterministic(path, PROFILE, CONFIG))


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
    assert "I06" in _ids(run_deterministic(path, PROFILE, CONFIG))


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
