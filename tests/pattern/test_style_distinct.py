"""Задача D3: три стиля одного содержания визуально различимы. Стиль сам
переделывает намерения слайдов (`pattern.shape`), планировщик каждого
стиля платит за облики соседей по пакету (`plan_patterns(taken=...)`),
сводка считает долю совпадений (`pattern.distinct`)."""
from __future__ import annotations
import asyncio
from types import SimpleNamespace

from deckforge.pattern.distinct import distinctness, slide_looks
from deckforge.pattern.intent import SlideIntent, intents_from_outline
from deckforge.pattern.planner import batch_looks, plan_batch, plan_patterns
from deckforge.pattern.scoring import look_key
from deckforge.pattern.shape import MIN_SLIDES, shape_for_style
from deckforge.pattern.style import load_style
from deckforge.plan.outline import Outline, OutlineSlide

from .conftest import bullets, cards, profile, section, pattern, headline

_DECK = [
    "title", "agenda", "context", "problem", "solution", "how_it_works", "case", "risks",
    "team", "roadmap", "ask", "closing",
]


def _intents(kinds=_DECK, needs: int = 3) -> list[SlideIntent]:
    outline = Outline(slides=[
        OutlineSlide(kind=k, intent=f"Пункт {i}", needs=[f"факт {i}.{j}" for j in range(needs)])
        for i, k in enumerate(kinds)
    ], title="T")
    return intents_from_outline(outline)


def _batch_profile():
    """Шаблон, где у содержания есть из чего выбирать: карточки разного
    числа, списки, колонки, раскладка-схема с декором на каждую единицу."""
    return profile(
        section("cover", source=1, text=True),
        section("divider", source=5),
        pattern("thanks", "section", [headline(sample_text="Спасибо за внимание!")], source=60),
        cards("cards3", 3, source=11), cards("cards4", 4, source=12), cards("cards4b", 4, source=13, decor=6),
        cards("cards6", 6, source=14), cards("icons4", 4, source=15, decor=8, words=12),
        bullets("list_a", source=20, max_bullets=6), bullets("cols", source=21, kind="two_col", max_bullets=6),
        bullets("list_c", source=22, words=40, max_bullets=6),
    )


def _plans(prof, intents=None):
    base = intents or _intents()
    shaped = {s: shape_for_style(base, s, profile=prof) for s in ("dense", "airy", "visual")}
    return plan_batch(shaped, prof)


def test_three_styles_share_at_most_forty_percent_of_layouts_by_content_item(rich_profile):
    for prof in (rich_profile, _batch_profile()):
        plans = _plans(prof)
        summary = distinctness({s: slide_looks(p, prof) for s, p in plans.items()})

        assert summary["max_overlap"] <= 0.4, summary["pairs"]
        assert summary["ok"]
        assert all(pair["positions"] > 0 for pair in summary["pairs"].values())


def test_without_the_batch_penalty_styles_collide_more(rich_profile):
    """Штраф пакета и делает различие: без него те же стили на том же
    шаблоне совпадают чаще."""
    base = _intents()
    alone = {
        s: plan_patterns(shape_for_style(base, s, profile=rich_profile), rich_profile, s) for s in ("dense", "airy", "visual")
    }
    together = _plans(rich_profile, base)
    def worst(plans):
        return distinctness({s: slide_looks(p, rich_profile) for s, p in plans.items()})["max_overlap"]

    assert worst(together) < worst(alone)


def test_a_neighbours_look_on_the_same_item_is_avoided(rich_profile):
    base = _intents()
    first = plan_patterns(base, rich_profile, "dense")
    taken = batch_looks(first, rich_profile)
    second = plan_patterns(base, rich_profile, "dense", taken=taken)
    looks = {p.pattern_id: look_key(p) for p in rich_profile.patterns}

    same = [
        a.position for a, b in zip(first, second)
        if not a.intent.is_hero and looks[a.pattern_id] == looks[b.pattern_id]
    ]
    assert len(same) <= len(first) * 0.4


def test_heroes_are_not_penalised_the_cover_stays_the_cover(rich_profile):
    plans = _plans(rich_profile)
    for plan in plans.values():
        assert plan[0].pattern_id == "cover"
        assert plan[-1].pattern_id == "thanks"


def test_dense_merges_neighbours_but_not_below_the_minimum():
    base = _intents()
    dense = shape_for_style(base, "dense")

    assert MIN_SLIDES <= len(dense) < len(base)
    merged = [it for it in dense if len(it.outline_indices) > 1]
    assert merged
    for it in merged:
        assert "; " in it.intent
        assert len(it.needs) == sum(len(base[i].needs) for i in it.outline_indices)
    # Каждый пункт структуры покрыт ровно одним слайдом: содержание не теряется.
    covered = sorted(i for it in dense for i in it.outline_indices)
    assert covered == list(range(len(base)))
    assert all(4 <= it.items <= 6 for it in dense if it.outline_kind in ("context", "risks", "roadmap"))


def test_airy_keeps_one_thesis_and_visual_short_lists():
    base = _intents(needs=5)
    assert all(it.items <= 3 for it in shape_for_style(base, "airy"))
    assert all(it.items <= 4 for it in shape_for_style(base, "visual"))
    assert len(shape_for_style(base, "airy")) == len(base)


