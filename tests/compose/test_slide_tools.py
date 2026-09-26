"""Инструменты сборки слайда, которые отдаются агенту (`compose/slide_tools.py`).

Проверяется главное обещание модуля: агент видит последствия своего решения
ДО того, как слайд попал в колоду, и при этом не получает ни одного способа
назвать координату."""
from __future__ import annotations
from dataclasses import replace

from deckforge.compose.slide_tools import list_layouts, try_slide
from deckforge.plan.spec import BulletBlock, SlideSpec

from tests.compose.conftest import TEMPLATES_DIR


def _template(profile):
    return TEMPLATES_DIR / profile.source_name


def test_layout_catalog_tells_what_each_layout_holds(PROFILE):
    rows = list_layouts(PROFILE)

    assert rows, "у разобранного шаблона обязаны быть раскладки"
    for row in rows:
        assert row["layout_id"] and row["kind"]
        assert isinstance(row["holds"], list)


def test_layout_catalog_hides_coordinates_and_colours(PROFILE):
    """Граница слоёв: агент выбирает раскладку по смыслу и вместимости.
    Ни координаты, ни цвета, ни кегли в ответ не попадают — иначе он начнёт
    ими распоряжаться, и гарантия «вёрстку решает код» перестанет
    существовать."""
    forbidden = {"box", "left", "top", "width", "height", "size_pt", "color_hex", "slots", "decor"}

    for row in list_layouts(PROFILE):
        assert not forbidden.intersection(row), f"наружу утекло лишнее: {sorted(forbidden.intersection(row))}"


def test_layout_catalog_can_be_narrowed_to_one_kind(PROFILE):
    all_rows = list_layouts(PROFILE)
    kind = all_rows[0]["kind"]

    narrowed = list_layouts(PROFILE, kind=kind)

    assert narrowed, "сужение по существующему виду не должно давать пустой список"
    assert {row["kind"] for row in narrowed} == {kind}


def test_trying_a_slide_reports_how_full_it_came_out(PROFILE):
    """Ровно тот вопрос, на который модель раньше не могла ответить: слайд
    из одной строки в блоке размером в пол-холста выглядит пустым, и это
    видно только после сборки."""
    layout = list_layouts(PROFILE)[0]
    spec = SlideSpec(
        index=0, kind=layout["kind"], headline="Итоги квартала",
        blocks=[BulletBlock(items=["Одна короткая строка"])],
    )

    verdict = try_slide(spec, layout["layout_id"], PROFILE, _template(PROFILE))

    assert 0 <= verdict["fill_percent"] <= 100
    assert isinstance(verdict["findings"], list)
    assert isinstance(verdict["ok"], bool)


def test_trying_a_slide_leaves_no_trace_in_the_deck(PROFILE):
    """Инструмент вправе быть вызванным сколько угодно раз: он рисует
    черновик в своей копии презентации и в переданный спек ничего не
    дописывает."""
    layout = list_layouts(PROFILE)[0]
    spec = SlideSpec(index=0, kind=layout["kind"], headline="Итоги квартала")
    before = list(spec.findings)

    for _ in range(3):
        try_slide(spec, layout["layout_id"], PROFILE, _template(PROFILE))

    assert spec.findings == before


def test_an_unknown_layout_is_answered_honestly_not_by_an_exception(PROFILE):
    """Модель вправе ошибиться номером. Ошибка обязана вернуться ей текстом,
    который объясняет, что делать, — тем же принципом, что и диспетчер
    инструментов в `plan/writer.py`: неверный вызов не роняет цикл."""
    spec = SlideSpec(index=0, kind="bullets", headline="Итоги квартала")

    verdict = try_slide(spec, "такой-раскладки-нет", PROFILE, _template(PROFILE))

    assert verdict["ok"] is False
    assert any("нет" in note for note in verdict["notes"])


def test_trying_a_slide_surfaces_what_the_code_did_to_the_text(PROFILE):
    """Усечения и невлезший текст раньше оставались внутри сборки — агент о
    них не узнавал и потому не мог написать короче."""
    layout = next((row for row in list_layouts(PROFILE) if "bullets" in row["holds"]), None)
    if layout is None:
        return
    long_item = "Очень длинный пункт, который заведомо не помещается в свою рамку " * 12
    spec = SlideSpec(
        index=0, kind=layout["kind"], headline="Итоги квартала",
        blocks=[BulletBlock(items=[long_item])],
    )

    verdict = try_slide(spec, layout["layout_id"], PROFILE, _template(PROFILE))

    assert verdict["findings"] or verdict["notes"], "про невлезающий текст обязано быть сказано"


def test_layout_catalog_gives_a_target_not_only_a_limit(PROFILE):
    """Предел без цели читается как «чем короче, тем безопаснее». Цель —
    0,8 предела, округлённая."""
    for row in list_layouts(PROFILE):
        cap = row["max_chars_per_item"]
        assert row["target_chars_per_item"] == (round(cap * 0.8) if cap else None)
        for place in row["places"]:
            if place["max_chars"]:
                assert place["target_chars"] == round(place["max_chars"] * 0.8)
            if place["max_words"]:
                assert place["target_words"] == round(place["max_words"] * 0.8)
