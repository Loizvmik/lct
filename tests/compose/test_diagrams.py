"""Тесты `compose/diagrams.py` (Task 10) — brief дословно по составу
случаев: все шесть раскладок схем строятся нативными фигурами (не
растром), геометрия карточек — из словаря форм шаблона, пиктограммы —
только из иконочного набора шаблона (VK Education), отказ, когда набора
нет вовсе."""
from __future__ import annotations

import pytest
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_SHAPE_TYPE

from deckforge.compose.diagrams import DIAGRAM_KINDS, DiagramSpec, NoIconSet, add_diagram, add_pictogram_row


def test_every_diagram_kind_builds_from_native_shapes(new_slide, PROFILE, BOX):
    assert set(DIAGRAM_KINDS) == {"process", "cycle", "hierarchy", "funnel", "comparison", "timeline"}
    for kind in DIAGRAM_KINDS:
        shapes = add_diagram(
            new_slide(), BOX, DiagramSpec(kind=kind, items=["Заявка", "Проверка", "Согласование", "Выдача"]), PROFILE,
        )
        assert shapes, kind
        assert all(s.shape_type != MSO_SHAPE_TYPE.PICTURE for s in shapes), kind


def test_diagram_uses_template_shape_geometry(new_slide, PROFILE, BOX):
    """Скругление берётся из шаблона: если в нём только прямоугольники,
    рисовать скруглённые карточки — выход из дизайн-системы."""
    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="process", items=["A", "B"]), PROFILE)
    allowed = {
        MSO_AUTO_SHAPE_TYPE.from_xml(e.prst)
        for e in PROFILE.shape_vocabulary
        if e.prst in ("rect", "roundRect", "ellipse")
    }
    autoshapes = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
    assert autoshapes
    assert all(s.auto_shape_type in allowed for s in autoshapes)


def test_hierarchy_connects_parent_to_every_child(new_slide, PROFILE, BOX):
    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="hierarchy", items=["Итог", "A", "B", "C"]), PROFILE)
    autoshapes = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
    connectors = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.LINE]
    assert len(autoshapes) == 4  # родитель + 3 ребёнка
    assert len(connectors) == 3  # по одному соединителю на ребёнка


def test_funnel_levels_shrink_in_width(new_slide, PROFILE, BOX):
    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="funnel", items=["Показы", "Клики", "Заявки", "Сделки"]), PROFILE)
    widths = [s.width for s in shapes]
    assert widths == sorted(widths, reverse=True)


def test_unknown_diagram_kind_raises(new_slide, PROFILE, BOX):
    with pytest.raises(ValueError):
        add_diagram(new_slide(), BOX, DiagramSpec(kind="bogus", items=["A"]), PROFILE)


def test_pictograms_come_from_the_template_icon_set(new_slide, profile_fixture, BOX):
    profile = profile_fixture("Шаблон презентации VK Education.pptx")
    shapes = add_pictogram_row(new_slide(), BOX, ["рост", "время", "команда"], profile)
    assert len(shapes) == 3
    assert all(s.shape_type == MSO_SHAPE_TYPE.PICTURE for s in shapes)


def test_pictograms_prefer_high_confidence_icons(new_slide, profile_fixture, BOX):
    """Живой замер на VK Tech: первые иконки каталога В ПОРЯДКЕ ОБХОДА
    ПАКЕТА — декоративный мусор (голая буква, почти пустой PNG), а не
    пиктограммы; отбор обязан предпочитать `confidence`, а не порядок
    каталога."""
    profile = profile_fixture("VK Tech шаблон.pptx")
    low_confidence_parts = {
        ref.part_name for ref in profile.assets.icons if ref.confidence < 1.0 or ref.size_bytes < 300
    }
    shapes = add_pictogram_row(new_slide(), BOX, ["a", "b", "c", "d", "e"], profile)
    assert len(shapes) == 5
    used_blobs = {shape.image.blob for shape in shapes}

    # Косвенная проверка: среди пяти вставленных изображений не должно
    # встретиться байт-в-байт то же содержимое, что у заведомо бракованных
    # файлов каталога (маленьких/низкой уверенности).
    import zipfile
    with zipfile.ZipFile(profile.source_path) as pkg:
        broken_blobs = {pkg.read(part) for part in low_confidence_parts}
    assert not (used_blobs & broken_blobs)


def test_pictograms_are_refused_when_template_has_no_icon_set(new_slide, PROFILE, BOX):
    """Иконки либо из шаблона, либо их нет. Эмодзи и чужие наборы не подставляются."""
    profile_without_icons = PROFILE.model_copy(
        update={"assets": PROFILE.assets.model_copy(update={"icons": []})}
    )
    with pytest.raises(NoIconSet):
        add_pictogram_row(new_slide(), BOX, ["рост"], profile_without_icons)
