"""Задача V1: правила оформления диаграмм, образцы графиков и класс
слайда-примера снимаются с шаблона детерминированно."""
from __future__ import annotations
import functools
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from deckforge.ooxml.geometry import Box
from deckforge.template.chart_rules import ChartRules, parse_chart_rules
from deckforge.template.patterns import CHART_TIER_NATIVE, chart_target_slot, slide_mentions_chart
from deckforge.template.profile import TemplateProfile
from deckforge.template.prototypes import image_palette, slide_class_of

TEMPLATES = Path("dataset/templates")


@functools.lru_cache(maxsize=None)
def _profile(name: str) -> TemplateProfile:
    return TemplateProfile.from_file(TEMPLATES / name, cache_dir=None)


def test_rules_text_of_vk_education_is_read_literally():
    rules = parse_chart_rules(
        "Перекрытие рядов = ±50%.Боковой зазор = 0%. Избавляемся от линий сетки, засечек. "
        "В примере — метки данных вместо вертикальной оси.",
    )
    assert rules == ChartRules(overlap=-50, gap_width=0, no_gridlines=True, data_labels=True)
    assert parse_chart_rules("Перекрытие рядов 20 %").overlap == 20
    assert parse_chart_rules("Просто текст про графики").empty


def test_chart_words_skip_gantt_and_infographics():
    assert slide_mentions_chart("Пример оформления графика")
    assert slide_mentions_chart("Гистограмма и диаграмма с областями")
    assert not slide_mentions_chart("Диаграмма Ганта")
    assert not slide_mentions_chart("Инфографика")


def test_vk_education_rules_and_chart_samples_are_in_the_profile():
    profile = _profile("Шаблон презентации VK Education.pptx")
    rules = profile.chart_rules
    assert (rules.overlap, rules.gap_width, rules.no_gridlines, rules.source_slide) == (-50, 0, True, 51)
    linked = {p.source_slide: p for p in profile.chart_prototypes if p.pattern_id}
    assert {47, 48, 49, 50} <= set(linked)
    assert all(not p.gridlines and p.show_values for p in linked.values())
    assert linked[47].chart_type == "bar"
    assert "#0" in linked[47].palette[0], "первый цвет образца: синий VK"
    by_id = {p.pattern_id: p for p in profile.patterns}
    for n in (47, 48, 49, 50):
        pattern = by_id[linked[n].pattern_id]
        assert pattern.slide_class == "visual_prototype"
        assert any(s.chart_frame for s in pattern.slots if s.role == "image")


def test_workspace_chart_pictures_are_prototypes_without_a_layout():
    """WorkSpace рисует графики картинками на слайдах 20-21, в раскладки
    они не попали: образец есть, раскладки у него нет, стиль берётся
    для графика на другой раскладке."""
    profile = _profile("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")
    samples = {p.source_slide: p for p in profile.chart_prototypes}
    assert {20, 21} <= set(samples)
    assert all(p.pattern_id is None for p in samples.values())
    assert len(samples[20].palette) >= 2


def test_native_chart_of_lct_is_a_native_chart_place():
    profile = _profile("ЛЦТ2026 Шаблон презентации.pptx")
    assert profile.chart_prototypes == []
    slide21 = next(p for p in profile.patterns if p.pattern_id == "slide21")
    _slot, tier = chart_target_slot(slide21.slots)
    assert tier == CHART_TIER_NATIVE


def _png(colors: list[tuple[int, int, int]], transparent: bool) -> bytes:
    img = Image.new("RGBA", (120, 60), (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    for i, color in enumerate(colors):
        for x in range(10 + i * 40, 30 + i * 40):
            for y in range(10, 60):
                img.putpixel((x, y), (*color, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_palette_is_taken_from_the_bars_not_from_the_background():
    """Прозрачный фон PNG не темнит палитру: у VK Education слайд 48 без
    подложки выходил бордовым вместо розового."""
    palette = image_palette(_png([(0, 119, 255), (255, 56, 133)], transparent=True))
    assert len(palette) == 2

    def near(hex_color: str, rgb: tuple[int, int, int]) -> bool:
        got = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return max(abs(a - b) for a, b in zip(got, rgb)) <= 8

    assert any(near(c, (0, 119, 255)) for c in palette) and any(near(c, (255, 56, 133)) for c in palette)
    assert image_palette(_png([], transparent=False)) == []


def _pattern(headline: str, *, pictures: int = 0, texts=(), source: int = 10):
    slots = [SimpleNamespace(role="headline", sample_text=headline, chart_frame=False, box=Box(0, 0, 1, 0.1))]
    slots += [SimpleNamespace(role="body", sample_text=t, chart_frame=False, box=Box(0, 0.2, 1, 0.1)) for t in texts]
    decor = [SimpleNamespace(kind="picture") for _ in range(pictures)]
    return SimpleNamespace(pattern_id="p", source_slide_index=[source], slots=slots, decor=decor)


def test_slide_classes_need_a_strong_signal():
    """Рыба «Оцените высокий уровень защищённости» (VK Tech) и карточки с
    иконкой в каждой остаются раскладками содержания."""
    assert slide_class_of(_pattern("Безопасность", texts=["Оцените высокий уровень защищенности"] * 4), None,
                          set()) == "content_pattern"
    assert slide_class_of(_pattern("Пункт", pictures=9), None, set()) == "content_pattern"
    assert slide_class_of(_pattern("Объёмные иконки сервисов", pictures=9), None, set()) == "asset_sheet"
    assert slide_class_of(_pattern("Как пользоваться шаблоном"), None, set()) == "instruction"
    assert slide_class_of(_pattern("Графики", source=51), ChartRules(no_gridlines=True, source_slide=51),
                          set()) == "style_guide"
    assert slide_class_of(_pattern("Пример оформления графика", source=48), None, {48}) == "visual_prototype"
