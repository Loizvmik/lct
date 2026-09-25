"""Изолированный `app.yaml` для тестов: кеш профилей уводится во временный
каталог, чтобы прогон тестов не читал и не писал общий `cache/profiles/`
рабочего дерева. Почему это отдельный модуль, а не только фикстура в
`tests/conftest.py`: та фикстура живёт на уровне функции, а генерация в
`tests/api/` идёт в фикстуре уровня модуля, которая срабатывает раньше, и
профиль без ключа модели уезжал в настоящий кеш (26 сентября 2026: запись
`ЛЦТ2026`-шаблона с `pattern_kinds_source: geometry` в `cache/profiles/`,
`source_path` вёл в `pytest-of-…`). Один текст конфига на оба уровня."""
from __future__ import annotations

from pathlib import Path

_APP_YAML = """
llm:
  provider: yandex
  model: qwen3.6-35b-a3b
  roles:
    outline: qwen3.6-35b-a3b
    writer: qwen3.6-35b-a3b
    pattern_picker: qwen3.6-35b-a3b
    palette_namer: qwen3.6-35b-a3b
    content_audit: qwen3.6-35b-a3b
paths:
  workspace: workspace
  artifacts: artifacts
  profile_cache: {cache_dir}
render:
  soffice_path: null
"""


def write_isolated_app_yaml(root: Path) -> Path:
    """Пишет `app.yaml` с `profile_cache` внутри `root` и возвращает путь к
    нему; подставлять в `deckforge.template.profile.APP_YAML_PATH`."""
    config = root / "app.yaml"
    config.write_text(_APP_YAML.format(cache_dir=root / "profile_cache"), encoding="utf-8")
    return config
