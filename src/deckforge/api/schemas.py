"""Pydantic-схемы HTTP-слоя (Task 16, интерфейс брифа дословно).

Отдельно от `template.profile`/`plan.spec`/`audit.findings` — те модули не
знают об HTTP и не обязаны знать (та же граница, что и у остального
проекта: `plan/` не импортирует `compose/`). Схемы этого модуля — тонкий
перевод уже существующих датаклассов/pydantic-моделей в форму ответа API,
без дублирования их логики.
"""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal["parse", "outline", "write", "compose", "audit", "export"]
JobStatus = Literal["running", "done", "error"]
Style = Literal["dense", "airy", "visual"]


class TemplateUploadResponse(BaseModel):
    template_id: str
    profile: dict


class ProfileResponse(BaseModel):
    template_id: str
    profile: dict


class DeckInput(BaseModel):
    """Поля, общие для одного задания и пакета стилей."""

    template_id: str
    brief: str
    sources: list[str] = Field(default_factory=list)
    title: str | None = None
    language: str = "ru"
    target_slides: int | None = None
    # Step 2a брифа: демонстрация идёт без диалога — по умолчанию чинится
    # всё, что чинится автоматически (`audit.autofix.SUPPORTED_CHECKS`);
    # экран выбора в интерфейсе остаётся для случая, когда человек хочет
    # вмешаться (см. `POST /api/decks/{id}/fix`).
    autofix: bool = True


class DeckCreateRequest(DeckInput):
    # Задача Q: одно задание = одна презентация одного стиля.
    style: Style = "dense"


class DeckCreateResponse(BaseModel):
    job_id: str


class DeckBatchRequest(DeckInput):
    # Несколько стилей одним запросом: по заданию на стиль, структура общая.
    styles: list[Style] = Field(default_factory=lambda: ["dense", "airy", "visual"], min_length=1)


class DeckBatchResponse(BaseModel):
    batch_id: str
    job_ids: list[str]


class JobResponse(BaseModel):
    job_id: str
    template_id: str
    status: JobStatus
    stage: Stage | None
    stages: list[Stage]
    deck_id: str | None
    error: str | None = None
    # Задача Q: стиль задания, пакет (если создано вместе с соседями),
    # режим и секунды задания на верхнем уровне для списка заданий.
    style: Style = "dense"
    batch_id: str | None = None
    mode: str | None = None
    seconds: float | None = None
    # Задача H: бюджет прогона (секунды по стадиям, пропущенные стадии и
    # почему) и сводка аудита по картинке рискованных слайдов.
    budget: dict | None = None
    visual_audit: dict | None = None
    # Задача R: по стилю задания список находок «требует переписать текст или
    # другую раскладку» (`Finding.repair == "structural"`), автопочинка их
    # не трогает.
    structural: dict[str, list[dict]] | None = None


class FindingModel(BaseModel):
    id: str
    check_id: str
    severity: Literal["critical", "major", "minor"]
    slide_index: int | None
    shape_ref: str | None
    message: str
    box: dict | None  # {left, top, width, height} — доли холста, или null
    fixable: bool
    fix_hint: str
    # "local" чинит автопочинка, "structural" требует другого текста или
    # раскладки, "none" только сведения (см. `audit.findings.Repair`).
    repair: Literal["local", "structural", "none"] = "none"


class VariantSummary(BaseModel):
    variant: Literal["dense", "airy", "visual"]
    slide_count: int
    preview_pngs: list[str]
    findings: list[FindingModel]
    by_severity: dict[str, int]
    by_check: dict[str, int]
    autofixed_count: int
    # Задача M: средние оценки PPTEval (content/design) этого варианта,
    # если аудит по картинке прошёл и прислал хоть одну валидную оценку
    # (`audit.visual._axis_average`) — `None`, если нет (аудит не пошёл,
    # или модель не оценила ни один слайд).
    content_avg: float | None = None
    design_avg: float | None = None
    # Задача T: метрики верности шаблону (`audit.fidelity.FidelityReport`,
    # снятые слепком через `dataclasses.asdict`) — `None`, пока экспорт
    # варианта ещё не дошёл до этого шага, или метрика не посчиталась
    # (честная деградация, см. `api.jobs._export_variant`).
    fidelity: dict | None = None


class FixRequest(BaseModel):
    variant: Literal["dense", "airy", "visual"]
    finding_ids: list[str]


class FixResponse(BaseModel):
    variant: Literal["dense", "airy", "visual"]
    applied: list[str]
    skipped: list[str]
    findings: list[FindingModel]
    preview_pngs: list[str]
