"""Архитектурная граница пакета `deckforge.plan` (докстрока `plan/spec.py`:
"план не импортирует python-pptx... не знает ни одной координаты" — Task 13,
подтверждена Task 19: агентный цикл `plan.writer` получил новый инструмент,
`compose.fit_check.measure_fit`, который РАБОТАЕТ с координатами (`PatternSlot.
box`, `Canvas`) — граница обязана остаться целой: `plan/` зовёт инструмент
как чёрный ящик (текст, роль, профиль, вид раскладки) и получает обратно
только плоские числа, сам не должен ни разу тронуть `python-pptx` или
`Box`/EMU-координату напрямую.

Тест ниже проверяет буквальный, однозначный кусок этого обещания — импорт
`python-pptx` — тем же приёмом (`ast`), что и `tests/template/test_
architecture.py`/`tests/test_no_hardcoded_prompts.py` в этом же проекте."""
from __future__ import annotations
import ast
from pathlib import Path

PLAN_PKG = Path("src/deckforge/plan")


def _pptx_import_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "pptx" or alias.name.startswith("pptx.") for alias in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "pptx" or node.module.startswith("pptx.")):
                lines.append(node.lineno)
    return lines


def test_plan_package_does_not_import_python_pptx():
    offenders = {}
    for path in sorted(PLAN_PKG.glob("*.py")):
        lines = _pptx_import_lines(path)
        if lines:
            offenders[path.name] = lines
    assert not offenders, (
        f"эти модули deckforge.plan импортируют python-pptx: {offenders} — "
        "план не должен знать ни одной координаты (докстрока plan/spec.py)"
    )


def test_the_check_itself_catches_a_planted_pptx_import(tmp_path):
    """Тест теста (тот же парный приём, что и в соседних архитектурных
    проверках проекта): если бы модуль `plan/` правда заимпортировал
    `python-pptx`, проверка выше должна была бы это заметить."""
    planted = tmp_path / "planted.py"
    planted.write_text("from pptx import Presentation\n", encoding="utf-8")
    assert _pptx_import_lines(planted), "проверка не заметила явный импорт python-pptx — тест бесполезен"
