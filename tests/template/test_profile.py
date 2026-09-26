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
from dataclasses import replace
from pathlib import Path

import pytest

import deckforge.template as pkg
from deckforge.provider.base import LLMProvider, VisionProvider
from deckforge.render.soffice import RenderError
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


# --- Диск-кеш профиля по отпечатку файла (Task 8 код-ревью, находка 2) ---


def test_from_file_writes_a_cache_file_keyed_by_fingerprint(tmp_path):
    cache_dir = tmp_path / "cache"
    profile = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx", cache_dir=cache_dir)

    cache_file = cache_dir / f"{profile.fingerprint}.json"
    assert cache_file.exists()
    assert TemplateProfile.model_validate_json(cache_file.read_text(encoding="utf-8")) == profile


def test_from_file_accepts_cache_dir_as_a_plain_string_not_only_a_path(tmp_path):
    """Найдено этой задачей: `cache_dir` документирован как `Path | None`,
    но Python не приводит аргументы к аннотации сама — до фикса
    `cache_dir="строка"` падал `TypeError: unsupported operand type(s) for
    /: 'str' and 'str'` на первой же попытке собрать путь к файлу кеша
    (`effective_cache_dir / f"{fingerprint}.json"`), а не честно работал с
    любым путём, как обещает сигнатура."""
    cache_dir_str = str(tmp_path / "cache")
    profile = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx", cache_dir=cache_dir_str)

    cache_file = Path(cache_dir_str) / f"{profile.fingerprint}.json"
    assert cache_file.exists()
    assert TemplateProfile.model_validate_json(cache_file.read_text(encoding="utf-8")) == profile


def test_from_file_second_call_uses_cache_without_reparsing(tmp_path, monkeypatch):
    """Оркестратор генерации будет дёргать from_file на каждую колоду —
    повторный вызов на том же файле не должен снова обходить пакет и снова
    звать модель (fingerprint не меняется, файл на диске не менялся)."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("collect_usage вызван на закешированном профиле — кеш не сработал")

    monkeypatch.setattr("deckforge.template.profile.collect_usage", _must_not_be_called)

    second = TemplateProfile.from_file(path, cache_dir=cache_dir)
    assert second == first


def test_corrupted_cache_file_triggers_a_fresh_parse_instead_of_crashing(tmp_path):
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)
    cache_file = cache_dir / f"{first.fingerprint}.json"
    cache_file.write_text("это не json профиля", encoding="utf-8")

    second = TemplateProfile.from_file(path, cache_dir=cache_dir)
    assert second == first
    # Повреждённый кеш обязан быть перезаписан валидным профилем, а не
    # оставлен битым для следующего вызова.
    assert TemplateProfile.model_validate_json(cache_file.read_text(encoding="utf-8")) == first


def test_stale_cache_with_an_older_schema_version_triggers_a_fresh_parse(tmp_path, monkeypatch):
    """Task 10 отчёт, находка №5: диск-кеш ключуется ТОЛЬКО отпечатком
    файла — правка, добавляющая новое поле в модель профиля, не меняет
    отпечаток .pptx, а старый файл кеша это поле не несёт (pydantic тихо
    подставил бы дефолт при чтении). Кеш обязан нести версию СХЕМЫ профиля
    (`PROFILE_SCHEMA_VERSION`) и признавать себя недействительным, когда
    версия в файле кеша не совпадает с текущей — тогда правка на поле
    ловится сразу, без ручной чистки `cache/profiles/`."""
    from deckforge.template import profile as profile_module

    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)
    cache_file = cache_dir / f"{first.fingerprint}.json"
    import json as _json
    payload = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload.get("schema_version") == profile_module.PROFILE_SCHEMA_VERSION
    payload["schema_version"] = -1  # версия заведомо старее любой реальной
    cache_file.write_text(_json.dumps(payload), encoding="utf-8")

    calls = []
    real_collect_usage = profile_module.collect_usage

    def _tracking_collect_usage(*args, **kwargs):
        calls.append(1)
        return real_collect_usage(*args, **kwargs)

    monkeypatch.setattr("deckforge.template.profile.collect_usage", _tracking_collect_usage)

    second = TemplateProfile.from_file(path, cache_dir=cache_dir)
    assert calls, "кеш со старой версией схемы принят как валидный — разбор не повторился"
    assert second == first, "пересчитанный профиль обязан совпасть с исходным (тот же файл, тот же код)"

    rewritten = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == profile_module.PROFILE_SCHEMA_VERSION, (
        "кеш обязан быть перезаписан текущей версией схемы, а не оставлен со старой"
    )


def test_cache_missing_schema_version_entirely_is_also_treated_as_stale(tmp_path):
    """Тот же брак, что и выше, но для кеша, записанного ДО того, как в
    модель вообще добавили поле `schema_version` (реальный сценарий отчёта:
    "предыдущая правка добавила в модель новые поля, и старый кеш их не
    несёт") — поля нет в файле вовсе, не просто устаревшее значение."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)
    cache_file = cache_dir / f"{first.fingerprint}.json"
    import json as _json
    payload = _json.loads(cache_file.read_text(encoding="utf-8"))
    del payload["schema_version"]
    cache_file.write_text(_json.dumps(payload), encoding="utf-8")

    second = TemplateProfile.from_file(path, cache_dir=cache_dir)
    assert second == first


