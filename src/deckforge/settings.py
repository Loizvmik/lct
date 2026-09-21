"""Загрузка конфигурации DeckForge.

config/app.yaml — единственная точка настройки (ТЗ требует воспроизводимый
сетап конфиг-файлом). Секреты (ключ и id каталога Yandex) в yaml не хранятся —
их подтягивает Settings.load() из переменных окружения (.env).
"""
from __future__ import annotations
import os
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel

# Порядок важен: сначала более специфичные для macOS/Homebrew пути,
# shutil.which подстрахует остальные платформы.
_SOFFICE_CANDIDATES = (
    "/opt/homebrew/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/soffice",
)


class LLMRoles(BaseModel):
    """Модель для каждой роли LLM-конвейера."""

    outline: str
    writer: str
    pattern_picker: str
    palette_namer: str
    content_audit: str


class LLMConfig(BaseModel):
    provider: str
    model: str
    roles: LLMRoles

    def model_for(self, role: str) -> str:
        """Модель для роли; если роль не описана явно — модель по умолчанию."""
        return getattr(self.roles, role, None) or self.model


class PathsConfig(BaseModel):
    workspace: Path
    artifacts: Path


class RenderConfig(BaseModel):
    soffice_path: str | None = None

    def resolve_soffice(self) -> str:
        """Путь к soffice: из конфига, иначе автопоиском по типичным путям и PATH."""
        if self.soffice_path:
            return self.soffice_path
        found = shutil.which("soffice")
        if found:
            return found
        for candidate in _SOFFICE_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        raise RuntimeError(
            "soffice не найден автопоиском. Укажите render.soffice_path в config/app.yaml "
            "или установите LibreOffice."
        )


class Settings(BaseModel):
    llm: LLMConfig
    paths: PathsConfig
    render: RenderConfig
    yandex_api_key: str | None = None
    yandex_folder_id: str | None = None

    @classmethod
    def load(cls, path: Path) -> Settings:
        """Прочитать app.yaml и подмешать секреты из окружения (.env)."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(
            **data,
            yandex_api_key=os.environ.get("YANDEX_API_KEY"),
            yandex_folder_id=os.environ.get("YANDEX_FOLDER_ID"),
        )
