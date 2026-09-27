"""Пределы распаковки чужого pptx (`ooxml.package.check_zip_limits`)."""
from __future__ import annotations
import zipfile

import pytest

from deckforge.ooxml import package as pkg


def _zip_with_part(path, name: str, size: int):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(name, b"A" * size)
    return path


def test_decompression_bomb_is_refused_before_reading(tmp_path, monkeypatch):
    """Маленький zip с частью, которая разворачивается больше предела, не
    открывается вовсе: память под неё не выделяется."""
    monkeypatch.setattr(pkg, "MAX_PART_BYTES", 1 * pkg.MB)
    bomb = _zip_with_part(tmp_path / "bomb.pptx", "ppt/theme/theme1.xml", 3 * pkg.MB)
    assert bomb.stat().st_size < 100 * 1024, "сжатие 1000:1 даёт маленький файл"
    with pytest.raises(pkg.PackageTooLarge):
        pkg.PptxPackage.open(bomb)


def test_total_uncompressed_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(pkg, "MAX_PART_BYTES", 1 * pkg.MB)
    monkeypatch.setattr(pkg, "MAX_TOTAL_BYTES", 2 * pkg.MB)
    path = tmp_path / "many.pptx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(4):
            zf.writestr(f"ppt/media/image{i}.bin", b"B" * (900 * 1024))
    with pytest.raises(pkg.PackageTooLarge):
        pkg.PptxPackage.open(path)


def test_ordinary_package_opens(tmp_path):
    path = _zip_with_part(tmp_path / "small.pptx", "ppt/presentation.xml", 10 * 1024)
    with pkg.PptxPackage.open(path) as p:
        assert p.part("ppt/presentation.xml") == b"A" * (10 * 1024)