def test_different_files_get_independent_cache_entries(tmp_path):
    cache_dir = tmp_path / "cache"
    vktech = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx", cache_dir=cache_dir)
    education = TemplateProfile.from_file(
        TEMPLATES_DIR / "Шаблон презентации VK Education.pptx", cache_dir=cache_dir,
    )
    assert vktech.fingerprint != education.fingerprint
    assert (cache_dir / f"{vktech.fingerprint}.json").exists()
    assert (cache_dir / f"{education.fingerprint}.json").exists()


def test_default_cache_dir_is_read_from_app_yaml(monkeypatch):
    """`tests/conftest.py` изолирует дефолт диск-кеша, подменяя
    `APP_YAML_PATH` временным конфигом для ВСЕХ тестов (чтобы не писать в
    реальный `cache/` репозитория при каждом прогоне) — здесь конкретно этот
    тест возвращает `APP_YAML_PATH` на настоящий `config/app.yaml`, чтобы
    проверить настоящую логику чтения, а не подмену."""
    from deckforge.settings import Settings
    from deckforge.template import profile as profile_module

    real_app_yaml = Path(__file__).resolve().parents[2] / "config" / "app.yaml"
    monkeypatch.setattr(profile_module, "APP_YAML_PATH", real_app_yaml)

    assert profile_module._default_cache_dir() == Settings.load(real_app_yaml).paths.profile_cache


def test_default_cache_dir_is_none_when_config_is_unreadable(monkeypatch):
    from deckforge.template import profile as profile_module

    monkeypatch.setattr(profile_module, "APP_YAML_PATH", Path("/does/not/exist/app.yaml"))
    assert profile_module._default_cache_dir() is None


# --- Кеш не путает запасной вариант ролей палитры с ответом модели (Task 8
# код-ревью, находка 2): ключ кеша — только отпечаток файла, без учёта
# namer, поэтому профиль, закешированный без ключа (роли — запасным
# вариантом), после появления ключа должен быть переназначен моделью, а не
# отдан из кеша как есть навсегда. `name_palette_roles_report` подменяется
# заглушкой (тот же приём, что и в test_from_file_second_call_uses_cache_
# without_reparsing выше для collect_usage) — поведение зависит только от
# того, `llm is None` или нет, ровно как у настоящей функции; сама сеть
# здесь не нужна, проверяется логика profile.py, а не naming.py. ---


class _FakeNamer(LLMProvider):
    """Никогда не вызывается по-настоящему — name_palette_roles_report
    подменяется заглушкой ниже, которая решает по `llm is None`. Нужен
    только как typed-корректный «ключ доступен» сигнал для from_file."""

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        raise AssertionError("FakeNamer.complete не должен вызываться в этих тестах")


