"""Общая инфраструктура для всего дерева tests/.

Диск-кеш `TemplateProfile` (Task 8 код-ревью, находка 2) по умолчанию читает
каталог из `config/app.yaml` (`paths.profile_cache`) и пишет относительно
cwd — без изоляции тесты, вызывающие `TemplateProfile.from_file(path)` без
явного `cache_dir` (подавляющее большинство существующих тестов), заполняли
бы реальный `cache/` в рабочем дереве репозитория при каждом прогоне.

Подменяем не саму `_default_cache_dir()` (иначе
`test_default_cache_dir_is_read_from_app_yaml` не смог бы проверить
настоящую логику чтения — импорт функции внутри теста подхватил бы уже
подменённую autouse-фикстурой версию), а `profile.APP_YAML_PATH`: она
по-прежнему честно читает файл, но временный, с `profile_cache`, указывающим
во временный каталог теста. Тест, которому нужен настоящий
`config/app.yaml`, переопределяет `APP_YAML_PATH` обратно своим собственным
`monkeypatch.setattr` (стек monkeypatch корректно откатывает оба слоя по
завершении теста)."""
from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def _isolate_default_profile_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "profile_cache"
    fake_config = tmp_path / "app.yaml"
    fake_config.write_text(
        f"""
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
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("deckforge.template.profile.APP_YAML_PATH", fake_config)
