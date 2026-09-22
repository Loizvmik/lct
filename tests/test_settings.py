from pathlib import Path
import pytest
from deckforge.settings import RenderConfig, Settings

ROOT = Path(__file__).resolve().parents[1]
APP_YAML = ROOT / "config" / "app.yaml"


def _soffice_available() -> bool:
    """Как в @live для сетевых тестов: не роняем тест на машине без LibreOffice."""
    try:
        RenderConfig().resolve_soffice()
        return True
    except RuntimeError:
        return False


needs_soffice = pytest.mark.skipif(
    not _soffice_available(), reason="soffice не найден автопоиском на этой машине"
)


def test_load_reads_app_yaml():
    settings = Settings.load(APP_YAML)
    assert settings.llm.provider == "yandex"
    assert settings.llm.model
    assert settings.llm.deadline_seconds > 0
    assert settings.paths.workspace.name == "workspace"
    assert settings.paths.artifacts.name == "artifacts"


def test_pattern_kind_max_workers_is_read_from_app_yaml():
    """Задача "разбор незнакомого шаблона в бюджет", находка №4 — число
    потоков уточнения вида раскладки (`template.vision_kind`) теперь
    настройка, как и `slide_writer_max_workers`, а не хардкод-константа
    модуля."""
    settings = Settings.load(APP_YAML)
    assert settings.llm.pattern_kind_max_workers == 4


def test_llm_roles_resolve_to_allowed_models():
    from deckforge.provider.registry import assert_allowed

    settings = Settings.load(APP_YAML)
    for role in ("outline", "writer", "pattern_picker", "palette_namer", "content_audit"):
        model_id = settings.llm.model_for(role)
        assert_allowed(model_id)


def test_secrets_come_from_env(monkeypatch):
    monkeypatch.setenv("YANDEX_API_KEY", "test-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "test-folder")
    settings = Settings.load(APP_YAML)
    assert settings.yandex_api_key == "test-key"
    assert settings.yandex_folder_id == "test-folder"


def test_missing_config_file_raises():
    with pytest.raises(FileNotFoundError):
        Settings.load(ROOT / "config" / "does-not-exist.yaml")


def test_secret_in_yaml_raises_clear_error_not_typeerror(tmp_path):
    """Секретам место только в .env. Если ключ случайно попал в app.yaml,
    ошибка должна объяснять это, а не быть голым TypeError про повтор
    аргумента конструктора."""
    config_path = tmp_path / "app.yaml"
    config_path.write_text(
        """
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
render:
  soffice_path: null
yandex_api_key: leaked-into-yaml
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="yandex_api_key"):
        Settings.load(config_path)


def test_soffice_uses_configured_path_without_autodiscovery(tmp_path):
    fake_soffice = tmp_path / "soffice"
    fake_soffice.write_text("")
    config_path = tmp_path / "app.yaml"
    config_path.write_text(
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
render:
  soffice_path: {fake_soffice}
""",
        encoding="utf-8",
    )
    settings = Settings.load(config_path)
    assert settings.render.resolve_soffice() == str(fake_soffice)


@needs_soffice
def test_soffice_autodiscovery_when_not_configured(tmp_path):
    config_path = tmp_path / "app.yaml"
    config_path.write_text(
        """
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
render:
  soffice_path: null
""",
        encoding="utf-8",
    )
    settings = Settings.load(config_path)
    # На машине эксперта (см. контекст: LibreOffice 26.8 установлен) автопоиск
    # обязан найти soffice без явного пути в конфиге.
    found = settings.render.resolve_soffice()
    assert found
    assert Path(found).name == "soffice"