def _patch_palette_naming(monkeypatch, *, fallback_roles=None, model_roles=None):
    """Подменяет `deckforge.template.profile.name_palette_roles_report`:
    без llm — фиксированные «запасные» роли (source=fallback), с llm —
    фиксированные «модельные» роли (source=model). Возвращает список
    вызовов (для проверки, что на кеш-хите без нужды переназначения роль
    именования вовсе не дёргается)."""
    from deckforge.template.naming import PaletteNamingResult, PaletteNote

    fallback_roles = fallback_roles or {"brand": "#111111"}
    model_roles = model_roles or {"brand": "#222222"}
    calls: list[bool] = []

    def fake(usage, theme, llm):
        calls.append(llm is not None)
        if llm is None:
            return PaletteNamingResult(
                roles=dict(fallback_roles),
                notes=[PaletteNote("запасной вариант (тест)", severity="info")],
                source="fallback",
            )
        return PaletteNamingResult(
            roles=dict(model_roles),
            notes=[PaletteNote("роли назначены моделью (тест)", severity="info")],
            source="model",
        )

    monkeypatch.setattr("deckforge.template.profile.name_palette_roles_report", fake)
    return calls


def test_cache_hit_with_fallback_roles_and_available_model_reassigns_roles(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    calls = _patch_palette_naming(monkeypatch)

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)  # namer=None -> fallback
    assert first.palette_roles == {"brand": "#111111"}

    second = TemplateProfile.from_file(path, cache_dir=cache_dir, namer=_FakeNamer())
    assert second.palette_roles == {"brand": "#222222"}
    assert calls == [False, True]  # первый вызов без llm, второй — с ним

    # Остальной разбор не переделан — те же лейауты/паттерны, что в первом
    # профиле (детерминированная часть кеша не трогается переназначением).
    assert second.layouts == first.layouts
    assert second.patterns == first.patterns

    # Кеш на диске обновлён — следующий вызов без изменения конфигурации
    # больше не должен опять переназначать.
    cache_file = cache_dir / f"{first.fingerprint}.json"
    reread = TemplateProfile.model_validate_json(cache_file.read_text(encoding="utf-8"))
    assert reread.palette_roles == {"brand": "#222222"}


def test_cache_hit_with_model_roles_and_no_key_returns_as_is(tmp_path, monkeypatch):
    """Обратный случай: профиль с ролями от модели не деградирует до
    запасного варианта, если следующий вызов идёт без ключа."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    calls = _patch_palette_naming(monkeypatch)

    first = TemplateProfile.from_file(path, cache_dir=cache_dir, namer=_FakeNamer())
    assert first.palette_roles == {"brand": "#222222"}

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("collect_usage вызван без нужды переназначать роли")

    monkeypatch.setattr("deckforge.template.profile.collect_usage", _must_not_be_called)

    second = TemplateProfile.from_file(path, cache_dir=cache_dir, namer=None)
    assert second.palette_roles == {"brand": "#222222"}  # не деградировал
    assert second == first
    assert calls == [True]  # второй вызов вообще не дошёл до name_palette_roles_report


def test_cache_hit_with_model_roles_and_same_key_does_not_touch_network(tmp_path, monkeypatch):
    """Повторный вызов с той же конфигурацией (модель уже доступна и уже
    была использована) не трогает сеть — роли уже от модели, переназначать
    нечего."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    calls = _patch_palette_naming(monkeypatch)
    namer = _FakeNamer()

    first = TemplateProfile.from_file(path, cache_dir=cache_dir, namer=namer)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("collect_usage вызван на уже модельном кеш-хите")

    monkeypatch.setattr("deckforge.template.profile.collect_usage", _must_not_be_called)

    second = TemplateProfile.from_file(path, cache_dir=cache_dir, namer=namer)
    assert second == first
    assert calls == [True]  # ровно один реальный вызов именования, не два


# --- Кеш не путает геометрическое предположение о виде раскладки с ответом
# модели. Та же болезнь, что вылечена выше для ролей палитры, и тот же
# способ лечения — признак происхождения внутри профиля
# (`pattern_kinds_source`), а не в ключе кеша. Найдено на живом прогоне:
# профиль ЛЦТ2026, записанный в общий `cache/profiles/` прогоном БЕЗ ключа,
# потом отдавался запросам с ключом — восемь из пятнадцати раскладок
# оставались `bullets` с `kind_confidence=0.3` (корзина по умолчанию
# `patterns._classify_kind`, обязанная по порогу `vision_kind._ASK_
# CONFIDENCE_THRESHOLD = 0.5` уйти на уточнение моделью). ---


