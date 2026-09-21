"""Обёртка над .pptx-пакетом как над OPC zip-архивом.

Тема резолвится только через `_rels`, никогда по имени файла: у VK
WorkSpace `ppt/theme/theme1.xml` — офисная заглушка, а брендовая палитра
лежит в `theme2.xml`, привязанном к `slideMaster1.xml` через relationship
с типом `theme`. Парсер, читающий `theme1.xml` напрямую, эту подмену не
заметит.
"""
from __future__ import annotations
import hashlib
import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree
from PIL import Image


@dataclass(frozen=True)
class MediaEntry:
    name: str
    size_bytes: int
    width: int | None
    height: int | None
    has_alpha: bool
    md5: str


class PptxPackage:
    """Части .pptx как OPC zip-пакета: XML частей, relationships, медиа.

    Держит `zipfile.ZipFile` открытым, а не распаковывает архив на диск и
    не грузит его целиком в память — шаблоны весят десятки мегабайт, а в
    `ppt/media/` бывает больше двухсот изображений.
    """

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self._zf = zf
        self._names = zf.namelist()
        self._name_set = set(self._names)
        self._xml_cache: dict[str, etree._Element] = {}
        self._rels_cache: dict[str, dict[str, str]] = {}
        # rid → (rel_type, target, is_external) для part_name, сырые (ещё не
        # резолвнутые в имя парта) relationship-записи. Общий кэш для rels()
        # и related() — раньше related() перечитывал и перепарсивал .rels-XML
        # из zip на каждый вызов, не пользуясь кэшем вовсе, в отличие от rels().
        self._raw_rels_cache: dict[str, list[tuple[str, str, str, bool]]] = {}

    @classmethod
    def open(cls, path: Path) -> "PptxPackage":
        return cls(zipfile.ZipFile(path, "r"))

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> "PptxPackage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def names(self) -> list[str]:
        return self._names

    def part(self, name: str) -> bytes:
        return self._zf.read(name)

    def xml(self, name: str) -> etree._Element:
        cached = self._xml_cache.get(name)
        if cached is None:
            cached = etree.fromstring(self.part(name))
            self._xml_cache[name] = cached
        return cached

    def rels(self, part_name: str) -> dict[str, str]:
        """rId → имя связанного парта.

        Внешние связи (`TargetMode="External"`, например гиперссылки)
        отдаются как есть, без резолва относительно пакета.
        """
        cached = self._rels_cache.get(part_name)
        if cached is not None:
            return cached

        result: dict[str, str] = {}
        for rid, rel_type, target, is_external in self._iter_rels(part_name):
            result[rid] = target if is_external else _resolve_target(part_name, target)
        self._rels_cache[part_name] = result
        return result

    def related(self, part_name: str, rel_type_suffix: str) -> list[str]:
        """Имена партов, связанных с `part_name` отношением с данным суффиксом типа.

        Суффикс — последний сегмент URI типа (`.../relationships/theme` → `"theme"`).
        """
        out = []
        for rid, rel_type, target, is_external in self._iter_rels(part_name):
            if rel_type.rsplit("/", 1)[-1] != rel_type_suffix:
                continue
            out.append(target if is_external else _resolve_target(part_name, target))
        return out

    def _iter_rels(self, part_name: str) -> list[tuple[str, str, str, bool]]:
        cached = self._raw_rels_cache.get(part_name)
        if cached is not None:
            return cached

        rels_name = _rels_part_name(part_name)
        if rels_name not in self._name_set:
            cached = []
        else:
            root = etree.fromstring(self.part(rels_name))
            cached = [
                (rel.get("Id"), rel.get("Type", ""), rel.get("Target", ""),
                 rel.get("TargetMode") == "External")
                for rel in root
            ]
        self._raw_rels_cache[part_name] = cached
        return cached

    def media(self) -> list[MediaEntry]:
        return [
            self._media_entry(info)
            for info in self._zf.infolist()
            if info.filename.startswith("ppt/media/")
        ]

    def _media_entry(self, info: zipfile.ZipInfo) -> MediaEntry:
        width = height = None
        has_alpha = False
        # Image.open читает только заголовок до первого обращения к .size —
        # пиксели не декодируются, полный файл в память не разворачивается.
        try:
            with self._zf.open(info.filename) as stream, Image.open(stream) as img:
                width, height = img.size
                has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
        except Exception:
            # неподдерживаемый/битый формат (напр. emf/wmf) не должен ронять разбор пакета
            pass

        md5 = hashlib.md5()
        with self._zf.open(info.filename) as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                md5.update(chunk)

        return MediaEntry(
            name=info.filename,
            size_bytes=info.file_size,
            width=width,
            height=height,
            has_alpha=has_alpha,
            md5=md5.hexdigest(),
        )


def _rels_part_name(part_name: str) -> str:
    base_dir = posixpath.dirname(part_name)
    base_name = posixpath.basename(part_name)
    return posixpath.join(base_dir, "_rels", f"{base_name}.rels")


def _resolve_target(part_name: str, target: str) -> str:
    """Относительный Target — относительно каталога part_name; абсолютный ("/...") — от корня пакета."""
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = posixpath.dirname(part_name)
    return posixpath.normpath(posixpath.join(base_dir, target))
