"""Реестр версий агентов и конфигов, управляющих поведением воркфлоу.

ТЗ п.2.4 «Версионирование скиллов и агентов, лежащих в основе воркфлоу» и
п.4 «Промпты/конфиги скиллов и агентов в репозитории лежат отдельными
файлами, т.е. не зашиты в код» — этот модуль только ЧИТАЕТ эти файлы (их
frontmatter/версию и байты целиком для отпечатка), сам не содержит ни одной
инструкции модели и ни одного порога аудита.

Модель воркфлоу: пять ролей вызывают модель в жёстко заданных местах
пайплайна (`agents/*/AGENT.md`), а не свободным циклом инструментов поверх
форка MiniMax-AI/Mini-Agent — осознанный выбор в пользу прозрачной границы
программных слоёв (критерий ТЗ «прозрачное разделение программных слоёв»);
решение и его причина разобраны в `.superpowers/sdd/task-15-report.md`.
`manifest()` — то, что делает этот выбор проверяемым независимо от того,
форк это или нет: правка любого промпта или конфига меняет отпечаток здесь,
даже если автор забыл поднять версию в frontmatter/`version:`.
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = ROOT / "agents"
CONFIG_DIR = ROOT / "config"

# Конфиги, управляющие поведением воркфлоу (Task 15 бриф): пороги аудита,
# словарь имён макетов, реестр разрешённых моделей и единственная точка
# настройки самого приложения. Список закрытый и явный — новый конфиг
# отдельным файлом входит в реестр версий, только если его сюда дописали,
# то же требование "не забыть", что и с версией у агентов.
CONFIG_FILES: tuple[Path, ...] = (
    CONFIG_DIR / "app.yaml",
    CONFIG_DIR / "audit.yaml",
    CONFIG_DIR / "layout-kinds.yaml",
    CONFIG_DIR / "models.yaml",
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)

# Формат версии — semver `major.minor.patch`, без пре-релизных/build суффиксов
# (реестру нужно только "поднялась/не поднялась", не полный semver).
VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+\Z")


@dataclass(frozen=True)
class ManifestEntry:
    """Одна строка реестра: чем управляется поведение и какой версии оно сейчас."""

    kind: str  # "agent" | "config"
    name: str
    version: str
    sha256: str
    path: Path

    def label(self) -> str:
        """`"outline-writer 1.0.0"` — то, что уходит в свойство документа."""
        return f"{self.name} {self.version}"

    def has_semver(self) -> bool:
        return bool(VERSION_RE.match(self.version))


@dataclass(frozen=True)
class WorkflowManifest:
    entries: tuple[ManifestEntry, ...]

    def entry(self, name: str) -> ManifestEntry:
        for e in self.entries:
            if e.name == name:
                return e
        raise KeyError(f"{name!r} нет в реестре версий воркфлоу")

    def sha_for(self, name: str) -> str:
        return self.entry(name).sha256

    def version_for(self, name: str) -> str:
        return self.entry(name).version

    def as_property_value(self) -> str:
        """Строка для `docProps/custom.xml` (свойство `deckforge_workflow`):
        по одной записи `имя версия` на агента/конфиг, разделены `"; "`."""
        return "; ".join(e.label() for e in self.entries)


def manifest() -> WorkflowManifest:
    """Собрать реестр версий заново — читает файлы с диска на каждый вызов,
    без кеша: реестр обязан отражать состояние репозитория прямо сейчас
    (в частности — в тесте, что правка промпта меняет отпечаток)."""
    return WorkflowManifest(entries=tuple(_agent_entries() + _config_entries()))


def _agent_entries() -> list[ManifestEntry]:
    entries = []
    for path in sorted(AGENTS_DIR.glob("*/AGENT.md")):
        front = _read_frontmatter(path)
        name = str(front.get("name") or path.parent.name)
        version = str(front.get("version") or "")
        entries.append(ManifestEntry("agent", name, version, _sha256(path), path))
    return entries


def _config_entries() -> list[ManifestEntry]:
    entries = []
    for path in CONFIG_FILES:
        entries.append(ManifestEntry("config", path.name, _config_version(path), _sha256(path), path))
    return entries


def _read_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(f"{path}: нет YAML-frontmatter (---...---) первым блоком файла")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: frontmatter должен разбираться в словарь")
    return data


def _config_version(path: Path) -> str:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "version" in data:
        return str(data["version"])
    return ""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