class _FakeVision(VisionProvider):
    """Никогда не вызывается по-настоящему: `classify_patterns_by_vision`
    подменяется заглушкой, которая решает по `llm is None`. Нужен только как
    typed-корректный сигнал «ключ vision доступен» для from_file (тот же
    приём, что и `_FakeNamer` выше)."""

    def ask_image(self, png, prompt, *, max_tokens=1024) -> str:
        raise AssertionError("FakeVision.ask_image не должен вызываться в этих тестах")


_VISION_SUMMARY = "Вид раскладки: вид уточнён моделью (тест)."
_VISION_SKIP_NOTE = "p-тест: слайд-источник не нашёлся — вид оставлен геометрическим (тест)."


def _patch_vision_kind(monkeypatch, *, model_kind="quote"):
    """Подменяет `deckforge.template.profile.classify_patterns_by_vision`:
    без llm — паттерны как есть и ни одной заметки (ровно как настоящая
    функция), с llm — всем паттернам проставлен `model_kind` плюс сводка и
    заметка об отказе. Возвращает список вызовов, чтобы проверять, что на
    кеш-хите без нужды дозапроса модель не трогается вовсе."""
    calls: list[bool] = []

    def fake(patterns, template_path, llm, **kwargs):
        calls.append(llm is not None)
        if llm is None:
            return list(patterns), []
        return (
            [replace(p, kind=model_kind) for p in patterns],
            [_VISION_SUMMARY, _VISION_SKIP_NOTE],
        )

    monkeypatch.setattr("deckforge.template.profile.classify_patterns_by_vision", fake)
    return calls


def _vision_provenance_lines(profile):
    return [
        line for line in profile.provenance
        if line.startswith(("Вид раскладки", "Виды раскладки"))
    ]


def test_cache_hit_with_geometric_kinds_and_available_vision_reclassifies(tmp_path, monkeypatch):
    """Главный случай находки: профиль, собранный БЕЗ ключа модели, не
    удовлетворяет запрос, у которого ключ есть."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    calls = _patch_vision_kind(monkeypatch)

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)  # vision=None -> только геометрия
    assert first.pattern_kinds_source == "geometry"
    assert {p.kind for p in first.patterns} != {"quote"}

    second = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=_FakeVision())
    assert calls == [False, True]  # первый разбор без модели, второй — с ней
    assert second.pattern_kinds_source == "model"
    assert [p.kind for p in second.patterns] == ["quote"] * len(first.patterns)

    # Детерминированная часть разбора не потеряна и не переписана: те же
    # лейауты, та же палитра, те же паттерны с точностью до вида.
    assert second.layouts == first.layouts
    assert second.palette_roles == first.palette_roles
    assert second.palette_roles_source == first.palette_roles_source
    assert [p.pattern_id for p in second.patterns] == [p.pattern_id for p in first.patterns]
    assert [p.slots for p in second.patterns] == [p.slots for p in first.patterns]

    # Отчёт «откуда что взято» не задваивается: строка про вид раскладки
    # ровно одна и теперь это сводка модели, а не «моделью не уточнялся».
    assert _vision_provenance_lines(second) == [_VISION_SUMMARY]
    assert _VISION_SKIP_NOTE in second.warnings

    # Кеш на диске обновлён — следующий вызов с той же конфигурацией ничего
    # не дозапрашивает.
    cache_file = cache_dir / f"{first.fingerprint}.json"
    reread = TemplateProfile.model_validate_json(cache_file.read_text(encoding="utf-8"))
    assert reread == second


def test_cache_hit_with_model_kinds_and_no_vision_returns_as_is(tmp_path, monkeypatch):
    """Обратный случай: профиль, собранный С моделью, переиспользуется
    всегда — вызов без ключа отдаёт сохранённые виды, а не откатывает их к
    геометрии. Деградация честная: перечитывать нечего."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    calls = _patch_vision_kind(monkeypatch)

    first = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=_FakeVision())
    assert first.pattern_kinds_source == "model"

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("шаблон перемайнен без нужды дозапрашивать виды")

    monkeypatch.setattr("deckforge.template.profile.mine_patterns", _must_not_be_called)

    second = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=None)
    assert second == first
    assert calls == [True]  # второй вызов вообще не дошёл до классификации


