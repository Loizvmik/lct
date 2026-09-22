"""Регрессия на находку аудита T02 (Task 10 отчёт, задача 10a): `compose/
charts.py`, `tables.py` и `diagrams.py` клали кегль типографической шкалы
шаблона (`profile.type_scale.steps`) на слайд БЕЗ денормировки к реальному
холсту шаблона (`Canvas.norm`/`TemplateProfile.canvas_norm`) — `builder.py`
уже это делал (`_shrink_sequence`), три остальных модуля брали шкалу как
есть. На шаблоне со стандартным холстом 13.333″ множитель равен единице, и
баг не проявлялся; на VK Tech (холст 10″, множитель 1.333) кегли текста
графиков/таблиц/схем выходили завышенными примерно на треть — сорок находок
T02 на одной демонстрационной колоде.

Параметризовано по всем трём учебным шаблонам — тот же принцип, что и у
остальных regression-тестов `tests/compose/` (находка №3 код-ревью Task 10:
раньше всё гонялось на одном VK Tech, где случайно не проявлялась ни одна
из более ранних находок)."""
from __future__ import annotations

import pytest

from deckforge.compose.charts import ChartSpec, Series, add_chart
from deckforge.compose.diagrams import DiagramSpec, add_diagram
from deckforge.compose.tables import TableSpec, add_table
from deckforge.ooxml.geometry import Box

# Дубликат `TEMPLATE_NAMES` из `tests/compose/conftest.py` — намеренно, не
# импорт (см. докстроку conftest.py про независимость тестовых модулей).
_TEMPLATE_NAMES = [
    "VK Tech шаблон.pptx",
    "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
    "Шаблон презентации VK Education.pptx",
]

_STEP_NAMES = ("micro", "caption", "body", "h2", "h1", "display")

# То же допустимое отклонение, что и у самой проверки аудита T02
# (config/audit.yaml, `template.size_tolerance_pt`) — не откалибровано
# отдельно под тесты, честное повторное использование одного и того же
# порога "это одна и та же ступень шкалы с точностью до округления".
_TOL = 0.5


def _allowed_sizes(profile) -> set[float]:
    """Денормированные ступени `type_scale.steps` этого профиля — то, что
    `TemplateProfile.type_scale_pt` обязан вернуть для КАЖДОЙ из шести
    именованных ступеней на РЕАЛЬНОМ холсте шаблона."""
    return {profile.type_scale_pt(name) for name in _STEP_NAMES}


def _assert_on_scale(size_pt: float, allowed: set[float], template_name: str, where: str) -> None:
    assert any(abs(size_pt - a) <= _TOL for a in allowed), (
        template_name, where, size_pt, sorted(allowed),
    )


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_chart_text_size_is_on_template_scale(new_slide, profile_fixture, BOX, template_name):
    """`_style_chart` кладёт `type_scale.steps["caption"]` на `chart.font`
    (наследуется подписями делений) — денормированным к холсту профиля."""
    profile = profile_fixture(template_name)
    frame = add_chart(
        new_slide(), BOX,
        ChartSpec(
            kind="bar", categories=["A", "B"], series=[Series("s", [1, 2])],
            axis_titles=("Категория", "Значение"),
        ),
        profile,
    )
    allowed = _allowed_sizes(profile)
    _assert_on_scale(frame.chart.font.size.pt, allowed, template_name, "chart.font")
    for axis in (frame.chart.category_axis, frame.chart.value_axis):
        _assert_on_scale(axis.tick_labels.font.size.pt, allowed, template_name, "axis.tick_labels")


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_table_text_size_is_on_template_scale(new_slide, profile_fixture, template_name):
    """`_fit_font_size` начинает с `type_scale.steps["body"]` и ужимает
    циклом вниз только если контент не влезает — большая рамка и короткий
    текст держат кегль ровно на ступени "body" без единого шага ужимания
    (иначе промежуточные значения цикла, `base * 0.9**k`, сами по себе не
    обязаны совпасть ни с одной ИМЕНОВАННОЙ ступенью шкалы, и тест проверял
    бы не то)."""
    profile = profile_fixture(template_name)
    box = Box(left=0.05, top=0.05, width=0.9, height=0.9)
    frame = add_table(
        new_slide(), box,
        TableSpec(header=["A", "B"], rows=[["1", "2"]]),
        profile,
    )
    allowed = _allowed_sizes(profile)
    checked = 0
    for row in frame.table.rows:
        for cell in row.cells:
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.font.size is None:
                        continue
                    _assert_on_scale(run.font.size.pt, allowed, template_name, "table cell")
                    checked += 1
    assert checked > 0, template_name


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_diagram_card_label_size_is_on_template_scale(new_slide, profile_fixture, BOX, template_name):
    """`_fit_label_size` перебирает `_LABEL_STEPS` ("body"/"caption"/
    "micro") и возвращает первую ступень, что помещается — короткая подпись
    в просторной карточке ("process", `BOX` целиком) помещается на первой же
    ступени, кегль карточки — ровно денормированная ступень шкалы."""
    profile = profile_fixture(template_name)
    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="process", items=["Раз", "Два"]), profile)
    allowed = _allowed_sizes(profile)
    checked = 0
    for shape in shapes:
        if not shape.has_text_frame:
            continue
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                if run.font.size is None:
                    continue
                _assert_on_scale(run.font.size.pt, allowed, template_name, "diagram card label")
                checked += 1
    assert checked > 0, template_name