def _dataset(words: set[str], series: bool = True):
    return SimpleNamespace(words=words, series=object() if series else None)


def test_visual_charts_every_numeric_series_and_dense_tables_it():
    """Необязательный ряд источника: visual показывает графиком, dense
    таблицей, на самом близком по словам пункте."""
    from deckforge.plan.data_types import VisualIntent

    base = _intents()
    words = set(__import__("deckforge.plan.series", fromlist=["stems"]).stems("Пункт 7 риски"))
    vi = VisualIntent(type="chart", required=False, reason="ряд", data_ref="d1", chart_kind="bar",
                      data=_dataset(words))
    visual = shape_for_style(base, "visual", [vi])
    dense = shape_for_style(base, "dense", [vi])

    charted = [it for it in visual if it.form == "chart"]
    assert len(charted) == 1 and charted[0].visual_intent is vi
    tabled = [it for it in dense if it.form == "table"]
    assert len(tabled) == 1 and tabled[0].visual_intent.type == "table"
    # Уже занятый структурой набор не переназначается.
    taken = [*base]
    taken[3] = SlideIntent(index=3, outline_kind="data", intent="x", items=1, form="chart", visual_intent=vi)
    assert sum(it.form == "chart" for it in shape_for_style(taken, "visual", [vi])) == 1


def test_distinctness_counts_by_content_item_not_by_slide_number():
    def s(pos, outline, look, hero=False):
        return {"position": pos, "outline": outline, "pattern_id": look, "kind": "cards", "look": look, "hero": hero}

    looks = {
        "dense": [s(0, [0], "cover", True), s(1, [1, 2], "A"), s(2, [3], "B")],
        "airy": [s(0, [0], "cover", True), s(1, [], "div", True), s(2, [1], "A"), s(3, [2], "C"), s(4, [3], "D")],
    }
    summary = distinctness(looks, limit=0.4)
    pair = summary["pairs"]["dense/airy"]

    assert pair == {"positions": 3, "same": 1, "overlap": 0.333, "shared_looks": 0.25}
    assert summary["unique_looks"] == {"dense": 2, "airy": 3}
    assert summary["ok"]


def test_visual_contract_caps_list_items_at_eight_words(rich_profile):
    from deckforge.plan.contracts import build_contracts

    base = _intents()
    visual = plan_patterns(shape_for_style(base, "visual"), rich_profile, "visual")
    dense = plan_patterns(shape_for_style(base, "dense"), rich_profile, "dense")
    v_slots = [sl for c in build_contracts(visual, rich_profile, "visual") for sl in c.slots
               if sl.block in ("cards", "bullets") and sl.count >= 2]
    d_slots = [sl for c in build_contracts(dense, rich_profile, "dense") for sl in c.slots
               if sl.block in ("cards", "bullets") and sl.count >= 2]

    assert v_slots and all(sl.item.max_words <= 8 for sl in v_slots)
    assert any(sl.item.max_words > 8 for sl in d_slots)


def test_visual_roadmap_takes_label_layouts():
    """Дорожная карта у visual подписями: раскладка, где у события два
    слова, для неё не тесна, а для dense тесна."""
    from deckforge.pattern.forms import forms_of
    from deckforge.pattern.scoring import overflow

    tight = cards("timeline", 6, words=2, decor=8)
    prof = profile(tight)
    form = forms_of(prof)["timeline"]
    road = SlideIntent(index=1, outline_kind="roadmap", intent="План", items=5)
    assert overflow(road, form, load_style("visual")) == 0.0
    assert overflow(road, form, load_style("dense")) > 0.5


def test_plan_share_orders_dense_airy_visual_and_releases_on_failure(rich_profile):
    from deckforge.api.jobs import PlanShare

    async def run():
        share = PlanShare(["visual", "airy", "dense"])
        order: list[str] = []
        base = _intents()

        async def job(style, fail=False):
            taken = await share.before(style)
            if fail:
                share.abandon(style)
                return
            plan = plan_patterns(shape_for_style(base, style), rich_profile, style, taken=taken)
            order.append(style)
            share.publish(style, plan, rich_profile)

        await asyncio.gather(job("visual"), job("airy", fail=True), job("dense"))
        return share, order

    share, order = asyncio.run(run())
    assert order == ["dense", "visual"]
    summary = share.summary()
    assert set(summary["looks"]) == {"dense", "visual"}
    assert summary["pairs"]["dense/visual"]["overlap"] <= 0.4


def test_dense_never_asks_for_more_units_than_any_layout_holds():
    """Шаблон, где больше трёх единиц не лежит нигде: dense не сливает
    пункты в шесть единиц и не добирает до четырёх, иначе клон отклонит
    слайд и лестница сборки уведёт его на запасную раскладку."""
    small = profile(
        section("cover", source=1, text=True),
        pattern("thanks", "section", [headline(sample_text="Спасибо за внимание!")], source=60),
        cards("cards3", 3, source=11), cards("cards2", 2, source=12),
    )
    dense = shape_for_style(_intents(), "dense", profile=small)
    assert all(it.items <= 3 for it in dense)
    assert all(not a.relaxed for a in plan_patterns(dense, small, "dense"))
