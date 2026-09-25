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
завершении теста).

Чего эта фикстура НЕ прикрывает: разбор шаблона НА УРОВНЕ МОДУЛЯ
(`PROFILE = TemplateProfile.from_file(TEMPLATE)` в шапке тестового файла).
Импорт модуля случается ДО любой фикстуры, поэтому такой вызов читает
настоящий `config/app.yaml` и пишет в настоящий `cache/profiles/` рабочего
дерева. Так туда и попал профиль, собранный БЕЗ ключа модели, который потом
подобрал живой запуск пользователя через веб-интерфейс (23 сентября 2026):
кеш общий, ключ считается от содержимого файла, и отравленная запись жила
до ручной чистки. Поэтому все модульные разборы в дереве идут с
`cache_dir=None` — прогон тестов не имеет права ни читать, ни писать общий
кеш."""
from __future__ import annotations
import pytest

from _profile_cache_isolation import write_isolated_app_yaml


@pytest.fixture(autouse=True)
def _isolate_default_profile_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "deckforge.template.profile.APP_YAML_PATH", write_isolated_app_yaml(tmp_path),
    )
