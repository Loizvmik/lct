"""Задача T3: показатели, таблица, цитата и команда у шаблона без раскладки
их вида. Планировщик не ослабляет форму до карточек, а отдаёт
раскладку-носитель с пометкой `scratch_form`; контракт просит у писателя
саму форму; стиль назначает показатели, таблицу сравнения и цитату по
источникам."""
from __future__ import annotations

from deckforge.pattern.candidates import SCRATCH_FORMS, candidates_for, cover_pattern_id
from deckforge.pattern.forms import forms_of
from deckforge.pattern.intent import SlideIntent, intents_from_outline
from deckforge.pattern.planner import plan_patterns
from deckforge.pattern.shape import shape_for_style
from deckforge.plan.contracts import build_contract, contract_problems
from deckforge.plan.data_types import find_quotes, visual_intents
from deckforge.plan.outline import Outline, OutlineSlide, SourceDoc
from deckforge.plan.spec import SlideSpec, TeamBlock, TeamMember

from .conftest import bullets, cards, headline, pattern, profile, section, slot

_SOURCES = """## Сравнение: ручная маршрутизация и автоматическая

| Критерий | Ручная | Автоматическая |
|---|---|---|
| Кто назначает согласующего | автор заявки | правило по типу и сумме |
| Подмена отсутствующего | вручную, по звонку | из кадровой системы |
| Время назначения | до 4 часов | 2 секунды |

## План внедрения

| Квартал | Что делаем | Результат |
|---|---|---|
| III квартал 2026 | доработка правил | правила покрывают 92% заявок |
| IV квартал 2026 | интеграция с кадровой системой | автоподмена |

## Цитата заказчика

«Впервые заявка проходит быстрее, чем я успеваю о ней вспомнить.
Автоматическая маршрутизация сняла главный источник жалоб авторов.»
Сергей Лебедев, директор по операциям.
"""


def _no_forms_profile():
    """Шаблон без показателей, таблиц и цитат: обложка, разделитель, финал,
    карточки и два списка (носители)."""
    return profile(
        section("cover", source=1, text=True),
        section("divider", source=5),
        pattern("thanks", "section", [headline(sample_text="Спасибо за внимание!")], source=60),
        cards("cards3", 3, source=11), cards("cards4", 4, source=12),
        bullets("list_a", source=20), bullets("list_b", source=21, kind="two_col"),
    )


def _deck(form: str, **kw) -> list[SlideIntent]:
    return [
        SlideIntent(index=0, outline_kind="title", intent="Обложка"),
        SlideIntent(index=1, outline_kind="data", intent="Показатели", items=3, form=form, **kw),
        SlideIntent(index=2, outline_kind="closing", intent="Спасибо"),
    ]


def test_every_scratch_form_gets_a_text_carrier_instead_of_cards():
    prof = _no_forms_profile()
    for form in ("kpi", "table", "quote", "team"):
        plan = plan_patterns(_deck(form), prof, "dense")
        middle = plan[1]
        assert middle.scratch_form == form, form
        assert middle.pattern_id in ("list_a", "list_b", "divider"), (form, middle.pattern_id)
        assert middle.relaxed == ()
        # Обложка и финал носителями не бывают.
        assert middle.pattern_id not in ("cover", "thanks")
        assert all(alt not in ("cover", "thanks") for alt in middle.alternatives)


def test_native_layout_wins_over_scratch():
    kpi = pattern(
        "kpi3", "kpi", [headline()] + [slot("kpi_value", max_chars=8), slot("kpi_label", max_chars=40)] * 3,
        repeat=3, repeat_roles=("kpi_value", "kpi_label"), source=30,
    )
    prof = profile(*_no_forms_profile().patterns, kpi)
    plan = plan_patterns(_deck("kpi"), prof, "dense")
    assert plan[1].pattern_id == "kpi3"
    assert plan[1].scratch_form is None


