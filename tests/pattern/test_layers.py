"""Граница слоёв пакета `deckforge.pattern` и контракта слайда: работают
только с профилем (роли, пределы слов, повторы), без python-pptx, без
координат и без вызовов модели. Тот же приём (`ast`), что у соседних
архитектурных проверок."""
from __future__ import annotations
import ast
from pathlib import Path

FILES = sorted(Path("src/deckforge/pattern").glob("*.py")) + [Path("src/deckforge/plan/contracts.py")]


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "pptx" for a in node.names):
            found.append(f"{path}:{node.lineno}: import pptx")
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pptx":
            found.append(f"{path}:{node.lineno}: from pptx")
        if isinstance(node, ast.Attribute) and node.attr in ("box", "left", "top", "width", "height"):
            found.append(f"{path}:{node.lineno}: координата .{node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
            "complete", "ask_image",
        ):
            found.append(f"{path}:{node.lineno}: вызов модели")
    return found


def test_planner_and_contracts_know_no_coordinates_pptx_or_model():
    assert FILES and all(p.exists() for p in FILES)
    offences = [o for p in FILES for o in _offences(p)]
    assert not offences, "\n".join(offences)


def test_the_check_catches_a_planted_offence(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text("from pptx import Presentation\nx = slot.box\nllm.complete([])\n", encoding="utf-8")
    assert len(_offences(planted)) == 3
