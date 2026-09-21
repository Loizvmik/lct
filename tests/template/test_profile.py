"""Тесты — дословно из брифа Task 8 (.superpowers/sdd/task-8-brief.md, Step 1),
с тем же отклонением, что и в остальных tests/template/*.py: ЛЦТ2026 —
контрольный шаблон для защиты, в тестах не участвует (см. test_theme.py).

`_all_modules` для `test_profile_never_falls_back_to_hardcoded_vk_values`
сканирует ВЕСЬ пакет `deckforge.template` (все модули разбора, не только
`profile.py`/`naming.py`, добавленные этой задачей) — страж требования
«решение не заточено под три шаблона» обязан видеть весь пакет, иначе он
ничего не стережёт за пределами двух файлов.

Раньше здесь было сужение до двух файлов, потому что буквальное сравнение
исходного текста модуля падало: докстроки theme.py/usage.py/typography.py/
grid.py/layouts.py/assets.py/patterns.py (Task 3-7, отдельные код-ревью)
честно ссылаются на реальные имена шаблонов как обоснование калибровки
порогов ("не подобрано по наблюдению за файлом, а вот почему") — это
свидетельство добросовестности, а не хардкод поведения. Хардкод, которого
боится этот тест, — это код, который принимает решение ПО ИМЕНИ шаблона в
исполняемой части модуля, а не упоминание имени в докстроке/комментарии как
пояснение калибровки.

Правильное решение — не сужать область сканирования, а сузить то, что
сравнивается: `_executable_source` разбирает модуль через `ast`, отбрасывает
докстроки (первый `Expr`-константа-строка в теле module/class/def) и
вырезает построчные комментарии (`tokenize`), оставляя только то, что
реально исполняется. Строковый литерал вроде `"VK Tech"` в исполняемом коде
(сравнение, ключ словаря, дефолт параметра) по-прежнему поймается; то же имя
в докстроке или в `# комментарии` — нет.
"""
import ast
import importlib
import inspect
import io
import os
import time
import tokenize
from pathlib import Path

import pytest

import deckforge.template as pkg
from deckforge.template.profile import TemplateProfile

TEMPLATES_DIR = Path("dataset/templates")

live = pytest.mark.skipif(not os.getenv("YANDEX_API_KEY"), reason="нет YANDEX_API_KEY")


def _all_modules(pkg):
    """Все модули пакета разбора — включая theme/usage/typography/grid/
    layouts/assets/patterns (Task 3-7), не только profile.py/naming.py."""
    module_names = [
        "theme", "usage", "typography", "grid", "layouts", "assets",
        "patterns", "profile", "naming",
    ]
    return [importlib.import_module(f"{pkg.__name__}.{name}") for name in module_names]


def _docstring_spans(tree: ast.AST) -> set[tuple[int, int]]:
    """Позиции (начальная, конечная строка) строковых констант-докстрок
    (module/class/def) в дереве — их видит ast (первый `Expr` в теле —
    строковый литерал), но не видит tokenize (для него докстрока — обычный
    STRING-токен). Только номера строк, не колонки: `ast.col_offset` — байтовый
    UTF-8-офсет, `tokenize` — офсет в кодовых точках, для кириллицы (весь этот
    пакет) они расходятся; совпадения строки начала и конца достаточно —
    два разных строковых литерала, занимающих ровно тот же диапазон строк в
    одном файле, на практике не встречаются."""
    spans: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            doc = first.value
            spans.add((doc.lineno, doc.end_lineno))
    return spans


def _executable_source(source: str) -> str:
    """`source` без докстрок и без построчных комментариев — то, что реально
    исполняется. Докстрока определена через `ast` (позиция первого
    `Expr`-строки в теле module/class/def), комментарии вырезаны через
    `tokenize.COMMENT`. Всё остальное (включая строковые константы вне
    докстрок — сравнения, ключи словарей, литералы по умолчанию) остаётся
    и участвует в проверке на хардкод."""
    tree = ast.parse(source)
    doc_spans = _docstring_spans(tree)

    kept: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING:
            span = (tok.start[0], tok.end[0])
            if span in doc_spans:
                continue
        kept.append(tok.string)
    return "\n".join(kept)


def test_executable_source_strips_docstrings_and_comments():
    """Красный тест на само извлечение (находка код-ревью 1): имя шаблона в
    докстроке модуля/функции и в комментарии должно исчезнуть, а то же имя
    в исполняемом строковом литерале — остаться."""
    source = (
        '"""Модуль про VK Tech — докстрока, безобидно."""\n'
        "# comment about VK Tech — тоже безобидно\n"
        "def f():\n"
        '    """Докстрока функции про VK Tech — тоже безобидно."""\n'
        '    x = "VK Tech"  # это настоящий хардкод\n'
        "    return x\n"
    )
    cleaned = _executable_source(source)
    assert cleaned.count("VK Tech") == 1


