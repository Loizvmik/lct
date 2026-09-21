"""Общая инфраструктура тестов Task 4 (типографическая шкала и сетка),
Task 5 (каталог лейаутов) и Task 6 (каталог ассетов).

`profile_fixture` — вызываемая фикстура: `profile_fixture(name) -> TemplateProfile`
с полями `.type_scale`/`.grid`/`.layouts`/`.assets`, ровно как использует её бриф.
Разбор одного и того же файла кэшируется на весь прогон (`functools.lru_cache`)
— иначе каждый тест из test_typography.py/test_grid.py/test_layouts.py/
test_assets.py, ссылающийся на один и тот же шаблон, заново парсил бы весь
.pptx (собственно usage.collect_usage уже обходит все слайды/лейауты/мастера,
это не дёшево).
"""
from __future__ import annotations
import functools
from dataclasses import dataclass
from pathlib import Path

import pytest

from deckforge.ooxml.package import PptxPackage
from deckforge.template.assets import AssetCatalog, build_asset_catalog
from deckforge.template.grid import Grid, build_grid
from deckforge.template.layouts import LayoutEntry, build_layout_catalog
from deckforge.template.theme import pick_primary_master, read_theme
from deckforge.template.typography import TypeScale, build_type_scale
from deckforge.template.usage import collect_usage

TEMPLATES_DIR = Path("dataset/templates")

# Тесты пишем по первым трём шаблонам брифа — ЛЦТ2026 контрольный, для
# защиты, в тестах не участвует (тот же принцип, что в tests/template/
# test_theme.py и tests/template/test_usage.py).
ALL_TEMPLATES = [
    "VK Tech шаблон.pptx",
    "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
    "Шаблон презентации VK Education.pptx",
]


@dataclass(frozen=True)
class TemplateProfile:
    type_scale: TypeScale
    grid: Grid
    layouts: list[LayoutEntry]
    assets: AssetCatalog


@functools.lru_cache(maxsize=None)
def _build_profile(name: str) -> TemplateProfile:
    with PptxPackage.open(TEMPLATES_DIR / name) as pkg:
        canvas = pkg.canvas()
        usage = collect_usage(pkg, canvas)
        type_scale = build_type_scale(pkg, canvas, usage)
        grid = build_grid(pkg, canvas)
        theme = read_theme(pkg, pick_primary_master(pkg))
        # usage/type_scale переданы явно (находка код-ревью Task 5, п.4):
        # build_layout_catalog больше не обязан пересчитывать их сам, раз
        # они уже посчитаны парой строк выше для остального профиля —
        # без этого архив обходился бы дважды.
        layouts = build_layout_catalog(pkg, canvas, theme, grid, usage=usage, type_scale=type_scale)
        assets = build_asset_catalog(pkg, canvas, layouts)
    return TemplateProfile(type_scale=type_scale, grid=grid, layouts=layouts, assets=assets)


@pytest.fixture
def profile_fixture():
    return _build_profile
