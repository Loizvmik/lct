"""Хранилище разобранных шаблонов (`template/store.py`)."""
from __future__ import annotations

from deckforge.template.store import LocalProfileStore, profile_key


def test_the_same_bytes_give_the_same_key():
    assert profile_key(b"pptx-bytes", 11) == profile_key(b"pptx-bytes", 11)


def test_different_bytes_give_different_keys():
    assert profile_key(b"one", 11) != profile_key(b"two", 11)


def test_the_key_changes_with_the_schema_version():
    """Главное обещание модуля. Запись, сделанную старым разбором, новый код
    видеть не должен: 23 сентября 2026 мы дважды получили из кеша данные,
    которых текущий код уже не подразумевал — сперва раскладки, не
    уточнённые моделью, потом старую вместимость."""
    assert profile_key(b"pptx-bytes", 10) != profile_key(b"pptx-bytes", 11)


def test_a_missing_key_reads_as_absent_not_as_an_error(tmp_path):
    store = LocalProfileStore(tmp_path / "нет-такого-каталога")

    assert store.get("чего-там-нет") is None


def test_what_was_put_can_be_read_back(tmp_path):
    store = LocalProfileStore(tmp_path / "profiles")

    store.put("ключ", '{"schema_version": 11}')

    assert store.get("ключ") == '{"schema_version": 11}'


def test_a_failed_write_does_not_raise(tmp_path):
    """Кеш — ускорение, а не источник правды: нет прав, полон диск — разбор
    выполняется заново, генерация не падает."""
    blocker = tmp_path / "занято"
    blocker.write_text("я файл, а не каталог", encoding="utf-8")
    store = LocalProfileStore(blocker)

    store.put("ключ", "{}")

    assert store.get("ключ") is None
