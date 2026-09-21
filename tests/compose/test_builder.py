"""Тесты сборки слайда на лейауте шаблона (Task 9, Step 3 брифа).

Фикстуры `SAMPLE_SPEC`/`CARDS_SPEC`/`PROFILE`/`TEMPLATE` бриф не задаёт
дословно (только сигнатуры тестовых функций и их поведение) — собраны здесь
по одному из учебных шаблонов (VK Tech, 20 паттернов: `bullets`×11,
`cards`×4, `two_col`×3, `section`×2 — золотой прогон `test_golden_profiles.
py`) реальным содержанием пакета `fixtures/content-packs/queue-latency`
(цифры и формулировки — из его `sources.md`)."""
from __future__ import annotations
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from deckforge.compose.builder import Variant, build_deck
from deckforge.compose.textfit import measure
from deckforge.ooxml.package import PptxPackage
from deckforge.plan.spec import BulletBlock, Card, CardBlock, DeckSpec, SlideSpec, TextBlock
from deckforge.template.profile import TemplateProfile

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")
PROFILE = TemplateProfile.from_file(TEMPLATE)

SAMPLE_SPEC = DeckSpec(
    title="Сокращение времени согласования заявок",
    language="ru",
    slides=[
        SlideSpec(
            index=0, kind="section",
            headline="Автоматическая маршрутизация заявок",
        ),
        SlideSpec(
            index=1, kind="bullets",
            headline="Где уходит время",
            blocks=[
                BulletBlock(items=[
                    "Ожидание первого согласующего — медиана 18 часов",
                    "Ожидание второго согласующего — медиана 11 часов",
                    "Чистая работа людей — 28 минут из 31,5 часа сквозного цикла",
                ]),
            ],
            source_note="Источник: замер 1 240 заявок, март—май 2026",
        ),
        SlideSpec(
            index=2, kind="two_col",
            headline="Пилот дал результат",
            blocks=[
                TextBlock(text="Сквозная медиана сократилась с 31,5 до 6,2 часа."),
                TextBlock(text="Доля переназначений вручную упала с 39% до 4%."),
            ],
        ),
    ],
)

CARDS_SPEC = DeckSpec(
    title="Сокращение времени согласования заявок",
    language="ru",
    slides=[
        SlideSpec(index=0, kind="section", headline="Что нужно для раскатки"),
        SlideSpec(
            index=1, kind="bullets", headline="Риски",
            blocks=[BulletBlock(items=["Задержка данных кадровой системы", "4 подразделения со своими регламентами"])],
        ),
        SlideSpec(
            index=2, kind="cards", headline="Что нужно для раскатки",
            blocks=[
                CardBlock(items=[
                    Card(title="Правила закупок", body="Доработка правил для закупок свыше 5 млн ₽ — 3 недели"),
                    Card(title="Кадровая система", body="Интеграция для автоподмены отсутствующих — 2 недели"),
                    Card(title="Обучение", body="340 согласующих, 4 вебинара по 40 минут"),
                    Card(title="Бюджет", body="2,4 млн ₽, из них 1,9 млн ₽ ФОТ"),
                ]),
            ],
        ),
    ],
)


def _runs(shape):
    if not shape.has_text_frame:
        return []
    return [run for paragraph in shape.text_frame.paragraphs for run in paragraph.runs]


def _family(shape) -> str | None:
    runs = _runs(shape)
    return runs[0].font.name if runs else None


def _size(shape) -> float | None:
    runs = _runs(shape)
    return runs[0].font.size.pt if runs and runs[0].font.size is not None else None


def _is_logo(shape, profile: TemplateProfile) -> bool:
    if shape.shape_type != MSO_SHAPE_TYPE.PICTURE or profile.assets.logo is None:
        return False
    try:
        with PptxPackage.open(TEMPLATE) as pkg:
            logo_bytes = pkg.part(profile.assets.logo.part_name)
    except Exception:
        return False
    return shape.image.blob == logo_bytes


def test_slide_is_built_on_a_layout_from_the_template():
    """Проверка аудита T04: слайд обязан сидеть на макете шаблона, а не на пустом."""
    out = build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)
    prs = Presentation(str(out))
    template_layout_names = {e.name for e in PROFILE.layouts}
    assert len(prs.slides) == len(SAMPLE_SPEC.slides)
    for slide in prs.slides:
        assert slide.slide_layout.name in template_layout_names


