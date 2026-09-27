"""Клон кладёт значение и подпись показателя в одну фигуру примера.

У WorkSpace на слайдах 17 и 18 «43%» и строка под ним набраны одной
надписью: разбор делит её на слоты `kpi_value` и `kpi_label` с одним
`source_shape_id`. Раньше второй слот не находил фигуры, клон отклонялся,
и показатели уходили в сборку с нуля мелким кеглем."""
from __future__ import annotations
from pathlib import Path

import pytest
from pptx import Presentation

from deckforge.audit.config import AuditConfig
from deckforge.compose import builder
from deckforge.compose.clone import sample_slides_by_number
from deckforge.ooxml.ns import qn
from deckforge.plan.spec import Kpi, KpiBlock, SlideSpec
from deckforge.template.profile import TemplateProfile

TEMPLATE = Path("dataset/templates/VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx")


@pytest.fixture(scope="module")
def profile():
    return TemplateProfile.from_file(TEMPLATE, cache_dir=None)


def _pattern(profile, pattern_id: str):
    return next(builder._pattern_from_model(m) for m in profile.patterns if m.pattern_id == pattern_id)


def _shared(pattern) -> dict[str, list]:
    by_id: dict[str, list] = {}
    for slot in pattern.slots:
        if slot.source_shape_id:
            by_id.setdefault(slot.source_shape_id, []).append(slot)
    return {k: v for k, v in by_id.items() if len(v) > 1}


@pytest.mark.parametrize("number, items", [
    (17, [Kpi("31%", "заявок закрыто в срок"), Kpi("4,1", "оценка удобства")]),
    (18, [Kpi("6,2 ч", "медиана согласования")]),
])
def test_value_and_label_share_one_shape(profile, number, items):
    pattern = _pattern(profile, f"slide{number}")
    shared = _shared(pattern)
    assert shared, "разбор больше не делит фигуру показателя на два слота"
    prs = Presentation(str(TEMPLATE))
    sources = sample_slides_by_number(prs)
    builder._clear_sample_slides(prs)
    spec = SlideSpec(index=0, kind="kpi", headline="Пилот сократил медиану", blocks=[KpiBlock(items=items)])

    outcome = builder.place_slide_by_clone(prs, spec, pattern, profile, AuditConfig.load(), sources[number])

    assert outcome.reason is None, outcome
    slide = prs.slides[-1]
    for kpi in items:
        sp = next(
            el for el in slide._element.iter(qn("p:sp"))
            if any((t.text or "") == kpi.value for t in el.iter(qn("a:t")))
        )
        paragraphs = [p for p in sp.iter(qn("a:p")) if "".join(t.text or "" for t in p.iter(qn("a:t")))]
        texts = ["".join(t.text or "" for t in p.iter(qn("a:t"))) for p in paragraphs]
        assert texts == [kpi.value, kpi.label]
        sizes = [int(p.find(".//" + qn("a:rPr")).get("sz")) / 100 for p in paragraphs]
        value_pt = next(s.size_pt for v in shared.values() for s in v if s.role == "kpi_value")
        label_pt = next(s.size_pt for v in shared.values() for s in v if s.role == "kpi_label")
        assert sizes == [value_pt, label_pt]
        assert sizes[0] >= 1.5 * sizes[1]