def test_scratch_form_never_sits_on_cover_or_closing_when_nothing_else_is_there():
    prof = profile(
        section("cover", source=1, text=True),
        pattern("thanks", "section", [headline(sample_text="Спасибо за внимание!")], source=60),
        cards("cards3", 3, source=11),
    )
    forms = forms_of(prof)
    found = candidates_for(
        _deck("quote")[1], prof, forms, position=1, last=2, cover_id=cover_pattern_id(prof),
        closing_ids=frozenset({"thanks"}),
    )
    assert "thanks" not in found.pattern_ids
    assert "cover" not in found.pattern_ids


def test_team_outline_point_gets_the_team_form():
    outline = Outline(slides=[OutlineSlide(kind="team", intent="Команда", needs=["Анна", "Илья"])], title="T")
    assert intents_from_outline(outline)[0].form == "team"
    assert "team" in SCRATCH_FORMS


def test_contract_asks_the_writer_for_the_form_itself():
    prof = _no_forms_profile()
    for form, block in (("kpi", "kpi"), ("quote", "quote"), ("team", "team")):
        assignment = plan_patterns(_deck(form), prof, "dense")[1]
        contract = build_contract(assignment, prof, "dense")
        assert contract.scratch_form == form
        assert [s.block for s in contract.slots] == [block]
    kpi = build_contract(plan_patterns(_deck("kpi"), prof, "dense")[1], prof, "dense")
    assert 2 <= kpi.slots[0].count <= 4
    table = build_contract(plan_patterns(_deck("table"), prof, "dense")[1], prof, "dense")
    assert table.slots == () and table.required_visual == "table"


def test_team_count_is_an_upper_bound_not_a_quota():
    prof = _no_forms_profile()
    contract = build_contract(plan_patterns(_deck("team"), prof, "dense")[1], prof, "dense")
    two = SlideSpec(index=1, kind="bullets", headline="Команда пилота из двух человек", blocks=[
        TeamBlock(items=[TeamMember("Анна Смирнова", "продукт"), TeamMember("Илья Петров", "архитектор")]),
    ])
    assert not [p for p in contract_problems(two, contract) if "участник" in p]
    many = SlideSpec(index=1, kind="bullets", headline="Команда пилота из многих людей", blocks=[
        TeamBlock(items=[TeamMember(f"Человек {i}", "роль") for i in range(contract.slots[0].count + 1)]),
    ])
    assert any("участник" in p for p in contract_problems(many, contract))


def test_text_comparison_table_is_required_and_roadmap_is_not():
    intents = {vi.data_ref: vi for vi in visual_intents([SourceDoc(name="s", text=_SOURCES)])}
    tables = [vi for vi in intents.values() if vi.type == "table"]
    comparison = next(vi for vi in tables if "Сравнение" in vi.data.table.heading)
    assert comparison.required
    # План по кварталам таблицей не обязателен: у дорожной карты своя форма.
    assert not any(vi.required for vi in tables if "План" in vi.data.table.heading)


def test_quote_with_author_is_found_in_sources():
    quotes = find_quotes(_SOURCES)
    assert len(quotes) == 1
    assert quotes[0].text.startswith("Впервые заявка")
    assert quotes[0].author == "Сергей Лебедев, директор по операциям"
    assert find_quotes("Продукт «Портал заявок» запущен в марте.") == []


def test_styles_give_the_source_quote_a_quote_slide_and_data_points_kpi():
    visuals = visual_intents([SourceDoc(name="s", text=_SOURCES)])
    base = [
        SlideIntent(index=0, outline_kind="title", intent="Обложка"),
        SlideIntent(index=1, outline_kind="data", intent="Ключевые показатели пилота", items=3),
        SlideIntent(index=2, outline_kind="context", intent="Цитата заказчика о пилоте", items=1),
        SlideIntent(index=3, outline_kind="closing", intent="Спасибо"),
    ]
    for style in ("dense", "visual"):
        shaped = shape_for_style(base, style, visuals, profile=_no_forms_profile())
        assert shaped[1].form == "kpi", style
        assert shaped[2].form == "quote", style
        assert any("Впервые заявка" in n for n in shaped[2].needs)
