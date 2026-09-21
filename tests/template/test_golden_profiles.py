"""Золотой тест по ключевым полям TemplateProfile (Task 8 код-ревью,
находка 4): `fixtures/expected/*.json` раньше лежали мёртвым грузом —
~1 МБ полных профилей, которые НИ ОДИН тест не читал, и которые молча
разъехались бы с реальностью при любой правке любого из семи модулей
разбора.

Решение — не полное дерево профиля (перегенерировать десяток чисел при
намеренном изменении алгоритма не больно, а вот отличить намеренное
изменение от регрессии в мегабайте JSON — больно), а ключевые поля: роли
палитры, ступени типографической шкалы, поля сетки, число лейаутов и
паттернов. Файлы в fixtures/expected/ ужаты до этого набора (были ~1 МБ
суммарно, стали ~2.7 КБ).

Палитра — без ключа (namer=None): золотые файлы сгенерированы `deckforge
parse` без ключа (см. task-8-report.md), детерминированный запасной
вариант именования не меняется между прогонами.

ЛЦТ2026 — контрольный шаблон для защиты, в тестах не участвует, тот же
принцип, что и в остальных tests/template/*.py (см. test_theme.py):
fixtures/expected/ЛЦТ2026 *.json обновлён вместе с остальными и годится для
ручной сверки, но не для automated golden-теста."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from deckforge.template.profile import TemplateProfile

TEMPLATES_DIR = Path("dataset/templates")
GOLDEN_DIR = Path("fixtures/expected")

GOLDEN_TEMPLATES = [
    "VK Tech шаблон.pptx",
    "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
    "Шаблон презентации VK Education.pptx",
]


def _golden_fields(profile: TemplateProfile) -> dict:
    return {
        "source_name": profile.source_name,
        "palette_roles": dict(sorted(profile.palette_roles.items())),
        "type_scale_steps": dict(sorted(profile.type_scale.steps.items())),
        "grid_margins": {
            "margin_left": profile.grid.margin_left,
            "margin_right": profile.grid.margin_right,
            "margin_top": profile.grid.margin_top,
            "margin_bottom": profile.grid.margin_bottom,
            "gutter": profile.grid.gutter,
        },
        "layouts_count": len(profile.layouts),
        "patterns_count": len(profile.patterns),
    }


@pytest.mark.parametrize("name", GOLDEN_TEMPLATES)
def test_key_fields_match_golden_profile(name):
    profile = TemplateProfile.from_file(TEMPLATES_DIR / name)
    actual = _golden_fields(profile)

    golden_path = GOLDEN_DIR / f"{Path(name).stem}.json"
    expected = json.loads(golden_path.read_text(encoding="utf-8"))

    assert actual["source_name"] == expected["source_name"]
    assert actual["palette_roles"] == expected["palette_roles"]
    assert actual["layouts_count"] == expected["layouts_count"]
    assert actual["patterns_count"] == expected["patterns_count"]
    assert actual["type_scale_steps"] == pytest.approx(expected["type_scale_steps"])
    assert actual["grid_margins"] == pytest.approx(expected["grid_margins"])
