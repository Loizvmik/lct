"""ТЗ п.4: «Промпты/конфиги скиллов и агентов в репозитории лежат отдельными
файлами, т.е. не зашиты в код» — ищет в `src/deckforge` строковые литералы,
похожие на вшитую инструкцию модели на русском.

Русские докстроки и комментарии в проекте — норма (почти каждый модуль),
так что наивный grep по подстроке даёт сплошные ложные срабатывания. Ищем
именно то, что уходит В МОДЕЛЬ: строковый литерал (`ast.Constant`), который
- НЕ докстрока (не первый `Expr`-statement модуля/класса/функции — то, что
  `ast.get_docstring` не видит, комментарии `#` в AST вообще не попадают);
- длиннее короткого протокольного сообщения (`_MIN_LEN`) — короткие технические
  строки вроде "Ответь одним объектом JSON по схеме" в `provider/yandex.py`
  не домен-промпт конкретной роли, а протокольная обвязка одна на всех
  вызовов (JSON-режим), которая не относится ни к одному `agents/*/AGENT.md`
  и потому не должна лежать отдельным файлом;
- содержит характерный для промпта маркер обращения к модели.
"""
from __future__ import annotations
import ast
from pathlib import Path

SRC = Path("src/deckforge")

# Маркеры прямого обращения к модели, дословно из брифа Task 15 плюс
# формулировка, которой открываются все agents/*/AGENT.md ("Тебе дан...").
_PROMPT_MARKERS = ("Ты ", "Тебе дан", "Ответь", "Правила:")

# Короче — протокольная обвязка (см. докстроку модуля), длиннее — уже
# содержательная инструкция. "Ответь одним объектом JSON по схеме,
# без markdown-ограды:" — 58 символов, с запасом ниже порога.
_MIN_LEN = 80


def _docstring_node_ids(tree: ast.Module) -> set[int]:
    """id() строковых `Constant`-узлов, которые являются докстрокой модуля,
    класса, функции или метода — `ast.walk` видит их как обычные строковые
    константы, ничем не помеченные, если не отфильтровать явно."""
    ids: set[int] = set()
    scopes = [tree] + [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for scope in scopes:
        body = getattr(scope, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _offenders(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    doc_ids = _docstring_node_ids(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in doc_ids:
            continue
        text = node.value
        if len(text) < _MIN_LEN or not any(marker in text for marker in _PROMPT_MARKERS):
            continue
        found.append(f"{path}:{node.lineno}: {text[:70]!r}...")
    return found


def test_no_prompt_text_is_hardcoded_in_python():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        offenders.extend(_offenders(path))
    assert not offenders, "похоже на вшитый промпт (должен жить в AGENT.md/SKILL.md):\n" + "\n".join(
        offenders
    )


def test_the_check_itself_catches_a_planted_prompt(tmp_path):
    """Тест теста: если бы промпт правда зашили в код, проверка выше его бы поймала."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        'SYSTEM_PROMPT = (\n'
        '    "Ты помощник, который отвечает на вопросы пользователя. Правила:\\n"\n'
        '    "- будь краток\\n"\n'
        '    "- отвечай только фактами, ничего не выдумывай от себя\\n"\n'
        ')\n',
        encoding="utf-8",
    )
    assert _offenders(planted), "проверка не заметила явно вшитый промпт — тест бесполезен"
