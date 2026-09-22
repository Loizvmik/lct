"""Тесты `deckforge.audit.report.AuditReport.merge` (Task 12, интерфейс
брифа дословно: `AuditReport.merge(deterministic, visual) -> AuditReport`)."""
from __future__ import annotations

from deckforge.audit.findings import Finding
from deckforge.audit.report import AuditReport
from deckforge.audit.visual import VisualAuditResult


def _f(check_id: str, slide_index: int | None, severity: str = "major") -> Finding:
    return Finding(
        check_id=check_id, severity=severity, slide_index=slide_index, shape_ref=None,
        message=f"{check_id} на слайде {slide_index}", box=None, fixable=False, fix_hint="",
    )


def test_merge_combines_both_lists_and_sorts_stably():
    det = [_f("L01", 1), _f("I01", None)]
    vis = VisualAuditResult(findings=[_f("C05", 0), _f("C09", None)])
    report = AuditReport.merge(det, vis)
    assert len(report.findings) == 4
    assert report.deterministic_count == 2
    assert report.visual_count == 2
    # slide_index=None сортируется первым (-1), как и в audit.deterministic._stable_sort.
    assert [f.slide_index for f in report.findings] == [None, None, 0, 1]


def test_merge_carries_visual_skipped_reason():
    det = [_f("L01", 0)]
    vis = VisualAuditResult(findings=[], skipped_reason="модель не мультимодальна")
    report = AuditReport.merge(det, vis)
    assert report.visual_skipped_reason == "модель не мультимодальна"
    assert report.findings == det  # детерминированные находки остаются на месте
    assert report.visual_count == 0


def test_merge_accepts_plain_list_for_visual_too():
    """Интерфейс брифа дословно (`run_visual(...) -> list[Finding]`) —
    `merge` обязан принимать и голый список, не только `VisualAuditResult`."""
    det = [_f("L01", 0)]
    vis = [_f("C05", 0)]
    report = AuditReport.merge(det, vis)
    assert len(report.findings) == 2
    assert report.visual_skipped_reason is None


def test_by_severity_and_by_check_counts():
    det = [_f("L01", 0, "critical"), _f("L01", 1, "critical")]
    vis = VisualAuditResult(findings=[_f("C05", 0, "major")])
    report = AuditReport.merge(det, vis)
    assert report.by_severity() == {"critical": 2, "major": 1}
    assert report.by_check() == {"L01": 2, "C05": 1}


def test_merge_empty_deterministic_and_empty_visual():
    report = AuditReport.merge([], VisualAuditResult(findings=[]))
    assert report.findings == []
    assert report.by_severity() == {}
