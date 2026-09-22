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

# Task 14 (и task-12-brief, раздел про soffice): LibreOffice на этой машине
# не видит системные шрифты (в т.ч. шрифт шаблона `Play`, установленный в
# `~/Library/Fonts/`) без явного `FONTCONFIG_PATH` — без него рендерит
# слайд шрифтом с засечками вместо шрифта шаблона, и PDF/PNG расходятся с
# .pptx. Автопоиск по типичным путям Homebrew/системного fontconfig, тем же
# приёмом, что и `_SOFFICE_CANDIDATES` выше.
_FONTCONFIG_CANDIDATES = (
    "/opt/homebrew/etc/fonts",
    "/usr/local/etc/fonts",
    "/etc/fonts",
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
    # Дефолт дублирует DEFAULT_DEADLINE_SECONDS в provider/yandex.py — см.
    # комментарий про происхождение числа в app.yaml. Со значением по
    # умолчанию (а не обязательным полем), чтобы конфиги без явного
    # deadline_seconds (например, собранные вручную в тестах) не переставали
    # парситься.
    deadline_seconds: float = 60.0
    # Дефолт дублирует `plan.writer.DEFAULT_WRITER_MAX_WORKERS` — см.
    # комментарий про происхождение числа в app.yaml. Со значением по
    # умолчанию по той же причине, что и `deadline_seconds` выше.
    slide_writer_max_workers: int = 4

    def model_for(self, role: str) -> str:
        """Модель для роли; если роль не описана явно — модель по умолчанию."""
        return getattr(self.roles, role, None) or self.model


class PathsConfig(BaseModel):
    workspace: Path
    artifacts: Path
    # Каталог диск-кеша TemplateProfile (Task 8 код-ревью, находка 2):
    # `TemplateProfile.from_file` пишет и читает сюда профили по отпечатку
    # файла (`fingerprint`), чтобы оркестратор генерации не платил ~20с
    # разбора и сетевого именования палитры за каждую колоду одного и того
    # же шаблона. Со значением по умолчанию — не обязательное поле, чтобы
    # конфиги без явного profile_cache (собранные вручную в тестах, как
    # workspace/artifacts) не переставали парситься.
    profile_cache: Path = Path("cache/profiles")


class RenderConfig(BaseModel):
    soffice_path: str | None = None
    # Каталог fontconfig, который видит шрифты шаблона (см. докстроку
    # `_FONTCONFIG_CANDIDATES`). `None` — автопоиск; пустая строка — явно
    # не передавать FONTCONFIG_PATH вовсе (унаследовать окружение как есть).
    fontconfig_path: str | None = None

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

    def resolve_fontconfig(self) -> str | None:
        """Каталог `FONTCONFIG_PATH` для вызова soffice, либо `None`, если
        ни явного значения, ни одного из типичных путей не нашлось (soffice
        в этом случае наследует окружение процесса как есть)."""
        if self.fontconfig_path is not None:
            return self.fontconfig_path or None
        for candidate in _FONTCONFIG_CANDIDATES:
            if Path(candidate).is_dir():
                return candidate
        return None


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
        leaked = [key for key in ("yandex_api_key", "yandex_folder_id") if key in data]
        if leaked:
            raise ValueError(
                f"{path}: секретам ({', '.join(leaked)}) не место в yaml-конфиге — "
                "их место в .env (см. .env.example)."
            )
        return cls(
            **data,
            yandex_api_key=os.environ.get("YANDEX_API_KEY"),
            yandex_folder_id=os.environ.get("YANDEX_FOLDER_ID"),
        )
