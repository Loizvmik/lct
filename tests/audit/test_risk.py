"""`audit.risk`: балл риска слайда и отбор рискованных слайдов под аудит
по картинке."""
from __future__ import annotations

from deckforge.audit.findings import Finding
from deckforge.audit.risk import pick_risky_slides, risk_score
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec, TableVisual, Visual

_CLONED = "Слайд 1: собран клоном слайда-примера №3 шаблона (раскладка 'p1')."


def _slide(index: int = 1, *, cloned: bool = True, notes: tuple[str, ...] = (), visual: Visual | None = None) -> SlideSpec:
    findings = ([_CLONED] if cloned else []) + list(notes)
    return SlideSpec(
        index=index, kind="bullets", headline="Заголовок", blocks=[BulletBlock(items=["пункт"])],
        visual=visual, findings=findings,
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
    assert [pos for pos, _ in picked] == [2, 4]
    all_risky = pick_risky_slides(spec, findings, max_slides=10, min_score=1.0)
    assert [pos for pos, _ in all_risky] == [2, 4, 1]
    assert pick_risky_slides(spec, findings, max_slides=0, min_score=0.0) == []


def test_pick_risky_slides_uses_autofixed_positions():
    spec = DeckSpec(title="t", language="ru", slides=[_slide(1), _slide(2)])
    assert pick_risky_slides(spec, [], max_slides=4, min_score=0.5) == []
    picked = pick_risky_slides(spec, [], max_slides=4, min_score=0.5, autofixed_slides={1})
    assert [pos for pos, _ in picked] == [1]
