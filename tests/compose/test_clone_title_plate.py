"""Заголовок клона на плашке-«таблетке» (ЛЦТ2026).

У ЛЦТ2026 на каждом слайде содержания заголовок стоит на цветной плашке
шириной в четверть слайда, а рамка плейсхолдера заголовка идёт на
восемь десятых ширины. Аудит мерит рамку текста целиком по ширине, и
плашка, закрытая рамкой наполовину, давала L02 (57% площади) на каждом
клоне: живой прогон 27 сентября 2026 собрал 9 слайдов из 14 с нуля.
Клон подгоняет пару: рамка заголовка сужается до ширины текста, плашка
растёт под неё. Модель не вызывается.
"""
from __future__ import annotations
from pathlib import Path

import pytest
from pptx import Presentation

from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import audit_slide_layout
from deckforge.compose import builder
from deckforge.compose.blocks import Paragraph
from deckforge.compose.clone import bind_text, clone_example_slide, match_slots, sample_slides_by_number, slide_refs
from deckforge.ooxml.geometry import Canvas

TEMPLATE = Path("dataset/templates/ЛЦТ2026 Шаблон презентации.pptx")


@pytest.fixture(scope="module")
def profile(profile_fixture):
    return profile_fixture(TEMPLATE.name)


def _clone_with_headline(profile, pattern_id: str, headline: str):
    prs = Presentation(str(TEMPLATE))
    sources = sample_slides_by_number(prs)
    builder._clear_sample_slides(prs)
    pattern = next(builder._pattern_from_model(m) for m in profile.patterns if m.pattern_id == pattern_id)
    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)
    slide = clone_example_slide(prs, sources[pattern.source_slide_index[0]], builder._find_layout(prs, pattern.layout_id))
    matched = match_slots(slide, pattern.slots, canvas)
    ref = matched[next(i for i, s in enumerate(pattern.slots) if s.role == "headline")]
    bind_text(ref.element, [Paragraph(headline)])
    return slide, ref, canvas


def _plate_title_overlaps(slide, canvas, profile) -> list:
    return [
        f for f in audit_slide_layout(slide, canvas, profile, AuditConfig.load())
        if f.check_id == "L02" and "Заголовок" in f.message and "Скругленный" in f.message
    ]


@pytest.mark.parametrize("headline", ["Риски", "Пилот сократил время согласования в 5 раз"])
def test_title_plate_wraps_cloned_headline(profile, headline):
    slide, ref, canvas = _clone_with_headline(profile, "slide24", headline)
    assert _plate_title_overlaps(slide, canvas, profile), "без подгонки плашка и рамка накладываются: тест не о чем"

    builder._fit_title_plate(slide, ref, [ref.element], canvas)

    assert not _plate_title_overlaps(slide, canvas, profile)
    title = next(r for r in slide_refs(slide, canvas) if r.element is ref.element)
    plate = next(r for r in slide_refs(slide, canvas) if r.name.startswith("Скругленный") and r.box.top < title.box.top)
    assert plate.box.left <= title.box.left and plate.box.right >= title.box.right


def test_two_line_headline_grows_plate_down(profile):
    """Логотипы справа сужают рамку заголовка, длинный заголовок встаёт в
    две строки: плашка обязана вырасти по высоте, а не оставить вторую
    строку висеть над собой."""
    slide, ref, canvas = _clone_with_headline(profile, "slide24", "Главная проблема процесса — ожидание согласующих")
    ref = builder._shrink_frame_away_from_decor(slide, ref, [ref.element], canvas)
    plate_before = next(r for r in slide_refs(slide, canvas) if r.name.startswith("Скругленный") and r.box.top < ref.box.top)

    builder._fit_title_plate(slide, ref, [ref.element], canvas)

    title = next(r for r in slide_refs(slide, canvas) if r.element is ref.element)
    plate = next(r for r in slide_refs(slide, canvas) if r.name.startswith("Скругленный") and r.box.top < title.box.top)
    assert title.box.height > ref.box.height, "две строки не влезают в рамку в одну строку высотой"
    assert plate.box.height > plate_before.box.height
    assert plate.box.bottom >= title.box.bottom
    assert not _plate_title_overlaps(slide, canvas, profile)


def test_inherited_white_text_on_light_example_is_recolored(profile):
    """Пример 15 «Пункты»: плейсхолдеры наследуют белый текст мастера, фон
    слайда светлый. Клон обязан перекрасить наш текст, раз своего цвета у
    run нет."""
    from deckforge.audit.config import AuditConfig as _Cfg
    from deckforge.compose.clone import inherited_text_color

    prs = Presentation(str(TEMPLATE))
    sources = sample_slides_by_number(prs)
    builder._clear_sample_slides(prs)
    pattern = next(builder._pattern_from_model(m) for m in profile.patterns if m.pattern_id == "slide15")
    canvas = Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)
    slide = clone_example_slide(prs, sources[15], builder._find_layout(prs, pattern.layout_id))
    matched = match_slots(slide, pattern.slots, canvas)
    ref = next(matched[i] for i, s in enumerate(pattern.slots) if s.role != "headline" and matched[i] is not None)
    bind_text(ref.element, [Paragraph("Бюджет 2,4 млн рублей")])
    scheme, clr_map = profile.theme.scheme, profile.theme.clr_map
    bg = builder._clone_background_luminance(slide, ref, profile, canvas)
    before = inherited_text_color(slide, ref.element, scheme, clr_map)
    assert builder._contrast_ratio_from_luminance(bg, builder._relative_luminance(before)) < 3, "тест не о чем"

    builder._fix_cloned_contrast(slide, ref, profile, canvas, _Cfg.load())

    after = inherited_text_color(slide, ref.element, scheme, clr_map)
    assert builder._contrast_ratio_from_luminance(bg, builder._relative_luminance(after)) >= 3
