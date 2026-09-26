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
from pydantic import BaseModel, Field

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
    # Task 18: уточнение вида раскладки (`Pattern.kind`) мультимодальной
    # моделью по картинке слайда-примера — `template.vision_kind.
    # classify_patterns_by_vision`. Со значением по умолчанию `None` (не
    # обязательное поле, как `content_audit` выше уже было для конфигов до
    # Task 12) — `model_for("pattern_kind")` падает на `self.model`, если
    # роль явно не расписана в `config/app.yaml`, то же поведение, что и у
    # любой роли, не перечисленной в `roles:` вовсе (см. `model_for`).
    pattern_kind: str | None = None
    # Task 20 (встраивание пользовательских фотографий): распределение
    # фотографий контент-пакета по слайдам, один вызов на колоду
    # (`plan.photos.assign_photos`) — тот же необязательный приём, что и
    # `pattern_kind` выше, роль новая и не обязана быть расписана в старых
    # конфигах (`model_for` падает на `self.model` без явной записи).
    photo_picker: str | None = None
    # Задача F: схема слотов раскладки по превью слайда-примера
    # (`template.vision_kind.describe_pattern_slots`), мультимодальная роль,
    # необязательная тем же приёмом, что и `pattern_kind`.
    pattern_schema: str | None = None


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
    # Задача W: одновременных вызовов модели на процесс, у всех ролей и
    # всех заданий вместе (`provider.scheduler.ModelScheduler`). Дефолт
    # дублирует `provider.scheduler.DEFAULT_LIMIT`.
    model_concurrency: int = 6
    # Task 19 ("настоящий агент вместо одиночного вызова"): сколько сетевых
    # кругов агентного цикла `plan.writer._write_with_agent_loop` разрешено
    # одному слайду — бриф задачи буквально: "Цикл ограничен двумя шагами".
    # Дефолт дублирует `plan.writer.AGENT_MAX_STEPS_DEFAULT` — см. её
    # комментарий про происхождение числа. Со значением по умолчанию по той
    # же причине, что и `slide_writer_max_workers` выше (конфиги без явного
    # поля, собранные вручную в тестах, не должны переставать парситься).
    slide_writer_agent_max_steps: int = 2
    # Ремонт недобора (`plan.writer._repair_underfill`): один добавочный
    # вызов на слайд, чьё крупнейшее текстовое место заполнено меньше чем
    # наполовину от цели. `fill_repair_max_items`: ремонтировать только
    # слайды с не более чем столькими элементами содержания, 0 — все.
    slide_writer_fill_repair: bool = True
    slide_writer_fill_repair_max_items: int = 0
    # Задача "разбор незнакомого шаблона в бюджет", находка №4 ("Гонять
    # оставшиеся обращения параллельно... число потоков лежит в
    # config/app.yaml. Сделай так же") — то же самое для уточнения вида
    # раскладки по картинке (`template.vision_kind.classify_patterns_by_
    # vision`): раньше число потоков было хардкод-константой модуля
    # (`DEFAULT_MAX_WORKERS`), теперь настройка, как и у `slide_writer_max_
    # workers`. Дефолт дублирует `vision_kind.DEFAULT_MAX_WORKERS` — см.
    # комментарий про происхождение числа в app.yaml.
    pattern_kind_max_workers: int = 4
    # Задача D: переранжирование раскладок моделью (`plan.writer.rerank_
    # patterns`) — короткий дедлайн вызова, свой параллелизм и бюджет всего
    # шага; происхождение чисел см. в app.yaml. Со значениями по умолчанию
    # по той же причине, что и поля выше.
    # Задача F: сколько паттернов описывает модель одновременно при снятии
    # схемы слотов. Дефолт дублирует `vision_kind.DEFAULT_SCHEMA_MAX_WORKERS`.
    pattern_schema_max_workers: int = 4
    pattern_picker_deadline_seconds: float = 20.0
    pattern_picker_max_workers: int = 8
    pattern_picker_step_budget_seconds: float = 40.0

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
    # Разрешение (dpi) рендера слайдов-примеров под уточнение вида раскладки
    # моделью (`template.vision_kind.classify_patterns_by_vision`) — задача
    # "разбор незнакомого шаблона в бюджет", находка №2 ("рисовать мельче").
    # Живой замер (`render/soffice.py`, докстрока про выборочный рендер):
    # `pdftoppm` на 9 разбросанных страниц контрольного шаблона — 4.6с при
    # dpi=110, 2.1с при dpi=72. Не наугад: сетка-коллаж
    # (`vision_kind._build_grid_collage`) ВСЕГДА пересжимает каждую ячейку до
    # фиксированной ширины `_CELL_WIDTH_PX=480` перед отправкой модели,
    # независимо от того, с каким dpi её растрировал `pdftoppm`, — значит
    # модель видит одну и ту же картинку что при 110, что при 72, ПОКА
    # исходный PNG шире 480px (типичный слайд 16:9 при dpi=72 — около 960px
    # по горизонтали, вдвое больше цели, без передискретизации вверх). 72 —
    # нижняя граница с заметным запасом над 480px, не наименьшее из
    # проверенных чисел (не гонялись ниже — качество, которое видит модель,
    # тут решает не dpi растрирования, а `_CELL_WIDTH_PX`, который эта
    # правка не трогает).
    pattern_kind_dpi: int = 72

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


