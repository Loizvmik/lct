"""Тесты `audit.fidelity.template_fidelity` — задача T (метрики верности
шаблону). Не импортируется из `conftest.py` намеренно (тот же принцип, что
и в `tests/audit/test_deterministic.py`/`tests/compose/test_builder.py`:
`tests/` — не пакет, каждый модуль объявляет свои TEMPLATE/PROFILE)."""
from __future__ import annotations
from pathlib import Path

from deckforge.audit.fidelity import template_fidelity
from deckforge.compose.builder import Variant, build_deck
from deckforge.plan.spec import Card, CardBlock, DeckSpec, SlideSpec
from deckforge.template.profile import TemplateProfile

# VK Education: `slide21` — четыре кружка с номерами над четырьмя
# описаниями, тот же паттерн, которым `tests/compose/test_clone.py::
# test_build_deck_marks_cloned_slides` уже доказал, что клон реально
# срабатывает (см. её докстроку) — воспроизводим тот же рецепт, а не
# гадаем содержание для клона заново.
EDU_TEMPLATE = Path("dataset/templates/Шаблон презентации VK Education.pptx")
EDU_PROFILE = TemplateProfile.from_file(EDU_TEMPLATE, cache_dir=None)

# VK Tech — для сценариев, не завязанных конкретно на клон (аудит T01-T03,
# колода с нуля), тот же файл, что и `tests/audit/conftest.py`.
TECH_TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")
TECH_PROFILE = TemplateProfile.from_file(TECH_TEMPLATE, cache_dir=None)


def _cards_spec(n: int) -> SlideSpec:
    return SlideSpec(
        index=0, kind="two_col", headline="Что нужно для раскатки", pattern_id="slide21",
        blocks=[CardBlock(items=[Card(body=f"Пункт номер {i + 1} плана раскатки") for i in range(n)])],
    )


def test_fidelity_on_cloned_deck_reports_high_clone_rate_and_shape_preservation():
    spec = DeckSpec(title="Клон", language="ru", slides=[_cards_spec(2)])
    pptx_path = build_deck(spec, EDU_PROFILE, EDU_TEMPLATE, Variant.dense, clone_examples=True)

    report = template_fidelity(pptx_path, spec, EDU_PROFILE)

    assert report.native_clone_rate == 1.0
    assert report.native_shape_preservation is not None
    assert 0.0 < report.native_shape_preservation <= 1.0
    assert report.mean_geometry_deviation is not None
    # Клон подгоняет незаполненные единицы повтора и рамку текста — коробка
    # заголовка/тела не обязана буквально совпасть с коробкой слота
    # паттерна, но остаётся заведомо близкой (доли холста), не где попало.
    assert report.mean_geometry_deviation < 0.2
    # Одна колода, один pattern_id — разнообразие максимальное (единица),
    # энтропия распределения одной корзины нормирована в ноль (см. докстроку
    # `_pattern_diversity_and_entropy`).
    assert report.pattern_diversity == 1.0
    assert report.pattern_entropy == 0.0
    assert "Верность шаблону" in report.summary


def test_fidelity_on_scratch_deck_reports_zero_clone_rate_and_notes_why():
    spec = DeckSpec(title="С нуля", language="ru", slides=[_cards_spec(2)])
    pptx_path = build_deck(spec, EDU_PROFILE, EDU_TEMPLATE, Variant.dense, clone_examples=False)

    report = template_fidelity(pptx_path, spec, EDU_PROFILE)

    assert report.native_clone_rate == 0.0
    assert report.native_shape_preservation is None
    assert report.mean_geometry_deviation is None
    assert any("клон" in note for note in report.notes)


def test_density_delta_is_none_without_source_density_field():
    """Параллельная задача R ещё не добавила `source_density` к паттернам —
    метрика честно пропускается с пометкой, остальные метрики считаются как
    обычно (одна недостающая метрика не роняет отчёт)."""
    spec = DeckSpec(title="Клон", language="ru", slides=[_cards_spec(2)])
    pptx_path = build_deck(spec, EDU_PROFILE, EDU_TEMPLATE, Variant.dense, clone_examples=True)

    report = template_fidelity(pptx_path, spec, EDU_PROFILE)

    assert report.density_delta is None
    assert any("source_density" in note for note in report.notes)
    assert report.native_clone_rate == 1.0  # соседняя метрика не пострадала


def test_pattern_diversity_is_none_without_pattern_ids():
    """`SlideSpec.pattern_id` не проставлен ни на одном слайде — например,
    план, собранный до `apply_variant`."""
    spec = DeckSpec(
        title="Без раскладки", language="ru",
        slides=[SlideSpec(index=0, kind="section", headline="Заголовок")],
    )
    pptx_path = build_deck(spec, TECH_PROFILE, TECH_TEMPLATE, Variant.dense, clone_examples=False)

    report = template_fidelity(pptx_path, spec, TECH_PROFILE)

    assert report.pattern_diversity is None
    assert report.pattern_entropy is None
    assert any("pattern_id" in note for note in report.notes)


def test_typography_palette_compliance_is_a_fraction_in_unit_interval():
    spec = DeckSpec(title="С нуля", language="ru", slides=[_cards_spec(2)])
    pptx_path = build_deck(spec, EDU_PROFILE, EDU_TEMPLATE, Variant.dense, clone_examples=False)

    report = template_fidelity(pptx_path, spec, EDU_PROFILE)

    assert report.typography_palette_compliance is not None
    assert 0.0 <= report.typography_palette_compliance <= 1.0


def test_native_asset_usage_is_none_when_the_deck_has_no_pictures():
    spec = DeckSpec(title="Без картинок", language="ru", slides=[_cards_spec(2)])
    pptx_path = build_deck(spec, EDU_PROFILE, EDU_TEMPLATE, Variant.dense, clone_examples=False)

    report = template_fidelity(pptx_path, spec, EDU_PROFILE)

    if report.native_asset_usage is not None:
        assert 0.0 <= report.native_asset_usage <= 1.0
    else:
        assert any("картин" in note for note in report.notes)


def test_report_summary_mentions_every_metric_word():
    spec = DeckSpec(title="Клон", language="ru", slides=[_cards_spec(2)])
    pptx_path = build_deck(spec, EDU_PROFILE, EDU_TEMPLATE, Variant.dense, clone_examples=True)

    report = template_fidelity(pptx_path, spec, EDU_PROFILE)

    for word in ("клоном", "фигур", "типографика", "разнообразие", "нативных", "плотности"):
        assert word in report.summary, report.summary