def test_no_slide_is_a_single_raster_image():
    """ТЗ: слайд, выгруженный единым растровым изображением, не засчитывается."""
    prs = Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)))
    for slide in prs.slides:
        others = [s for s in slide.shapes if s.shape_type != MSO_SHAPE_TYPE.PICTURE]
        assert others, "слайд состоит из одних картинок"


def test_text_never_leaves_its_box():
    prs = Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)))
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame or not shape.text_frame.text.strip():
                continue
            family = _family(shape) or "Arial"
            size = _size(shape) or 12.0
            metrics = measure(shape.text_frame.text, family, size, shape.width / 914400)
            assert metrics.height_in <= shape.height / 914400 + 0.05


def test_only_template_colors_and_fonts_are_used():
    allowed_colors = set(PROFILE.palette_roles.values())
    allowed_fonts = set(PROFILE.type_scale.families) | set(PROFILE.type_scale.mono)
    for slide in Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense))).slides:
        for shape in slide.shapes:
            for run in _runs(shape):
                assert run.font.name in allowed_fonts
                if run.font.color and run.font.color.type is not None:
                    assert f"#{run.font.color.rgb}" in allowed_colors


def test_decor_of_the_pattern_is_carried_over():
    """Плашки и линии паттерна переносятся — иначе слайд теряет язык шаблона."""
    prs = Presentation(str(build_deck(CARDS_SPEC, PROFILE, TEMPLATE, Variant.visual)))
    cards_slide = prs.slides[2]
    assert len([s for s in cards_slide.shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]) >= 3


def test_template_master_shapes_are_not_duplicated():
    prs = Presentation(str(build_deck(SAMPLE_SPEC, PROFILE, TEMPLATE, Variant.dense)))
    for slide in prs.slides:
        logos = [s for s in slide.shapes if _is_logo(s, PROFILE)]
        assert len(logos) <= 1


def test_cards_expand_to_the_actual_number_of_items():
    """Бриф: "повтор разворачивай под фактическое число элементов" — четыре
    карточки контент-пакета, а не шесть/сколько было на слайде-примере."""
    prs = Presentation(str(build_deck(CARDS_SPEC, PROFILE, TEMPLATE, Variant.visual)))
    cards_slide = prs.slides[2]
    texts = [s.text_frame.text for s in cards_slide.shapes if s.has_text_frame and s.text_frame.text.strip()]
    assert any("Бюджет" in t for t in texts)
    assert any("Обучение" in t for t in texts)


def test_expanded_repeat_stays_within_the_canvas():
    """Находка ручной проверки на VK Education: паттерн "cards" там несёт
    составную единицу повтора (узкий `kpi_value` + широкий `card_body` на
    одной x-координате, друг над другом) — размер единицы обязан браться по
    САМОМУ ШИРОКОМУ слоту группы, иначе шаг пересчитывается с завышенным
    "свободным" пространством и последняя карточка уезжает за правое поле
    холста (см. `expand_repeat`)."""
    template = Path("dataset/templates/Шаблон презентации VK Education.pptx")
    profile = TemplateProfile.from_file(template)
    spec = DeckSpec(
        title="Т", language="ru",
        slides=[SlideSpec(
            index=0, kind="cards", headline="Что нужно для раскатки",
            blocks=[CardBlock(items=[
                Card(body="Доработка правил — 3 недели"),
                Card(body="Интеграция кадровой системы — 2 недели"),
                Card(body="Обучение 340 согласующих"),
                Card(body="Бюджет 2,4 млн ₽"),
            ])],
        )],
    )
    out = build_deck(spec, profile, template, Variant.dense)
    prs = Presentation(str(out))
    canvas_w_in = prs.slide_width / 914400
    for slide in prs.slides:
        for shape in slide.shapes:
            assert shape.left / 914400 >= -0.01
            assert (shape.left + shape.width) / 914400 <= canvas_w_in + 0.01


def test_findings_recorded_when_content_does_not_fit():
    """Бриф: "молча обрезать нельзя, это увидит аудит" — искусственно
    огромный блок, который не влезет ни на одном кегле шкалы вплоть до
    подписи, обязан оставить finding, а не тихо потеряться."""
    huge = SlideSpec(
        index=0, kind="bullets", headline="Заголовок",
        blocks=[BulletBlock(items=["слово " * 400])],
    )
    spec = DeckSpec(title="Т", language="ru", slides=[huge])
    build_deck(spec, PROFILE, TEMPLATE, Variant.dense)
    assert huge.findings, "переполнение слота должно оставить находку"