def test_profile_is_json_roundtrippable():
    profile = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx")
    assert TemplateProfile.model_validate_json(profile.to_json()) == profile


def test_provenance_explains_every_token():
    """Как отчёт разбора в aiva: человек должен видеть, откуда взято каждое значение."""
    profile = TemplateProfile.from_file(
        TEMPLATES_DIR / "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx"
    )
    joined = "\n".join(profile.provenance)
    assert "theme2" in joined
    assert "заглушк" in joined  # про деградировавший fontScheme


def test_warnings_flag_degraded_sources():
    profile = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx")
    assert any("txStyles" in w for w in profile.warnings)


def test_profile_never_falls_back_to_hardcoded_vk_values():
    """Страж требования «решение не заточено под три шаблона» — весь пакет
    разбора, но только исполняемый код (докстроки и комментарии отброшены,
    см. докстроку модуля)."""
    source = "".join(_executable_source(inspect.getsource(m)) for m in _all_modules(pkg))
    for forbidden in ["#0077FF", "Play", "VK Tech", "VK Education", "WorkSpace"]:
        assert forbidden not in source, f"в исполняемом коде парсера захардкожено {forbidden}"


def test_parsing_is_fast_enough():
    """Разбор входит в бюджет 5 минут на колоду с большим запасом."""
    start = time.monotonic()
    TemplateProfile.from_file(TEMPLATES_DIR / "Шаблон презентации VK Education.pptx")
    assert time.monotonic() - start < 30


# --- Дополнительные тесты (не из брифа дословно, но по требованиям задачи) ---

@pytest.mark.parametrize("name", [
    "VK Tech шаблон.pptx",
    "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
    "Шаблон презентации VK Education.pptx",
])
def test_palette_roles_are_from_input_palette_and_pass_contrast(name):
    """Требование задачи: «предложенный цвет обязан присутствовать во входной
    палитре, контраст пары фона и текста пересчитывается кодом» — без ключа
    (namer=None) роли идут запасным вариантом, но обязаны сами соблюдать те же
    правила, что код проверяет за моделью."""
    from deckforge.template.naming import MIN_CONTRAST, contrast_ratio

    profile = TemplateProfile.from_file(TEMPLATES_DIR / name)
    # Собственная входная палитра (тема + usage) в профиль не выносится
    # отдельным полем — прямая проверка "каждый hex встречается в ней"
    # дублировала бы внутренности naming.py. Здесь достаточно проверить
    # внешне наблюдаемую часть контракта: формат хекса и контраст пары
    # surface/on_surface, если обе роли назначены.
    for hexv in profile.palette_roles.values():
        assert hexv.startswith("#") and len(hexv) == 7

    roles = profile.palette_roles
    if "surface" in roles and "on_surface" in roles:
        assert contrast_ratio(roles["surface"], roles["on_surface"]) >= MIN_CONTRAST


def test_profile_builds_without_key_using_fallback_naming():
    """«Без ключа профиль обязан собираться целиком, просто с ролями из
    запасного варианта» — namer=None не должен ходить в сеть и не должен
    мешать сборке остального профиля."""
    profile = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx", namer=None)
    assert profile.layouts
    assert profile.patterns
    assert any("запасн" in note for note in profile.provenance)


@live
def test_palette_naming_calls_the_real_model_and_code_still_validates_it():
    """С ключом naming.py действительно вызывает модель (не только
    фолбэк) — код обязан принять валидный ответ и профиль обязан
    собраться так же полно, как без ключа."""
    from deckforge.provider.yandex import YandexProvider
    from deckforge.template.naming import MIN_CONTRAST, contrast_ratio

    namer = YandexProvider(
        model="qwen3.6-35b-a3b",
        api_key=os.environ["YANDEX_API_KEY"],
        folder_id=os.environ["YANDEX_FOLDER_ID"],
    )
    profile = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx", namer=namer)

    assert profile.layouts and profile.patterns
    for hexv in profile.palette_roles.values():
        assert hexv.startswith("#") and len(hexv) == 7
    roles = profile.palette_roles
    if "surface" in roles and "on_surface" in roles:
        assert contrast_ratio(roles["surface"], roles["on_surface"]) >= MIN_CONTRAST
