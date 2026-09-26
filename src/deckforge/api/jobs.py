"""Оркестратор задач API (Task 16) — единственное место, которое склеивает
весь пайплайн (`parse -> outline -> write -> compose -> audit -> export`)
в фоновую задачу, публикующую прогресс по этапам.

Работа идёт в фоне через `asyncio.TaskGroup` (брифом дословно): три
варианта вёрстки не зависят друг от друга ни на этапе сборки, ни на этапе
аудита, ни на этапе выгрузки — каждый такой этап запускает три задачи
`TaskGroup`'ом и ждёт все разом, а не по очереди. Сами шаги пайплайна
(`TemplateProfile.from_file`, `build_outline`, `write_slides`, `build_deck`,
`run_deterministic`, `export_bundle`) — синхронный, блокирующий код (диск,
subprocess `soffice`, CPU); каждый вызов уходит в `asyncio.to_thread`, чтобы
не заморозить цикл событий (и, значит, не заморозить чтение прогресса
другим запросом, пока задача работает).

Артефакты каждой задачи лежат в каталоге задания на диске
(`<paths.artifacts>/api/decks/<job_id>/<variant>/...`), не в памяти —
`VariantState` в памяти несёт только пути и уже посчитанные находки;
перезапуск процесса теряет реестр задач (см. `JobStore` ниже), но не сами
собранные файлы."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable
from uuid import uuid4

from deckforge.audit.autofix import SUPPORTED_CHECKS, apply_fixes
from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import run_deterministic
from deckforge.audit.findings import Finding
from deckforge.compose.builder import build_deck, build_safe_deck
from deckforge.export.bundle import export_bundle
from deckforge.plan.coverage import ContentValidationError, validate_deck_content, validate_pptx_content
from deckforge.plan.outline import SourceDoc, build_outline
from deckforge.plan.spec import (
    BulletBlock, CardBlock, DeckSpec, KpiBlock, QuoteBlock, SlideSpec, TextBlock,
    deck_spec_to_dict,
)
from deckforge.plan.variants import Variant, apply_variant
from deckforge.plan.writer import AGENT_MAX_STEPS_DEFAULT, DEFAULT_WRITER_MAX_WORKERS, write_slides
from deckforge.provider.base import LLMProvider
from deckforge.provider.registry import ModelNotAllowed
from deckforge.provider.yandex import YandexProvider
from deckforge.settings import Settings
from deckforge.template.profile import TemplateProfile

APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"
logger = logging.getLogger(__name__)

STAGES: tuple[str, ...] = ("parse", "outline", "write", "compose", "audit", "export")


class JobError(ValueError):
    """Ошибка, чей текст безопасно показать пользователю API как есть
    (400/404) — не голое исключение из глубины пайплайна."""


class ProviderUnavailableError(JobError):
    """Генерацию нельзя начинать без обязательных AI-ролей."""


ProviderFactory = Callable[[str], LLMProvider | None]


def _safe_error(exc: BaseException) -> str:
    text = str(exc)[:1200] or type(exc).__name__
    try:
        settings = Settings.load(APP_YAML_PATH)
        for secret in (settings.yandex_api_key, settings.yandex_folder_id):
            if secret:
                text = text.replace(secret, "***")
    except Exception:
        pass
    return text


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _outline_dict(outline) -> dict:
    return {
        "title": outline.title,
        "language": outline.language,
        "generation_origin": outline.generation_origin,
        "generation_error": outline.generation_error,
        "slides": [
            {"index": index, "kind": slide.kind, "intent": slide.intent, "needs": list(slide.needs)}
            for index, slide in enumerate(outline.slides)
        ],
    }


def finding_id(finding: Finding) -> str:
    """Стабильный (по содержанию находки, не по позиции в списке)
    идентификатор находки — интерфейсу нужно адресовать ЧЕКБОКСОМ КОНКРЕТНУЮ
    находку (`POST /api/decks/{id}/fix`, `finding_ids`), а `Finding` сама
    id не несёт (датакласс `audit.findings`, общий для CLI и API)."""
    raw = f"{finding.check_id}|{finding.slide_index}|{finding.shape_ref}|{finding.message}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _build_role_provider(role: str) -> LLMProvider | None:
    """Тот же приём, что `cli._build_role_provider` — без ключа/сети
    пайплайн обязан продолжать работать запасными вариантами, не падать."""
    try:
        settings = Settings.load(APP_YAML_PATH)
    except Exception:
        return None
    if not settings.yandex_api_key or not settings.yandex_folder_id:
        return None
    try:
        return YandexProvider(
            model=settings.llm.model_for(role), api_key=settings.yandex_api_key,
            folder_id=settings.yandex_folder_id, deadline_seconds=settings.llm.deadline_seconds,
        )
    except (ValueError, ModelNotAllowed):
        return None


def _writer_max_workers() -> int:
    try:
        return Settings.load(APP_YAML_PATH).llm.slide_writer_max_workers
    except Exception:
        return DEFAULT_WRITER_MAX_WORKERS


def _writer_agent_max_steps() -> int:
    """Task 19 — тот же приём, что и `_writer_max_workers` выше: бюджет
    сетевых кругов агентного цикла (`config/app.yaml`, `llm.slide_writer_
    agent_max_steps`), запасной дефолт модуля без читаемого конфига."""
    try:
        return Settings.load(APP_YAML_PATH).llm.slide_writer_agent_max_steps
    except Exception:
        return AGENT_MAX_STEPS_DEFAULT


def _artifacts_root() -> Path:
    try:
        settings = Settings.load(APP_YAML_PATH)
        base = settings.paths.artifacts
        if not base.is_absolute():
            base = APP_YAML_PATH.parents[1] / base
    except Exception:
        base = APP_YAML_PATH.parents[1] / "artifacts"
    return base / "api"


@dataclass
class TemplateRecord:
    template_id: str
    path: Path
    profile: TemplateProfile


@dataclass
class VariantState:
    variant: Variant
    deck_spec: DeckSpec
    pptx_path: Path
    findings: list[Finding] = field(default_factory=list)
    finding_index: dict[str, Finding] = field(default_factory=dict)
    preview_pngs: list[Path] = field(default_factory=list)
    pdf_path: Path | None = None
    html_path: Path | None = None
    autofixed_count: int = 0
    content_verification: dict = field(default_factory=dict)

    def set_findings(self, findings: list[Finding]) -> None:
        self.findings = findings
        self.finding_index = {finding_id(f): f for f in findings}


@dataclass
class JobRecord:
    job_id: str
    template_id: str
    dir: Path
    status: str = "running"
    stage: str | None = None
    stages: list[str] = field(default_factory=list)
    error: str | None = None
    profile: TemplateProfile | None = None
    variants: dict[str, VariantState] = field(default_factory=dict)
    generation_summary: dict | None = None
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    def snapshot(self) -> dict:
        # deck_id == job_id (см. докстроку модуля) — колода этого задания
        # адресуется тем же идентификатором с самого создания, а не только
        # когда экспорт готов; эндпоинты, которым нужны данные варианта
        # (variants/fix/export), сами проверяют готовность (`get_deck`).
        # `template_id` нужен интерфейсу (Task 16, п.1): вернувшись на экран
        # брифа с экрана вариантов/аудита, генерировать заново по уже
        # разобранному шаблону, не загружая .pptx повторно.
        return {
            "job_id": self.job_id, "template_id": self.template_id, "status": self.status,
            "stage": self.stage, "stages": list(self.stages), "deck_id": self.job_id,
            "error": self.error, "generation_summary": self.generation_summary,
        }

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(self.snapshot())
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _notify(self) -> None:
        snap = self.snapshot()
        for queue in list(self._subscribers):
            queue.put_nowait(snap)

    def enter_stage(self, stage: str) -> None:
        self.stage = stage
        self.stages.append(stage)
        self._notify()

    def finish(self, *, error: str | None = None, warnings: bool = False) -> None:
        self.status = "error" if error else ("done_with_warnings" if warnings else "done")
        self.error = error
        self._notify()


class JobStore:
    """Реестр задач и шаблонов процесса (в памяти — см. докстроку модуля
    про то, что переживает и что не переживает перезапуск)."""

    def __init__(
        self, root: Path | None = None, *, provider_factory: ProviderFactory | None = None,
        template_provider_factory: ProviderFactory | None = None,
        allow_offline_fallback: bool = False,
        require_provider: bool = True,
        allow_content_fallback: bool = True,
    ) -> None:
        self.root = root if root is not None else _artifacts_root()
        self.provider_factory = provider_factory or _build_role_provider
        self.template_provider_factory = template_provider_factory or _build_role_provider
        self.allow_offline_fallback = allow_offline_fallback
        self.require_provider = require_provider and not allow_offline_fallback
        self.allow_content_fallback = allow_content_fallback
        self.templates: dict[str, TemplateRecord] = {}
        self.jobs: dict[str, JobRecord] = {}

    # -- шаблоны -----------------------------------------------------

    async def create_template(self, data: bytes, filename: str) -> TemplateRecord:
        if not filename.lower().endswith(".pptx") or not zipfile.is_zipfile(BytesIO(data)):
            raise JobError(
                f"Файл {filename!r} не похож на .pptx (ожидается ZIP-контейнер "
                "OOXML с расширением .pptx)."
            )
        template_id = uuid4().hex
        tdir = self.root / "templates" / template_id
        tdir.mkdir(parents=True, exist_ok=True)
        path = tdir / "template.pptx"
        path.write_bytes(data)
        try:
            profile = await asyncio.to_thread(
                TemplateProfile.from_file, path,
                namer=self.template_provider_factory("palette_namer"),
                vision=self.template_provider_factory("pattern_kind"),
            )
        except Exception as exc:  # noqa: BLE001 — любая причина разбора превращается в читаемую 400-ошибку
            shutil.rmtree(tdir, ignore_errors=True)
            raise JobError(f"Не удалось разобрать шаблон {filename!r} как .pptx: {exc}") from exc
        # The parser sees the internal storage name. The interface must show
        # the file name selected by the user instead.
        profile.source_name = Path(filename).name
        (tdir / "profile.json").write_text(profile.to_json(), encoding="utf-8")
        record = TemplateRecord(template_id=template_id, path=path, profile=profile)
        self.templates[template_id] = record
        return record

    def get_template(self, template_id: str) -> TemplateRecord:
        record = self.templates.get(template_id)
        if record is None:
            raise JobError(f"Шаблон {template_id!r} не найден.")
        return record

    # -- задания -------------------------------------------------------

    def create_job(
        self, *, template_id: str, brief: str, sources: list[str], title: str | None,
        language: str, target_slides: int | None, autofix: bool,
    ) -> JobRecord:
        template = self.get_template(template_id)
        try:
            outline_llm = self.provider_factory("outline")
            writer_llm = self.provider_factory("writer")
        except Exception as exc:
            raise ProviderUnavailableError(
                "Не удалось подключить AI для создания презентации. Проверьте настройки Yandex Cloud."
            ) from exc
        if self.require_provider and (outline_llm is None or writer_llm is None):
            raise ProviderUnavailableError(
                "AI для создания презентации недоступен. Проверьте YANDEX_API_KEY и "
                "YANDEX_FOLDER_ID в файле .env и перезапустите сервер."
            )
        job_id = uuid4().hex
        job_dir = self.root / "decks" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = JobRecord(job_id=job_id, template_id=template_id, dir=job_dir, profile=template.profile)
        self.jobs[job_id] = job
        asyncio.create_task(_run_job(
            job=job, template=template, brief=brief, sources=sources, title=title,
            language=language, target_slides=target_slides, autofix=autofix,
            outline_llm=outline_llm, writer_llm=writer_llm,
            allow_fallback=self.allow_content_fallback,
        ))
        return job

    def get_job(self, job_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError(f"Задание {job_id!r} не найдено.")
        return job

    def get_deck(self, deck_id: str) -> JobRecord:
        job = self.get_job(deck_id)
        if job.status not in ("done", "done_with_warnings"):
            raise JobError(f"Колода {deck_id!r} ещё не готова (статус: {job.status}).")
        return job


# ---------------------------------------------------------------------------
# Пайплайн одного задания
# ---------------------------------------------------------------------------

async def _run_job(
    *, job: JobRecord, template: TemplateRecord, brief: str, sources: list[str], title: str | None,
    language: str, target_slides: int | None, autofix: bool,
    outline_llm: LLMProvider | None, writer_llm: LLMProvider | None, allow_fallback: bool,
) -> None:
    report: dict = {"status": "running", "source_count": len(sources), "variants": {}}
    try:
        job.enter_stage("parse")
        profile = template.profile  # уже разобран при загрузке шаблона (POST /api/templates)
        job.profile = profile

        job.enter_stage("outline")
        source_docs = [SourceDoc(name="brief.md", text=brief)]
        source_docs.extend(SourceDoc(name=f"source-{i + 1}.md", text=text) for i, text in enumerate(sources))
        outline = await asyncio.to_thread(
            build_outline, brief, source_docs, profile, outline_llm, target_slides,
            title=title or "Презентация", language=language, allow_fallback=allow_fallback,
        )
        _write_json(job.dir / "outline.json", _outline_dict(outline))

        job.enter_stage("write")
        deck = await asyncio.to_thread(
            write_slides, outline, source_docs, profile, writer_llm,
            max_workers=_writer_max_workers(), agent_max_steps=_writer_agent_max_steps(),
            template_path=template.path, allow_fallback=allow_fallback,
        )
        coverage = await asyncio.to_thread(validate_deck_content, deck, [brief, *sources])
        _write_json(job.dir / "deck-spec.json", deck_spec_to_dict(deck))
        report.update({
            "outline_origin": outline.generation_origin,
            "outline_error": outline.generation_error,
            "slide_count": len(deck.slides),
            "slide_origins": [
                {"index": slide.index, "origin": slide.generation_origin, "error": slide.generation_error}
                for slide in deck.slides
            ],
            "source_coverage": coverage,
        })
        job.generation_summary = {
            "slide_count": len(deck.slides),
            "source_fact_count": coverage["source_fact_count"],
            "used_fact_count": coverage["used_fact_count"],
            "coverage_ratio": coverage["coverage_ratio"],
            "fallback_slide_count": sum(
                1 for slide in deck.slides if slide.generation_origin in ("fallback", "degraded")
            ),
        }
        _write_json(job.dir / "generation-report.json", report)

        job.enter_stage("compose")
        compose_results = await asyncio.gather(*(
            _compose_variant(job, variant, deck, profile, template.path, coverage)
            for variant in Variant
        ), return_exceptions=True)
        variant_errors: list[dict] = []
        for variant, result in zip(Variant, compose_results):
            if isinstance(result, BaseException):
                logger.error(
                    "generation variant failed",
                    extra={"job_id": job.job_id, "stage": "compose", "variant": variant.value},
                    exc_info=(type(result), result, result.__traceback__),
                )
                variant_errors.append(_variant_error("compose", variant.value, result))
        if not job.variants:
            raise RuntimeError(
                "Не удалось собрать ни один вариант: "
                + "; ".join(item["message"] for item in variant_errors)
            )
        report["variants"] = {
            name: state.content_verification for name, state in sorted(job.variants.items())
        }
        _write_json(job.dir / "generation-report.json", report)

        job.enter_stage("audit")
        config = AuditConfig.load()
        audit_names = list(job.variants)
        audit_results = await asyncio.gather(*(
            _audit_variant(job, name, profile, config, autofix) for name in audit_names
        ), return_exceptions=True)
        for name, result in zip(audit_names, audit_results):
            if isinstance(result, BaseException):
                variant_errors.append(_variant_error("audit", name, result))

        job.enter_stage("export")
        export_names = list(job.variants)
        export_results = await asyncio.gather(*(
            _export_variant(job, name, profile) for name in export_names
        ), return_exceptions=True)
        for name, result in zip(export_names, export_results):
            if isinstance(result, BaseException):
                variant_errors.append(_variant_error("export", name, result))
                job.variants.pop(name, None)
        if not job.variants:
            raise RuntimeError("Ни один собранный вариант не удалось подготовить к выдаче.")

        warnings = outline.generation_origin == "fallback" or bool(variant_errors) or any(
            slide.generation_origin in ("fallback", "degraded") for slide in deck.slides
        ) or any(
            slide.generation_origin == "degraded"
            for state in job.variants.values() for slide in state.deck_spec.slides
        )
        report["variant_errors"] = variant_errors
        report["status"] = "done_with_warnings" if warnings else "done"
        _write_json(job.dir / "generation-report.json", report)
        job.finish(warnings=warnings)
    except Exception as exc:  # noqa: BLE001 — любая причина отказа обязана дойти до пользователя API,
        # не остаться молчаливым "running" навсегда
        logger.exception("generation job failed", extra={"job_id": job.job_id, "stage": job.stage})
        error = _safe_error(exc)
        report.update({
            "status": "error", "error": error,
            "errors": [{
                "stage": job.stage, "variant": None, "slide_index": None,
                "exception_type": type(exc).__name__, "attempt": 1, "message": error,
            }],
        })
        _write_json(job.dir / "generation-report.json", report)
        job.finish(error=error)


def _variant_error(stage: str, variant: str, exc: BaseException) -> dict:
    return {
        "stage": stage,
        "variant": variant,
        "slide_index": None,
        "exception_type": type(exc).__name__,
        "attempt": 1,
        "message": _safe_error(exc),
    }


async def _compose_variant(
    job: JobRecord, variant: Variant, deck: DeckSpec, profile: TemplateProfile,
    template_path: Path, coverage: dict,
) -> None:
    dest_dir = job.dir / variant.value
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "deck.pptx"
    strategies: list[tuple[str, Variant | str | None]] = [(variant.value, variant)]
    strategies.extend(
        (name, candidate) for name, candidate in (
            ("visual-compatible", Variant.visual),
            ("dense-compatible", Variant.dense),
            ("original-compatible", None),
        )
        if candidate is None or candidate is not variant
    )
    strategies.append(("safe-basic", "safe-basic"))
    attempted: list[str] = []
    seen: set[tuple] = set()
    selected: tuple[DeckSpec, Path, dict, str] | None = None
    for strategy_name, strategy_variant in strategies:
        base = copy.deepcopy(deck)
        if strategy_variant == "safe-basic":
            variant_deck = base
            for slide in variant_deck.slides:
                slide.findings.append(
                    "Использована базовая безопасная компоновка: макеты шаблона теряли часть содержания."
                )
                slide.generation_origin = "degraded"
                slide.generation_error = "использована безопасная компоновка"
        elif strategy_variant is not None:
            variant_deck = await asyncio.to_thread(apply_variant, base, profile, strategy_variant)
        else:
            variant_deck = base
        signature = (strategy_name, tuple(
            (slide.kind, slide.pattern_id, len(slide.blocks)) for slide in variant_deck.slides
        ))
        if signature in seen:
            continue
        seen.add(signature)
        builder = build_safe_deck if strategy_variant == "safe-basic" else build_deck
        built_path = await asyncio.to_thread(
            builder, variant_deck, profile, template_path, variant,
        )
        try:
            verification = await asyncio.to_thread(
                validate_pptx_content, built_path, variant_deck, coverage,
            )
        except ContentValidationError as exc:
            attempted.append(f"{strategy_name}: {_safe_error(exc)}")
            continue
        selected = (variant_deck, built_path, verification, strategy_name)
        break
    if selected is None:
        raise ContentValidationError(
            f"для варианта {variant.value} не найдена раскладка без потери содержания: "
            + "; ".join(attempted)
        )
    variant_deck, built_path, verification, strategy_name = selected
    await asyncio.to_thread(shutil.copy2, built_path, dest)
    verification["layout_strategy"] = strategy_name
    verification["layout_attempts"] = len(attempted) + 1
    verification["dropped_content_count"] = 0
    job.variants[variant.value] = VariantState(
        variant=variant, deck_spec=variant_deck, pptx_path=dest,
        content_verification=verification,
    )


async def _audit_variant(job: JobRecord, variant_name: str, profile: TemplateProfile, config: AuditConfig, autofix: bool) -> None:
    state = job.variants[variant_name]
    findings = await asyncio.to_thread(run_deterministic, state.pptx_path, profile, config)
    if autofix:
        fixable = {finding_id(f): f for f in findings if f.check_id in SUPPORTED_CHECKS}
        if fixable:
            result = await asyncio.to_thread(apply_fixes, state.pptx_path, profile, findings, fixable)
            if result.changed:
                findings = await asyncio.to_thread(run_deterministic, state.pptx_path, profile, config)
            state.autofixed_count = len(result.applied)
    state.set_findings(findings)


async def _export_variant(job: JobRecord, variant_name: str, profile: TemplateProfile) -> None:
    state = job.variants[variant_name]
    out_dir = job.dir / variant_name
    bundle = await asyncio.to_thread(export_bundle, state.pptx_path, profile, out_dir, deck_spec=state.deck_spec)
    state.pdf_path = bundle.pdf
    state.html_path = bundle.html
    state.preview_pngs = bundle.pngs


# ---------------------------------------------------------------------------
# Починка по выбору пользователя (POST /api/decks/{id}/fix)
# ---------------------------------------------------------------------------

@dataclass
class FixOutcome:
    applied: list[str]
    skipped: list[str]
    findings: list[Finding]
    preview_pngs: list[Path]


async def fix_deck(job: JobRecord, variant_name: str, finding_ids: list[str]) -> FixOutcome:
    state = job.variants.get(variant_name)
    if state is None:
        raise JobError(f"Варианта {variant_name!r} нет в колоде {job.job_id!r}.")
    chosen = {fid: state.finding_index[fid] for fid in finding_ids if fid in state.finding_index}
    result = await asyncio.to_thread(apply_fixes, state.pptx_path, job.profile, state.findings, chosen)
    if result.changed:
        config = AuditConfig.load()
        findings = await asyncio.to_thread(run_deterministic, state.pptx_path, job.profile, config)
        state.set_findings(findings)
        state.autofixed_count += len(result.applied)
        bundle = await asyncio.to_thread(
            export_bundle, state.pptx_path, job.profile, job.dir / variant_name, deck_spec=state.deck_spec,
        )
        state.pdf_path, state.html_path, state.preview_pngs = bundle.pdf, bundle.html, bundle.pngs
    return FixOutcome(
        applied=result.applied, skipped=result.skipped,
        findings=state.findings, preview_pngs=state.preview_pngs,
    )
