"""Каталог ассетов (логотип/фон/иконки/фото) — тесты дословно из брифа Task 6
(Step 1), .superpowers/sdd/task-6-brief.md.

`profile_fixture` в телах тестов брифа нужен как параметр — иначе pytest не
подставит фикстуру и вызов упадёт с NameError; тот же приём, что уже
применялся в Task 3–5 (см. докстроку test_layouts.py).
"""
from __future__ import annotations
from conftest import ALL_TEMPLATES


def test_logo_is_found_in_every_template(profile_fixture):
    """Логотип = маленький файл, много размещений в лейаутах, угловая позиция."""
    for name in ALL_TEMPLATES:
        catalog = profile_fixture(name).assets
        assert catalog.logo is not None, name
        assert catalog.logo.size_bytes < 200_000


def test_education_icon_set_is_recognised(profile_fixture):
    """206 изображений ровно 112×112 — иконочный сет опознаётся однозначно."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").assets
    assert len(catalog.icons) >= 100


def test_full_bleed_background_is_separated_from_icons(profile_fixture):
    catalog = profile_fixture("VK Tech шаблон.pptx").assets
    assert catalog.backgrounds
    assert all(b.size_bytes > 100_000 for b in catalog.backgrounds)
    assert catalog.logo not in catalog.backgrounds


def test_logo_placement_is_recorded_for_the_audit(profile_fixture):
    """Проверка T05 «логотип сдвинут с положенного места» сверяется с этим."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").assets
    assert catalog.logo_placements
    top = catalog.logo_placements[0]
    assert 0.0 <= top.box.left <= 0.3
