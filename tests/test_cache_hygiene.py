"""Прогон тестов не имеет права трогать общий диск-кеш профилей.

Фикстура `tests/conftest.py::_isolate_default_profile_cache` уводит кеш во
временный каталог, но она — фикстура, а фикстуры выполняются ПОСЛЕ импорта
тестовых модулей. Разбор шаблона в шапке файла (`PROFILE = TemplateProfile.
from_file(TEMPLATE)`) случается на импорте, до неё, и потому читает
настоящий `config/app.yaml` и пишет в настоящий `cache/profiles/` рабочего
дерева.

Это не теория: 23 сентября 2026 в общий кеш лёг профиль, собранный тестовым
прогоном БЕЗ ключа модели (виды раскладок — только геометрия), а живой
запуск пользователя через веб-интерфейс подобрал его по совпадению
содержимого шаблона и собрал презентацию по недоразобранному шаблону.
Ключ кеша считается от байтов .pptx, так что отравленная запись годилась
для любого запуска и жила до ручной чистки.

Проверка ниже держит границу: вызов `from_file` ВНЕ функции обязан передать
`cache_dir` явно. Вызовы внутри функций и фикстур не проверяются — до них
autouse-фикстура уже отработала (в `tests/template/test_profile.py` есть
тесты, которые как раз проверяют поведение кеша по умолчанию, и запрещать
им вызов без `cache_dir` было бы запретом проверять то, ради чего они
написаны)."""
from __future__ import annotations
import ast
from pathlib import Path

TESTS_DIR = Path("tests")


def _module_level_from_file_calls(path: Path) -> list[int]:
    """Строки вызовов `*.from_file(...)` без `cache_dir`, лежащих вне любой
    функции — то есть исполняемых на импорте модуля."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    inside_function: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    inside_function.add(id(inner))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_file"
        and id(node) not in inside_function
        and not any(kw.arg == "cache_dir" for kw in node.keywords)
    ]


def test_no_test_module_parses_a_template_into_the_shared_cache():
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        offenders.extend(f"{path}:{line}" for line in _module_level_from_file_calls(path))
    assert not offenders, (
        "разбор шаблона на уровне модуля обязан идти с явным `cache_dir` — иначе прогон тестов "
        f"пишет в общий cache/profiles/ рабочего дерева: {', '.join(offenders)}"
    )


def test_the_check_itself_catches_a_planted_call(tmp_path):
    """Проверка проверки — тем же приёмом, что `tests/template/
    test_architecture.py::test_the_check_itself_catches_a_planted_model_call`:
    без неё тест выше зеленел бы и на сломанном обходе AST."""
    planted = tmp_path / "test_planted.py"
    planted.write_text(
        "from deckforge.template.profile import TemplateProfile\n"
        "PROFILE = TemplateProfile.from_file('x.pptx')\n",
        encoding="utf-8",
    )
    assert _module_level_from_file_calls(planted) == [2]


def test_a_call_inside_a_function_is_not_flagged(tmp_path):
    """Вызов внутри функции фикстура уже прикрывает — ловить его значит
    запретить тестам кеша проверять поведение по умолчанию."""
    ok = tmp_path / "test_ok.py"
    ok.write_text(
        "from deckforge.template.profile import TemplateProfile\n"
        "def _profile():\n"
        "    return TemplateProfile.from_file('x.pptx')\n",
        encoding="utf-8",
    )
    assert _module_level_from_file_calls(ok) == []
