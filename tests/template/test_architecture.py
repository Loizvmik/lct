"""Архитектурная граница пакета `deckforge.template` (Task 18): ровно два
модуля реально зовут модель — `naming.py` (роли палитры) и, с этой задачи,
`vision_kind.py` (вид раскладки по картинке слайда-примера, см. докстроки
`deckforge/template/__init__.py` и `template/patterns.py`). Раньше (до Task
18) граница была уже — только `naming.py` — и была заявлена докстрокой
пакета, но не проверялась тестом; эта задача расширяет границу явно и, по
условию задачи ("сделай это явно... тест обновлён"), заводит её первую
автоматическую проверку, а не молчаливое обещание в докстроке.

Модуль `LLMProvider`/`VisionProvider` (`provider/base.py`) — абстрактные
классы РОВНО с двумя методами, `complete`/`ask_image` (обращение к модели
всегда идёт через один из них, весь остальной код проекта — детерминированная
обвязка вокруг ответа). Проверка ниже поэтому не гоняется за импортами типов
(любой модуль вправе принять `llm: LLMProvider | None` параметром — `profile.
py` делает именно это ради сигнатуры `from_file`, не вызывая модель сам, см.
её докстроку), а ищет ВЫЗОВЫ `<что угодно>.complete(...)`/`<что угодно>.
ask_image(...)` буквально — это единственные две операции, которыми
вообще можно "позвать модель" в этом проекте, и они не встречаются в коде
ни для чего другого (сторонние библиотеки внутри `deckforge.template` их не
используют — см. `test_the_check_itself_catches_a_planted_model_call`)."""
from __future__ import annotations
import ast
from pathlib import Path

TEMPLATE_PKG = Path("src/deckforge/template")

# Модули, которым архитектурно РАЗРЕШЕНО реально вызывать модель — см.
# докстроку модуля.
_ALLOWED_CALLERS = {"naming.py", "vision_kind.py"}

# Единственные две операции, которыми проект вообще обращается к модели
# (`provider/base.py::LLMProvider.complete`/`VisionProvider.ask_image`).
_MODEL_CALL_METHODS = frozenset({"complete", "ask_image"})


def _model_call_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MODEL_CALL_METHODS
    ]


def test_only_naming_and_vision_kind_call_the_model():
    offenders = {}
    for path in sorted(TEMPLATE_PKG.glob("*.py")):
        if path.name == "__init__.py" or path.name in _ALLOWED_CALLERS:
            continue
        lines = _model_call_lines(path)
        if lines:
            offenders[path.name] = lines
    assert not offenders, (
        f"эти модули deckforge.template зовут .complete()/.ask_image() за пределами "
        f"архитектурной границы: {offenders} — докстрока пакета разрешает это только "
        f"{sorted(_ALLOWED_CALLERS)}"
    )


def test_naming_and_vision_kind_do_actually_call_the_model():
    """Тест теста: если бы оба разрешённых модуля перестали реально звать
    модель (переехали на что-то другое, не тронув список выше), проверка
    выше молчала бы зря, ничего больше не стерегя — убеждаемся, что оба
    числящихся в `_ALLOWED_CALLERS` модуля реально это делают."""
    for name in _ALLOWED_CALLERS:
        assert _model_call_lines(TEMPLATE_PKG / name), f"{name}: ожидался реальный вызов .complete()/.ask_image()"


def test_the_check_itself_catches_a_planted_model_call(tmp_path):
    """Тест теста (парный к предыдущему, с другой стороны): если бы модуль
    ВНЕ разрешённого списка правда позвал модель, проверка это бы заметила."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def f(llm):\n    return llm.complete([{'role': 'user', 'content': 'x'}])\n",
        encoding="utf-8",
    )
    assert _model_call_lines(planted), "проверка не заметила явный вызов .complete() — тест бесполезен"
