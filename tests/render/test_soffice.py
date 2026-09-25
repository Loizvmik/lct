"""Тесты `deckforge.render.soffice` (task-12-brief, раздел про soffice —
рендер PDF/PNG нужен этой задаче для PDF-экспорта и превью-панели, см.
task-14-brief: "часть про рендер возьми из task-12-brief.md")."""
from __future__ import annotations
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from deckforge.compose.builder import Variant, build_deck
from deckforge.render.soffice import soffice_available, to_pdf, to_pngs

pytestmark = pytest.mark.skipif(not soffice_available(), reason="LibreOffice не установлен")


def _pdf_pages(pdf: Path) -> int:
    result = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=10)
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise RuntimeError(f"pdfinfo не отдал число страниц для {pdf}")


def test_pptx_converts_to_pdf_with_the_same_page_count(DECK, PROFILE, PPTX, tmp_path):
    pdf = to_pdf(PPTX, tmp_path)
    assert pdf.exists()
    assert _pdf_pages(pdf) == len(_actual_slide_count(PPTX))


def _actual_slide_count(pptx: Path):
    from pptx import Presentation

    return list(Presentation(str(pptx)).slides)


def test_one_png_per_slide(PPTX, tmp_path):
    pngs = to_pngs(PPTX, tmp_path)
    assert len(pngs) == len(_actual_slide_count(PPTX))
    assert all(Image.open(p).width >= 1000 for p in pngs)


def test_png_resolution_matches_requested_dpi(PPTX, tmp_path):
    pngs_lo = to_pngs(PPTX, tmp_path / "lo", dpi=72)
    pngs_hi = to_pngs(PPTX, tmp_path / "hi", dpi=150)
    assert Image.open(pngs_hi[0]).width > Image.open(pngs_lo[0]).width


def test_parallel_conversions_do_not_fight_over_the_profile(PPTX, tmp_path):
    """Два soffice без своего -env:UserInstallation дерутся за блокировку профиля."""
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda i: to_pdf(PPTX, tmp_path / str(i)), range(4)))
    assert all(p.exists() for p in results)


def test_to_pngs_with_given_pdf_path_does_not_render_pdf_again(PPTX, tmp_path, monkeypatch):
    """Заранее отрендеренный PDF (`pdf_path=`) не должен гонять soffice
    ещё раз — именно этот дубликат и убирает эта правка (export_bundle
    иначе конвертирует один pptx дважды, см. докстроку `to_pngs`)."""
    import deckforge.render.soffice as soffice_mod

    pdf_path = to_pdf(PPTX, tmp_path)
    calls = []
    orig_run_soffice = soffice_mod._run_soffice
    monkeypatch.setattr(
        soffice_mod, "_run_soffice",
        lambda *a, **kw: calls.append(a) or orig_run_soffice(*a, **kw),
    )

    pngs = to_pngs(PPTX, tmp_path / "preview", pdf_path=pdf_path)

    assert len(pngs) == len(_actual_slide_count(PPTX))
    assert calls == []