def test_cache_hit_with_model_kinds_and_same_vision_does_not_ask_again(tmp_path, monkeypatch):
    """Повторный вызов с тем же ключом не гоняет рендер и модель заново —
    виды уже уточнены, дозапрашивать нечего."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    calls = _patch_vision_kind(monkeypatch)
    vision = _FakeVision()

    first = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=vision)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("шаблон перемайнен на уже модельном кеш-хите")

    monkeypatch.setattr("deckforge.template.profile.mine_patterns", _must_not_be_called)

    second = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=vision)
    assert second == first
    assert calls == [True]  # ровно одно обращение к классификации, не два


def test_cache_hit_needing_both_roles_and_kinds_refreshes_both_once(tmp_path, monkeypatch):
    """Профиль без ключа вовсе: при появлении обоих ключей дозапрашиваются и
    роли палитры, и виды раскладок, а в кеш ложится итог, а не промежуточное
    состояние."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    _patch_palette_naming(monkeypatch)
    _patch_vision_kind(monkeypatch)

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)
    assert (first.palette_roles_source, first.pattern_kinds_source) == ("fallback", "geometry")

    second = TemplateProfile.from_file(
        path, cache_dir=cache_dir, namer=_FakeNamer(), vision=_FakeVision(),
    )
    assert second.palette_roles == {"brand": "#222222"}
    assert second.palette_roles_source == "model"
    assert second.pattern_kinds_source == "model"

    cache_file = cache_dir / f"{first.fingerprint}.json"
    reread = TemplateProfile.model_validate_json(cache_file.read_text(encoding="utf-8"))
    assert reread == second


# --- Задача C: превью PNG на каждый паттерн шаблона в кэше профиля ---------

def _fake_to_pngs(monkeypatch, *, page_bytes: dict[int, bytes] | None = None):
    """Подменяет `deckforge.template.profile.to_pngs` — пишет по одному
    файлу-заглушке на каждую запрошенную страницу, в том же порядке, что и
    `pages` (тот же контракт, что настоящий `render.soffice.to_pngs`
    документирует для `_save_pattern_previews`: по одному PNG на страницу,
    по возрастанию номера). Настоящий soffice/poppler не трогается —
    контролируем только факт записи и порядок, не байты картинки."""
    calls: list[dict] = []

    def _fake(template_path, out_dir, dpi=110, pages=None):
        calls.append({"template_path": Path(template_path), "dpi": dpi, "pages": list(pages or [])})
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for page in pages or []:
            content = (page_bytes or {}).get(page, f"PNG-страница-{page}".encode())
            dest = out_dir / f"slide-{page}.png"
            dest.write_bytes(content)
            result.append(dest)
        return result

    monkeypatch.setattr("deckforge.template.profile.to_pngs", _fake)
    return calls


def test_preview_is_not_rendered_without_a_vision_key(tmp_path, monkeypatch):
    """Задача C, честная деградация: без ключа `vision` рендер шаблона не
    нужен ничему другому в `from_file` — заводить его ТОЛЬКО ради превью
    значило бы платить рендером там, где сегодня не платится ничего
    (`test_parsing_is_fast_enough` рассчитывает именно на это)."""
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("to_pngs вызван без ключа vision — превью не должны рендериться")

    monkeypatch.setattr("deckforge.template.profile.to_pngs", _must_not_be_called)
    cache_dir = tmp_path / "cache"

    profile = TemplateProfile.from_file(TEMPLATES_DIR / "VK Tech шаблон.pptx", cache_dir=cache_dir)

    assert profile.patterns
    assert all(p.preview_path is None for p in profile.patterns)


