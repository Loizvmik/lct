"""Тесты `compose/tables.py` (Task 10) — brief дословно по составу
случаев: кегль ужимается циклом, но не ниже пола; высоты строк — честный
замер, а не равное деление; цвет текста шапки выбирается по контрасту;
таблица строится и без стиля таблицы в самом файле (VK Tech)."""
from __future__ import annotations

from deckforge.compose.tables import FONT_FLOOR_RATIO, TableSpec, add_table
from deckforge.template.naming import contrast_ratio


def _cell_sizes(table) -> list[float]:
    sizes = []
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.font.size is not None:
                        sizes.append(run.font.size.pt)
    return sizes


def _header_text_color(table) -> str:
    run = table.cell(0, 0).text_frame.paragraphs[0].runs[0]
    return f"#{str(run.font.color.rgb)}"


def _contrast(hex_a: str, hex_b: str) -> float:
    return contrast_ratio(hex_a, hex_b)


def test_table_shrinks_font_until_it_fits_but_not_below_floor(new_slide, PROFILE, SMALL_BOX):
    frame = add_table(
        new_slide(), SMALL_BOX,
        TableSpec(
            header=["Метрика", "Было", "Стало"],
            rows=[["Медианное время ожидания заявки в очереди", "4 ч", "1 ч"]] * 6,
        ),
        PROFILE,
    )
    sizes = _cell_sizes(frame.table)
    assert sizes
    assert min(sizes) >= PROFILE.type_scale.steps["caption"] * FONT_FLOOR_RATIO


def test_row_heights_are_measured_not_split_evenly(new_slide, PROFILE, BOX):
    """Высота строки в .pptx — минимум, а не размер: редактор растит строку
    под текст, но не ужимает, и таблица тихо уезжает на подвал."""
    frame = add_table(
        new_slide(), BOX,
        TableSpec(header=["A", "B"], rows=[["коротко", "коротко"], ["очень длинный текст " * 6, "x"]]),
        PROFILE,
    )
    heights = [row.height for row in frame.table.rows]
    assert heights[2] > heights[1]


def test_header_text_color_is_chosen_by_contrast(new_slide, PROFILE, BOX):
    frame = add_table(new_slide(), BOX, TableSpec(header=["A", "B"], rows=[["1", "2"]]), PROFILE)
    header_fill = PROFILE.palette_roles.get("accent") or PROFILE.palette_roles.get("brand")
    assert _contrast(_header_text_color(frame.table), header_fill) >= 4.5


def test_table_without_template_table_style_still_builds(new_slide, profile_fixture, BOX):
    """У VK Tech tableStyles.xml отсутствует вовсе."""
    profile = profile_fixture("VK Tech шаблон.pptx")
    frame = add_table(new_slide(), BOX, TableSpec(header=["A", "B"], rows=[["1", "2"]]), profile)
    assert frame.has_table


def test_column_alignment_is_applied(new_slide, PROFILE, BOX):
    frame = add_table(
        new_slide(), BOX,
        TableSpec(header=["Метрика", "Значение"], rows=[["Строк", "10"]], align=["l", "r"]),
        PROFILE,
    )
    from pptx.enum.text import PP_ALIGN
    assert frame.table.cell(0, 1).text_frame.paragraphs[0].alignment == PP_ALIGN.RIGHT
