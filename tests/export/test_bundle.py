"""Тест `deckforge.export.bundle.export_bundle` (Task 14, Step 1 брифа:
`test_bundle_has_all_three_formats`). Требует LibreOffice — `.pdf`/`.png`
идут через `render.soffice` (см. её докстроку про изоляцию параллельных
вызовов и шрифт шаблона)."""
from __future__ import annotations

import pytest

from deckforge.export.bundle import export_bundle
from deckforge.render.soffice import soffice_available


@pytest.mark.skipif(not soffice_available(), reason="LibreOffice не установлен")
def test_bundle_has_all_three_formats(PROFILE, PPTX, tmp_path):
    bundle = export_bundle(PPTX, PROFILE, tmp_path)
    assert bundle.pptx.exists() and bundle.pdf.exists() and bundle.html.exists()


@pytest.mark.skipif(not soffice_available(), reason="LibreOffice не установлен")
def test_bundle_includes_deck_spec_when_given(DECK, PROFILE, PPTX, tmp_path):
    bundle = export_bundle(PPTX, PROFILE, tmp_path, deck_spec=DECK)
    text = bundle.html.read_text(encoding="utf-8")
    assert DECK.slides[1].headline in text


@pytest.mark.skipif(not soffice_available(), reason="LibreOffice не установлен")
def test_bundle_renders_one_png_per_slide(PROFILE, PPTX, tmp_path):
    from pptx import Presentation

    bundle = export_bundle(PPTX, PROFILE, tmp_path)
    assert len(bundle.pngs) == len(Presentation(str(PPTX)).slides)
