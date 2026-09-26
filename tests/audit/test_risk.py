"""`audit.risk`: технический и семантический балл риска слайда (задача L
добавила семантический рядом с техническим из задачи H) и отбор
рискованных слайдов под аудит по картинке по их сумме."""
from __future__ import annotations

from deckforge.audit.findings import Finding
from deckforge.audit.risk import pick_risky_slides, risk_score, semantic_risk
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec, TableVisual, Visual

_CLONED = "Слайд 1: собран клоном слайда-примера №3 шаблона (раскладка 'p1')."


def _slide(
    index: int = 1, *, cloned: bool = True, notes: tuple[str, ...] = (), visual: Visual | None = None,
    # Дефолт нейтрален к семантическому баллу (задача L): заголовок несёт
    # цифру (не «тема без вывода») и есть `source_note` — тесты технического
    # балла и старые тесты `pick_risky_slides` не должны молча приобрести
    # одинаковую добавку семантики на каждом слайде. Тесты именно
    # семантического балла ниже переопределяют оба явно.
    headline: str = "Показатели выросли на 12%", blocks=None, source_note: str | None = "Источник данных",
) -> SlideSpec:
    findings = ([_CLONED] if cloned else []) + list(notes)
    if blocks is None:
        blocks = [BulletBlock(items=["пункт"])]
    return SlideSpec(
        index=index, kind="bullets", headline=headline, blocks=blocks,
        visual=visual, findings=findings, source_note=source_note,
    )


def _finding(slide_index: int, severity: str = "major", check_id: str = "L03") -> Finding:
    return Finding(
        check_id=check_id, severity=severity, slide_index=slide_index, shape_ref=None,
        message="m", box=None, fixable=False, fix_hint="h",
    )


def test_clean_cloned_slide_has_zero_risk():
    assert risk_score(_slide(), []) == 0.0


def test_each_sign_raises_risk():
    base = risk_score(_slide(), [])
    assert risk_score(_slide(cloned=False), []) > base
    assert risk_score(_slide(), [_finding(0)]) > base
    assert risk_score(_slide(notes=("Слайд 1: текст слота «body» усечён — не влезает даже кеглем подписи (12.0pt).",)), []) > base
    dropped = "Слайд 1: подзаголовок не попал на слайд — в раскладке 'p1' нет слота под эту роль: «x»."
    assert risk_score(_slide(notes=(dropped,)), []) > base
    table = Visual(kind="table", table=TableVisual(rows=[["a", "b"], ["1", "2"]]))
    assert risk_score(_slide(visual=table), []) > base
    assert risk_score(_slide(visual=Visual(kind="photo")), []) > base
    assert risk_score(_slide(), [], autofixed=True) > base


def test_severity_orders_findings_and_is_capped():
    critical = risk_score(_slide(), [_finding(0, "critical")])
    minor = risk_score(_slide(), [_finding(0, "minor")])
    assert critical > minor
    many = risk_score(_slide(), [_finding(0, "critical")] * 20)
    assert many <= 3.0


def test_pick_risky_slides_orders_filters_and_limits():
    spec = DeckSpec(title="t", language="ru", slides=[
        _slide(1),                                   # 0: чистый клон, 0 баллов
        _slide(2, cloned=False),                     # 1: с нуля, 1 балл
        _slide(3, cloned=False),                     # 2: с нуля + critical, 3 балла
        _slide(4),                                   # 3: клон + minor, 0.3 балла
        _slide(5, cloned=False),                     # 4: с нуля + major, 2 балла
    ])
    findings = [_finding(2, "critical"), _finding(3, "minor"), _finding(4, "major"), _finding(None, "major")]
    picked = pick_risky_slides(spec, findings, max_slides=2, min_score=1.0)
    assert [pos for pos, _t, _s in picked] == [2, 4]
    all_risky = pick_risky_slides(spec, findings, max_slides=10, min_score=1.0)
    assert [pos for pos, _t, _s in all_risky] == [2, 4, 1]
    assert pick_risky_slides(spec, findings, max_slides=0, min_score=0.0) == []


def test_pick_risky_slides_uses_autofixed_positions():
    spec = DeckSpec(title="t", language="ru", slides=[_slide(1), _slide(2)])
    assert pick_risky_slides(spec, [], max_slides=4, min_score=0.5) == []
    picked = pick_risky_slides(spec, [], max_slides=4, min_score=0.5, autofixed_slides={1})
    assert [pos for pos, _t, _s in picked] == [1]