class FontBudgetConfig(BaseModel):
    # Предел ужимания кегля при подгонке текста (`compose.failure.
    # FontBudget`): доля от кегля примера и ступени шкалы шаблона вниз.
    min_ratio: float = 0.8
    max_steps: int = 2
    # Заголовок обложки, разделителя, финала: его даёт пользователь и
    # сокращать нельзя, поэтому предел мягче.
    hero_headline_min_ratio: float = 0.6
    hero_headline_max_steps: int = 4


class ComposeConfig(BaseModel):
    # Собирать слайд клоном слайда-примера шаблона (`compose.clone`), а
    # сборку с нуля держать запасным путём. Выключатель нужен на случай,
    # если на незнакомом шаблоне клон начнёт давать брак: вернуть старое
    # поведение одной строкой конфига, не откатывая код.
    clone_examples: bool = True
    # Задача V3: ступени починки по категории отказа (`compose.failure.
    # RepairPolicy`); пусто: таблица по умолчанию из кода.
    repair_policy: dict[str, list[str]] = Field(default_factory=dict)
    font_degradation_budget: FontBudgetConfig = FontBudgetConfig()


class PlanConfig(BaseModel):
    # Переранжирование раскладок моделью для вариантов airy и visual
    # (`plan.writer.rerank_patterns`). Выключатель на случай, если выбор
    # модели на незнакомом шаблоне окажется хуже кода или не влезет в бюджет
    # времени: вернуть чисто детерминированный выбор одной строкой конфига.
    rerank_variants: bool = True
    # Задача N: текст airy/visual под выбранную раскладку (`plan.writer.
    # realize_for_variant`) и выбор нарядных раскладок под это; числа см. в
    # app.yaml.
    realize_variants: bool = True
    realize_max_workers: int = 4
    realize_step_budget_seconds: float = 60.0


class RunModeConfig(BaseModel):
    """Одна строка таблицы `run.modes` (задача L, см. `workflow.budget.
    ModeSpec` — то же самое, только со стороны конфига, а не рантайма)."""

    min_remaining: float
    rerank: bool
    visual_audit_max_slides: int


def _default_run_modes() -> dict[str, RunModeConfig]:
    return {
        "full": RunModeConfig(min_remaining=120.0, rerank=True, visual_audit_max_slides=4),
        "fast": RunModeConfig(min_remaining=75.0, rerank=False, visual_audit_max_slides=2),
        "emergency": RunModeConfig(min_remaining=0.0, rerank=False, visual_audit_max_slides=0),
    }


class RunConfig(BaseModel):
    # Бюджет времени одного прогона и режимы деградации
    # (`workflow.budget.RunBudget`/`RunMode`). Происхождение чисел см. в
    # app.yaml. Со значениями по умолчанию по той же причине, что и поля
    # `LLMConfig`.
    budget_seconds: float = 300.0
    modes: dict[str, RunModeConfig] = Field(default_factory=_default_run_modes)
    visual_audit_min_risk: float = 1.0
    visual_audit_batch: bool = False
    # Задача W: резервы времени и оценки длительности вызова. Дефолты
    # дублируют `workflow.budget.BudgetPolicy`, происхождение чисел в app.yaml.
    visual_audit_reserve_seconds: float = 45.0
    compose_export_reserve_seconds: float = 60.0
    export_reserve_seconds: float = 30.0
    visual_audit_call_seconds: float = 25.0
    writer_call_seconds: float = 20.0


class Settings(BaseModel):
    llm: LLMConfig
    paths: PathsConfig
    render: RenderConfig
    # Со значением по умолчанию: конфиги без раздела `compose` (собранные
    # вручную в тестах) не должны переставать парситься.
    compose: ComposeConfig = ComposeConfig()
    plan: PlanConfig = PlanConfig()
    run: RunConfig = RunConfig()
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