def test_preview_saved_next_to_the_profile_when_vision_is_available(tmp_path, monkeypatch):
    """Главный случай задачи: с ключом `vision` каждый паттерн получает PNG
    первого исходного слайда рядом с JSON профиля в кеше."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    _patch_vision_kind(monkeypatch)  # классификация видов не ходит в сеть/рендер
    calls = _fake_to_pngs(monkeypatch)

    profile = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=_FakeVision())

    assert profile.patterns
    assert calls, "to_pngs не был вызван, хотя ключ vision передан"
    for pattern in profile.patterns:
        assert pattern.preview_path == f"previews/{pattern.pattern_id}.png"
        on_disk = profile.pattern_preview_path(pattern, cache_dir=cache_dir)
        assert on_disk == cache_dir / profile.fingerprint / "previews" / f"{pattern.pattern_id}.png"
        assert on_disk.exists()
        assert on_disk.read_bytes()  # не пустой файл


def test_render_failure_leaves_preview_path_none_without_crashing(tmp_path, monkeypatch):
    """Задача C, п.3: нет soffice/poppler (или рендер иначе упал) — разбор
    профиля не падает, превью просто отсутствуют."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    _patch_vision_kind(monkeypatch)

    def _boom(*args, **kwargs):
        raise RenderError("soffice недоступен (тест)")

    monkeypatch.setattr("deckforge.template.profile.to_pngs", _boom)

    profile = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=_FakeVision())

    assert profile.patterns  # разбор целиком не пострадал
    assert all(p.preview_path is None for p in profile.patterns)


def test_cache_hit_reads_back_preview_paths_without_rerendering(tmp_path, monkeypatch):
    """Кеш с превью читается обратно — второй вызов с той же конфигурацией
    не трогает рендер повторно, а превью из первого прогона сохраняются."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    _patch_vision_kind(monkeypatch)
    vision = _FakeVision()
    _fake_to_pngs(monkeypatch)

    first = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=vision)
    assert first.patterns and all(p.preview_path is not None for p in first.patterns)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("превью перерендерены на уже модельном кеш-хите")

    monkeypatch.setattr("deckforge.template.profile.to_pngs", _must_not_be_called)

    second = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=vision)
    assert second == first
    assert [p.preview_path for p in second.patterns] == [p.preview_path for p in first.patterns]

    cache_file = cache_dir / f"{first.fingerprint}.json"
    reread = TemplateProfile.model_validate_json(cache_file.read_text(encoding="utf-8"))
    assert [p.preview_path for p in reread.patterns] == [p.preview_path for p in first.patterns]


def test_reclassify_on_cache_hit_also_renders_previews(tmp_path, monkeypatch):
    """Профиль лёг в кеш без ключа `vision` (превью тоже нет — см. тест
    выше), следующий вызов с ключом дозапрашивает и виды, и превью, а не
    оставляет их `None` до следующего разбора с нуля."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    _patch_vision_kind(monkeypatch)

    first = TemplateProfile.from_file(path, cache_dir=cache_dir)  # vision=None
    assert all(p.preview_path is None for p in first.patterns)

    _fake_to_pngs(monkeypatch)
    second = TemplateProfile.from_file(path, cache_dir=cache_dir, vision=_FakeVision())

    assert second.pattern_kinds_source == "model"
    assert second.patterns
    assert all(p.preview_path is not None for p in second.patterns)


def test_pattern_preview_path_is_none_without_a_saved_preview_or_cache_dir(tmp_path, monkeypatch):
    """`pattern_preview_path` не выдумывает файл, которого нет: ни когда у
    паттерна `preview_path is None`, ни когда каталог кеша выключен."""
    cache_dir = tmp_path / "cache"
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"

    profile = TemplateProfile.from_file(path, cache_dir=cache_dir)  # vision=None -> нет превью
    pattern = profile.patterns[0]
    assert pattern.preview_path is None
    assert profile.pattern_preview_path(pattern, cache_dir=cache_dir) is None
    assert profile.pattern_preview_path(pattern, cache_dir=None) is None



def test_source_shape_ids_survive_the_json_roundtrip():
    """Id исходных фигур слотов и декора доходят через JSON кеша до
    сборки: без них клон снова угадывает фигуру по коробке."""
    profile = TemplateProfile.from_file(TEMPLATES_DIR / "Шаблон презентации VK Education.pptx", cache_dir=None)
    restored = TemplateProfile.model_validate_json(profile.to_json())
    slots = [s for p in restored.patterns for s in p.slots]
    decor = [d for p in restored.patterns for d in p.decor]
    assert slots and all(s.source_shape_id for s in slots)
    assert decor and all(d.source_shape_id for d in decor)
    assert restored == profile
