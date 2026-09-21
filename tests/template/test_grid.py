"""Сетка шаблона (поля/колонки/якоря) — тесты дословно из брифа Task 4
(Step 3), .superpowers/sdd/task-4-brief.md.
"""


def test_margins_match_measured_values(profile_fixture):
    """Числа замерены разведкой; допуск 0.5 п.п."""
    cases = [("VK Tech шаблон.pptx", 0.0463), ("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx", 0.0351),
             ("Шаблон презентации VK Education.pptx", 0.0540)]
    for name, expected in cases:
        grid = profile_fixture(name).grid
        assert abs(grid.margin_left - expected) < 0.005, name


def test_education_margins_are_symmetric(profile_fixture):
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert abs(grid.margin_left - grid.margin_right) < 0.002


def test_education_has_exact_half_split(profile_fixture):
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert any(abs(axis - 0.5) < 0.003 for axis in grid.columns)


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
    """p:guideLst отсутствует во всех трёх учебных Google-экспортах (брифом,
    п.9) — сетка обязана быть восстановлена кластеризацией, не направляющими."""
    for name in ("VK Tech шаблон.pptx", "Шаблон презентации VK Education.pptx"):
        assert profile_fixture(name).grid.native_guides_used is False


def test_vktech_column_verticals_are_found(profile_fixture):
    """VK Tech: карточные раскладки в 3-4 колонки дают вертикали на 63.61%
    и 81.56% (брифом, п.13)."""
    grid = profile_fixture("VK Tech шаблон.pptx").grid
    assert any(abs(axis - 0.6361) < 0.006 for axis in grid.columns)
    assert any(abs(axis - 0.8156) < 0.006 for axis in grid.columns)


def test_education_body_anchor_matches_measured_value(profile_fixture):
    """Верх основного текста у Education — 25.86% (брифом, п.15)."""
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    assert abs(grid.anchors["body_top"] - 0.2586) < 0.008


def test_skipped_shapes_without_box_are_counted(profile_fixture):
    """Шейпы без координат не годятся для сетки и пропускаются, но их число
    обязано попасть в результат, а не потеряться молча."""
    grid = profile_fixture("VK Tech шаблон.pptx").grid
    assert grid.skipped_no_box >= 0
    assert isinstance(grid.skipped_no_box, int)


def test_every_reported_number_carries_a_confidence_score(profile_fixture):
    """«Каждое число... должно нести меру уверенности» — margin_left/right/
    top/bottom, columns, gutter и baseline обязаны иметь запись в confidence."""
    grid = profile_fixture("Шаблон презентации VK Education.pptx").grid
    for key in ("margin_left", "margin_right", "margin_top", "margin_bottom",
                "columns", "gutter", "baseline"):
        assert key in grid.confidence
        assert 0.0 <= grid.confidence[key] <= 1.0
