"""`deckforge parse` — CLI поверх TemplateProfile.from_file (Task 8, Step 3).

Числа из брифа (Step 3): на VK Tech шаблоне — не меньше 8 паттернов и не
меньше 30 лейаутов.
"""
import json
from pathlib import Path

import pytest

import deckforge.cli as cli_module
from deckforge.cli import _build_namer, main

TEMPLATES_DIR = Path("dataset/templates")


@pytest.fixture(autouse=True)
def _disable_model_providers(monkeypatch):
    """Keep parser tests offline even when Settings discovers the repository .env."""
    monkeypatch.setattr(cli_module, "_build_role_provider", lambda _role: None)


def test_parse_writes_profile_json(tmp_path):
    output = tmp_path / "profile.json"
    exit_code = main(["parse", str(TEMPLATES_DIR / "VK Tech шаблон.pptx"), "-o", str(output)])
    assert exit_code == 0

    data = json.loads(output.read_text(encoding="utf-8"))
    assert len(data["patterns"]) >= 8
    assert len(data["layouts"]) >= 30
    assert data["provenance"]
    assert data["fingerprint"]


def test_parse_prints_provenance_and_warnings_report(capsys, tmp_path):
    output = tmp_path / "profile.json"
    main(["parse", str(TEMPLATES_DIR / "VK Tech шаблон.pptx"), "-o", str(output)])

    out = capsys.readouterr().out
    assert "Откуда что взято" in out
    assert "разобрано за" in out
    assert "Лейаутов:" in out
    assert "Предупреждения" in out  # у VK Tech txStyles/fontScheme деградировавшие


def test_parse_works_without_api_key(monkeypatch, tmp_path):
    """CLI не должен требовать сеть/ключ — без него roles идут запасным
    вариантом, но разбор обязан пройти целиком."""
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    output = tmp_path / "profile.json"
    exit_code = main(["parse", str(TEMPLATES_DIR / "VK Tech шаблон.pptx"), "-o", str(output)])
    assert exit_code == 0
    assert output.exists()


def test_build_namer_returns_none_without_secrets(monkeypatch):
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    assert _build_namer() is None


def test_parse_requires_output_argument():
    with pytest.raises(SystemExit):
        main(["parse", str(TEMPLATES_DIR / "VK Tech шаблон.pptx")])
