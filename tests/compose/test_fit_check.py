"""Тесты `compose.fit_check` — инструмент №1 агентного цикла `plan.writer`
(Task 19): реальный замер, через `compose.textfit.measure` (единственный
замер текста в проекте), не отдельная эвристика.

`PROFILE` — сессионная фикстура `tests/compose/conftest.py`, тот же реальный
разбор `dataset/templates/VK Tech шаблон.pptx`, что и у соседних тестов
`compose/` (графики/таблицы/схемы читают профиль целиком, синтетика здесь
не годится по той же причине, что и там)."""
from __future__ import annotations

from deckforge.compose.fit_check import measure_fit, slot_fit_geometry


def test_short_headline_fits_a_real_pattern_slot(PROFILE):
    geometry = slot_fit_geometry(PROFILE, "bullets")
    assert "headline" in geometry, "у bullets этого шаблона обязан быть headline-слот"
    result = measure_fit("Коротко", "headline", PROFILE, "bullets")
    assert result["fits"] is True
    assert result["overflow_in"] == 0.0


def test_absurdly_long_text_overflows_the_slot(PROFILE):
    long_text = "Очень длинный текст, который совершенно точно не влезет в рамку слота. " * 40
    result = measure_fit(long_text, "headline", PROFILE, "bullets")
    assert result["fits"] is False
    assert result["overflow_in"] > 0.0
    assert result["lines"] > 1


def test_role_absent_in_kind_degrades_honestly_instead_of_failing(PROFILE):
    """`kpi_value` не существует ни в одной раскладке `bullets` этого
    шаблона (см. kinds/roles в докстроке модуля) — инструмент обязан
    честно деградировать (`fits=True` с пометкой), а не упасть и не соврать
    "не влезает" на пустом месте."""
    result = measure_fit("что угодно", "kpi_value", PROFILE, "bullets")
    assert result["fits"] is True
    assert "note" in result


def test_geometry_uses_the_richest_slot_per_role_not_an_arbitrary_one(PROFILE):
    """Тот же принцип максимума, что `plan.writer._kind_capacity` уже
    применяет к `Capacity` — геометрия должна прийти от слота с НАИБОЛЬШИМ
    `max_chars` среди раскладок `kind`, не от первого попавшегося."""
    candidates = [
        slot
        for pattern in PROFILE.patterns if pattern.kind == "cards"
        for slot in pattern.slots if slot.role == "card_body" and slot.max_chars > 0
    ]
    assert candidates, "нужен хотя бы один card_body-слот с max_chars>0, иначе тест ничего не проверяет"
    richest = max(candidates, key=lambda s: s.max_chars)

    geometry = slot_fit_geometry(PROFILE, "cards")
    geo = geometry["card_body"]
    canvas_width_in = PROFILE.canvas_width_emu / 914400.0
    assert abs(geo["width_in"] - richest.box.width * canvas_width_in) < 1e-9
    assert geo["size_pt"] == richest.size_pt


def test_measure_fit_on_a_profile_free_kind_degrades_without_raising():
    """Без профиля (`None`) — та же честная деградация, что и другие
    инструменты/подсказки модели в проекте без данных под рукой (не
    исключение)."""
    result = measure_fit("текст", "headline", None, "bullets")
    assert result["fits"] is True
    assert "note" in result


def test_measure_fit_reports_how_full_the_slot_is(PROFILE):
    """«Влезает» — половина ответа: короткий текст в большой рамке влезает,
    но оставляет её пустой. `fill` растёт с длиной текста и не выходит за 1."""
    short = measure_fit("Коротко", "card_body", PROFILE, "cards")
    longer = measure_fit("Коротко и по делу, с цифрой и сроком. " * 3, "card_body", PROFILE, "cards")
    huge = measure_fit("Очень длинный текст. " * 200, "card_body", PROFILE, "cards")

    assert 0.0 < short["fill"] < longer["fill"] <= 1.0
    assert huge["fill"] == 1.0
    assert measure_fit("", "card_body", PROFILE, "cards")["fill"] == 0.0


def test_main_slot_fill_measures_the_biggest_text_place_of_the_chosen_layout(PROFILE):
    from deckforge.compose.fit_check import main_slot_fill
    from deckforge.plan.spec import Card, CardBlock, SlideSpec

    # Карточная раскладка с местом `card_body`: у двумерных сеток VK Tech
    # (задача V2) текст карточки снят ролью `body`.
    layout = next(p for p in PROFILE.patterns if p.kind == "cards" and any(s.role == "card_body" for s in p.slots))
    spec = SlideSpec(
        index=1, kind="cards", headline="Итог",
        blocks=[CardBlock(items=[Card(title="А", body="Коротко"), Card(title="Б", body="Чуть длиннее тело")])],
    )

    result = main_slot_fill(spec, PROFILE, layout_id=layout.pattern_id)

    assert result is not None
    assert result["layout_id"] == layout.pattern_id
    assert result["role"] == "card_body"
    assert result["chars"] == len("Чуть длиннее тело")
    assert result["target_chars"] == round(result["max_chars"] * 0.8)
    assert 0.0 < result["fill"] < result["target_fill"]


def test_main_slot_fill_is_silent_without_main_text(PROFILE):
    from deckforge.compose.fit_check import main_slot_fill
    from deckforge.plan.spec import SlideSpec

    assert main_slot_fill(SlideSpec(index=0, kind="section", headline="Обложка"), PROFILE) is None