def test_hero_slides_are_never_picked_for_visual_audit():
    """Обложка и разделители по замыслу несут один заголовок: модель
    отвечала на них «нет содержания» (C05) в каждом прогоне."""
    from deckforge.plan.spec import DeckSpec, SlideSpec
    from deckforge.audit.risk import pick_risky_slides

    spec = DeckSpec(title="t", language="ru", slides=[
        SlideSpec(index=0, kind="section", headline="Обложка", findings=["с нуля"] * 5),
        SlideSpec(index=1, kind="bullets", headline="Факт", findings=["с нуля"]),
    ])
    picked = pick_risky_slides(spec, [], max_slides=4, min_score=0.0)
    assert [pos for pos, _t, _s in picked] == [1]


# ---------------------------------------------------------------------------
# semantic_risk (задача L)
# ---------------------------------------------------------------------------

def test_clean_slide_with_verdict_headline_and_source_has_zero_semantic_risk():
    slide = _slide(headline="Выручка выросла на 30% за квартал", source_note="Отчёт отдела продаж, март 2026")
    assert semantic_risk(slide) == 0.0


def test_short_headline_is_topic_not_verdict():
    assert semantic_risk(_slide(headline="Итоги")) > 0.0
    assert semantic_risk(_slide(headline="Наша команда")) > 0.0


def test_long_headline_without_verb_or_digit_is_topic():
    long_no_verb = _slide(headline="Команда продукта и дизайна на проекте")
    assert semantic_risk(long_no_verb) > 0.0


def test_long_headline_with_verb_or_digit_is_not_topic():
    with_verb = _slide(headline="Команда сократила время ответа поддержки")
    with_digit = _slide(headline="Поддержка отвечает за 12 минут в среднем")
    assert semantic_risk(with_verb) == 0.0
    assert semantic_risk(with_digit) == 0.0


def test_title_duplicate_note_raises_semantic_risk():
    note = "Слайд 1: заголовок похож на слайд 4 ('Рост выручки') — возможно, один и тот же факт другими словами."
    base = semantic_risk(_slide(headline="Рост выручки на 30% в квартале"))
    with_dup = semantic_risk(_slide(headline="Рост выручки на 30% в квартале", notes=(note,)))
    assert with_dup > base


def test_fallback_slide_raises_semantic_risk():
    note = "Слайд собран запасным вариантом — модель недоступна или не вернула валидный ответ."
    base = semantic_risk(_slide(headline="Рост выручки на 30% в квартале"))
    fallback = semantic_risk(_slide(headline="Рост выручки на 30% в квартале", notes=(note,)))
    assert fallback > base


def test_unsourced_numbers_raise_semantic_risk():
    with_source = semantic_risk(_slide(headline="Рост выручки на 30%", source_note="CRM, март 2026"))
    without_source = semantic_risk(_slide(headline="Рост выручки на 30%", source_note=None))
    assert without_source > with_source


def test_empty_blocks_raise_semantic_risk():
    empty = semantic_risk(_slide(headline="Рост выручки на 30% в квартале", blocks=[]))
    non_empty = semantic_risk(_slide(headline="Рост выручки на 30% в квартале"))
    assert empty > non_empty


def test_semantic_risk_is_capped():
    note_dup = "Слайд 1: заголовок похож на слайд 4 ('X') — возможно, один и тот же факт другими словами."
    note_fallback = "Слайд собран запасным вариантом — модель недоступна или не вернула валидный ответ."
    worst = _slide(headline="Итоги", notes=(note_dup, note_fallback), blocks=[], source_note=None)
    assert semantic_risk(worst) <= 3.0


def test_pick_risky_slides_orders_by_sum_and_breaks_ties_by_semantic():
    """При равной сумме вперёд идёт слайд с большим семантическим баллом
    (бриф задачи L)."""
    # Слайд 1: чистый клон (0 технических), заголовок-тема (1 семантический) -> сумма 1.0
    topic_only = _slide(1, headline="Итоги", source_note="s")
    # Слайд 2: с нуля (1 технический), заголовок-вывод (0 семантических) -> сумма 1.0
    scratch_only = _slide(2, cloned=False, headline="Выручка выросла на 30%", source_note="s")
    spec = DeckSpec(title="t", language="ru", slides=[topic_only, scratch_only])
    picked = pick_risky_slides(spec, [], max_slides=2, min_score=1.0)
    assert [pos for pos, _t, _s in picked] == [0, 1]  # семантический балл выше — слайд 0 (topic_only) первым
