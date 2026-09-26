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
import json
import hashlib
import shutil
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from deckforge.audit.autofix import SUPPORTED_CHECKS, apply_fixes
from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import run_deterministic
from deckforge.audit.findings import Finding
from deckforge.compose.builder import build_deck
from deckforge.export.bundle import export_bundle
from deckforge.plan.outline import SourceDoc, build_outline
from deckforge.plan.spec import DeckSpec, deck_spec_to_dict
from deckforge.plan.variants import Variant, apply_variant
from deckforge.plan.writer import AGENT_MAX_STEPS_DEFAULT, DEFAULT_WRITER_MAX_WORKERS, rerank_patterns, write_slides
from deckforge.provider.base import LLMProvider
from deckforge.provider.registry import ModelNotAllowed
from deckforge.provider.yandex import YandexProvider
from deckforge.settings import Settings
from deckforge.template.profile import TemplateProfile
from deckforge.export.html import to_html_report
from deckforge.workflow.budget import RunBudget, load_policy
from deckforge.workflow.visual_stage import run_visual_stage

APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"

STAGES: tuple[str, ...] = ("parse", "outline", "write", "compose", "audit", "export")


class JobError(ValueError):
    """Ошибка, чей текст безопасно показать пользователю API как есть
    (400/404) — не голое исключение из глубины пайплайна."""


def finding_id(finding: Finding) -> str:
    """Стабильный (по содержанию находки, не по позиции в списке)
    идентификатор находки — интерфейсу нужно адресовать ЧЕКБОКСОМ КОНКРЕТНУЮ
    находку (`POST /api/decks/{id}/fix`, `finding_ids`), а `Finding` сама
    id не несёт (датакласс `audit.findings`, общий для CLI и API)."""
    raw = f"{finding.check_id}|{finding.slide_index}|{finding.shape_ref}|{finding.message}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _build_role_provider(role: str, *, deadline_seconds: float | None = None) -> LLMProvider | None:
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
            folder_id=settings.yandex_folder_id,
            deadline_seconds=deadline_seconds if deadline_seconds is not None else settings.llm.deadline_seconds,
        )
    except (ValueError, ModelNotAllowed):
        return None


def _rerank(deck: DeckSpec, profile: TemplateProfile, budget: RunBudget | None = None) -> dict[Variant, dict[int, str]]:
    """Тот же шаг, что `cli._rerank`: модель выбирает раскладку из трёх для
    airy и visual. Выключено в конфиге, нет ключа или режим прогона (задача
    L, зафиксирован на контрольной точке `after_write`) rerank не
    разрешает: пустой выбор, раскладку выбирает код."""
    try:
        settings = Settings.load(APP_YAML_PATH)
    except Exception:
        return {}
    if not settings.plan.rerank_variants:
        return {}
    if budget is not None and not budget.mode_spec().rerank:
        return {}
    llm = _build_role_provider("pattern_picker", deadline_seconds=settings.llm.pattern_picker_deadline_seconds)
    chosen = rerank_patterns(
        deck, profile, list(Variant), llm,
        max_workers=settings.llm.pattern_picker_max_workers,
        budget_seconds=settings.llm.pattern_picker_step_budget_seconds,
    )
    by_variant: dict[Variant, dict[int, str]] = {}
    for (variant, index), pattern_id in chosen.items():
        by_variant.setdefault(variant, {})[index] = pattern_id
    return by_variant


def _build_visual_auditor() -> LLMProvider | None:
    """Провайдер роли `content_audit` для аудита по картинке. Отдельной
    функцией, чтобы тесты подменяли только его, не трогая роли, которые
    пишут текст."""
    return _build_role_provider("content_audit")


def _new_budget() -> RunBudget:
    return RunBudget.from_policy(load_policy(APP_YAML_PATH))


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


def _writer_fill_repair() -> int | None:
    """Ремонт недобора, тот же приём, что `cli._writer_fill_repair`."""
    try:
        llm = Settings.load(APP_YAML_PATH).llm
    except Exception:
        return 0
    return llm.slide_writer_fill_repair_max_items if llm.slide_writer_fill_repair else None


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
    # Позиции слайдов (с нуля), на которых автопочинка что-то меняла: одна
    # из примет риска для аудита по картинке (`audit.risk`).
    autofixed_slides: set[int] = field(default_factory=set)
    # Находки аудита по картинке живут отдельно от детерминированных: после
    # ручной починки (`fix_deck`) детерминированный аудит пересчитывается, а
    # модель заново не спрашивают, и её находки иначе бы пропали.
    visual_findings: list[Finding] = field(default_factory=list)

    def set_findings(self, findings: list[Finding]) -> None:
        self.findings = findings + self.visual_findings
        self.finding_index = {finding_id(f): f for f in self.findings}


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
    # Бюджет прогона (`workflow.budget.RunBudget`): его же часы меряют
    # секунды каждой стадии для интерфейса и лога. `visual_audit`:
    # сводка стадии аудита по картинке (`VisualStageOutcome.summary`).
    budget: RunBudget | None = None
    visual_audit: dict | None = None
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _stage_started: float | None = None

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
            "error": self.error,
            "budget": self.budget.summary() if self.budget is not None else None,
            "visual_audit": self.visual_audit,
            # Задача R: находки, которые автопочинка не трогает, потому что
            # нужен другой текст или другая раскладка, по вариантам.
            "structural": {
                name: [
                    {"slide_index": f.slide_index, "check_id": f.check_id, "message": f.message}
                    for f in state.findings if f.repair == "structural"
                ]
                for name, state in self.variants.items()
            },
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

    def close_stage(self) -> None:
        """Записать секунды текущей стадии в бюджет. Стадии идут строго
        друг за другом (кроме аудита и экспорта при autofix=False, где
        граница проходит по первому варианту, дошедшему до экспорта), так
        что время стадии: от входа в неё до входа в следующую."""
        if self.budget is not None and self.stage is not None and self._stage_started is not None:
            self.budget.record(self.stage, self.budget.clock() - self._stage_started)
        self._stage_started = None

    def enter_stage(self, stage: str) -> None:
        self.close_stage()
        self.stage = stage
        self.stages.append(stage)
        if self.budget is not None:
            self._stage_started = self.budget.clock()
        self._notify()

    def finish(self, *, error: str | None = None) -> None:
        self.close_stage()
        self.status = "error" if error else "done"
        self.error = error
        self._notify()


class JobStore:
    """Реестр задач и шаблонов процесса (в памяти — см. докстроку модуля
    про то, что переживает и что не переживает перезапуск)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else _artifacts_root()
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
                namer=_build_role_provider("palette_namer"), vision=_build_role_provider("pattern_kind"),
            )
        except Exception as exc:  # noqa: BLE001 — любая причина разбора превращается в читаемую 400-ошибку
            shutil.rmtree(tdir, ignore_errors=True)
            raise JobError(f"Не удалось разобрать шаблон {filename!r} как .pptx: {exc}") from exc
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
        job_id = uuid4().hex
        job_dir = self.root / "decks" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = JobRecord(job_id=job_id, template_id=template_id, dir=job_dir, profile=template.profile)
        self.jobs[job_id] = job
        asyncio.create_task(_run_job(
            job=job, template=template, brief=brief, sources=sources, title=title,
            language=language, target_slides=target_slides, autofix=autofix,
        ))
        return job

    def get_job(self, job_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError(f"Задание {job_id!r} не найдено.")
        return job

    def get_deck(self, deck_id: str) -> JobRecord:
        job = self.get_job(deck_id)
        if job.status != "done":
            raise JobError(f"Колода {deck_id!r} ещё не готова (статус: {job.status}).")
        return job


# ---------------------------------------------------------------------------
# Пайплайн одного задания
# ---------------------------------------------------------------------------

async def _run_job(
    *, job: JobRecord, template: TemplateRecord, brief: str, sources: list[str], title: str | None,
    language: str, target_slides: int | None, autofix: bool,
) -> None:
    # Бюджет прогона создаётся первым делом: пять минут ТЗ считаются от
    # начала генерации, всё, что было до (загрузка и разбор шаблона),
    # в него не входит.
    job.budget = _new_budget()
    try:
        job.enter_stage("parse")
        profile = template.profile  # уже разобран при загрузке шаблона (POST /api/templates)
        job.profile = profile

        job.enter_stage("outline")
        outline_llm = _build_role_provider("outline")
        source_docs = [SourceDoc(name=f"source-{i + 1}.md", text=text) for i, text in enumerate(sources)]
        outline = await asyncio.to_thread(
            build_outline, brief, source_docs, profile, outline_llm, target_slides,
            title=title or "Презентация", language=language,
        )

        job.enter_stage("write")
        writer_llm = _build_role_provider("writer")
        deck = await asyncio.to_thread(
            write_slides, outline, source_docs, profile, writer_llm,
            max_workers=_writer_max_workers(), agent_max_steps=_writer_agent_max_steps(),
            template_path=template.path, fill_repair_max_items=_writer_fill_repair(),
        )

        # Задача L: первая контрольная точка режима прогона — фиксирует,
        # идёт ли ниже rerank раскладок моделью. Держится до точки
        # `after_compose` (см. докстроку `RunBudget.decide_mode`).
        job.budget.decide_mode("after_write")

        # План презентации на диск рядом с результатом. Командная строка
        # это делала всегда, интерфейс — нет, и разбирать жалобу «слайд
        # выглядит плохо» приходилось по собранному .pptx, где уже не видно
        # ни выбранной раскладки, ни находок сборки, ни того, что писала
        # модель (25 сентября 2026: полдня ушло на попытку восстановить по
        # файлу, почему заголовок вышел мелким, — без плана это гадание).
        try:
            (job.dir / "deck.json").write_text(
                json.dumps(deck_spec_to_dict(deck), ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — отладочный артефакт не вправе ронять генерацию
            pass

        job.enter_stage("compose")
        # Секунды переранжирования пишутся отдельно, но входят и в секунды
        # стадии compose: для интерфейса это одна стадия «вёрстка».
        rerank_started = job.budget.clock()
        preferred_by_variant = await asyncio.to_thread(_rerank, deck, profile, job.budget)
        job.budget.record("rerank", job.budget.clock() - rerank_started)
        async with asyncio.TaskGroup() as tg:
            for variant in Variant:
                tg.create_task(_compose_variant(
                    job, variant, deck, profile, template.path, preferred_by_variant.get(variant),
                ))

        # Задача L: вторая контрольная точка — после сборки всех вариантов,
        # перед аудитом/экспортом/аудитом по картинке. Держится до конца
        # прогона (следующей точки нет — это последняя необязательная
        # стадия пайплайна).
        job.budget.decide_mode("after_compose")

        # Ярлык на последний прогон рядом с каталогами заданий: их имена —
        # случайные номера, и найти «тот самый, который только что собрали»
        # иначе можно только по времени изменения. Ярлык переставляется на
        # каждом прогоне, старые каталоги не трогаются.
        # `job.dir.parent`, а не `self.root`: это функция, не метод, и
        # `self` тут нет. Ошибка глушилась исключением ниже, ярлык не
        # появлялся ни разу (найдено 27 сентября 2026 при задаче H).
        try:
            latest = job.dir.parent / "latest"
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(job.dir.name)
        except Exception:  # noqa: BLE001 — удобство, не вправе ронять генерацию
            pass

        config = AuditConfig.load()
        if autofix:
            # autofix=True: экспорт зависит от результата автопочинки
            # (`_audit_variant` перезаписывает pptx на диске), поэтому все
            # три варианта должны ДОДЕЛАТЬ аудит, прежде чем хоть один
            # уйдёт в экспорт — иначе можно экспортировать промежуточное
            # состояние pptx одного варианта, пока соседний ещё чинится.
            job.enter_stage("audit")
            async with asyncio.TaskGroup() as tg:
                for variant in Variant:
                    tg.create_task(_audit_variant(job, variant.value, profile, config, autofix))

            job.enter_stage("export")
            async with asyncio.TaskGroup() as tg:
                for variant in Variant:
                    tg.create_task(_export_variant(job, variant.value, profile))
        else:
            # autofix=False: `_audit_variant` не трогает pptx на диске, так
            # что экспорт варианта не зависит от аудита СОСЕДНИХ вариантов —
            # одна задача на вариант "аудит, затем экспорт" в одном
            # TaskGroup'е, а не два последовательных TaskGroup'а (лишнее
            # ожидание самого медленного варианта аудита перед началом
            # экспорта первого готового).
            job.enter_stage("audit")
            export_stage_entered = False

            async def _audit_then_export(variant_name: str) -> None:
                nonlocal export_stage_entered
                await _audit_variant(job, variant_name, profile, config, autofix)
                # Стадия "export" в прогрессе — общая на все три варианта,
                # выставляем её один раз, когда до экспорта добрался первый
                # вариант (порядок между вариантами не гарантирован, но сам
                # список стадий job.stages не должен раздуться дублями).
                if not export_stage_entered:
                    export_stage_entered = True
                    job.enter_stage("export")
                await _export_variant(job, variant_name, profile)

            async with asyncio.TaskGroup() as tg:
                for variant in Variant:
                    tg.create_task(_audit_then_export(variant.value))

        job.close_stage()
        await _visual_audit_dense(job, profile, source_docs)
        job.finish()
    except* Exception as eg:  # noqa: BLE001 — любая причина отказа обязана дойти до пользователя API,
        # не остаться молчаливым "running" навсегда
        job.finish(error="; ".join(str(e) for e in eg.exceptions))


async def _compose_variant(
    job: JobRecord, variant: Variant, deck: DeckSpec, profile: TemplateProfile, template_path: Path,
    preferred: dict[int, str] | None = None,
) -> None:
    variant_deck = await asyncio.to_thread(apply_variant, deck, profile, variant, preferred)
    built_path = await asyncio.to_thread(build_deck, variant_deck, profile, template_path, variant)
    dest_dir = job.dir / variant.value
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "deck.pptx"
    await asyncio.to_thread(shutil.copy2, built_path, dest)
    job.variants[variant.value] = VariantState(variant=variant, deck_spec=variant_deck, pptx_path=dest)


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
            state.autofixed_slides = {
                fixable[fid].slide_index for fid in result.applied
                if fid in fixable and fixable[fid].slide_index is not None
            }
    state.set_findings(findings)


async def _visual_audit_dense(job: JobRecord, profile: TemplateProfile, sources: list[SourceDoc]) -> None:
    """Аудит по картинке рискованных слайдов варианта dense, если бюджет
    прогона позволяет (`workflow.visual_stage`). Превью уже отрисованы
    экспортом. Находки ложатся в тот же список, что и детерминированные;
    HTML-отчёт dense перерисовывается с оценками модели. Любой сбой здесь
    не роняет готовую колоду: стадия необязательная."""
    state = job.variants.get(Variant.dense.value)
    if state is None or job.budget is None:
        return
    try:
        deterministic = [f for f in state.findings if f not in state.visual_findings]
        outcome = await asyncio.to_thread(
            run_visual_stage, job.budget, state.deck_spec, deterministic,
            _build_visual_auditor(),
            lambda: list(state.preview_pngs), sources=sources,
            autofixed_slides=state.autofixed_slides,
        )
    except Exception as exc:  # noqa: BLE001: необязательная стадия не вправе ронять готовую колоду
        job.visual_audit = {"ran": False, "skipped_reason": f"аудит по картинке упал: {exc}"}
        return
    summary = outcome.summary()
    job.visual_audit = summary
    if outcome.result is not None:
        state.visual_findings = outcome.findings
        state.set_findings(deterministic)
    # HTML dense-варианта перерисовывается в любом случае (не только когда
    # модель реально ответила): итоговый снимок бюджета (стадия visual_audit
    # уже посчитана `run_visual_stage`) и причина пропуска, если аудит не
    # пошёл, тоже часть отчёта задачи L, не только оценки модели.
    if state.html_path is not None:
        try:
            await asyncio.to_thread(
                to_html_report, state.deck_spec, profile, state.pptx_path, state.html_path,
                visual=outcome.result, budget=job.budget.summary(), risky_slides=summary.get("risk"),
            )
        except Exception:  # noqa: BLE001: HTML без оценок лучше, чем упавшее задание
            pass


async def _export_variant(job: JobRecord, variant_name: str, profile: TemplateProfile) -> None:
    state = job.variants[variant_name]
    out_dir = job.dir / variant_name
    # Задача L: режим прогона уже решён (`decide_mode("after_compose")` в
    # `_run_job` идёт до этой стадии) — снимок бюджета попадает в HTML сразу,
    # список рискованных слайдов (`risky_slides`) допишет только `_visual_
    # audit_dense`, перерисовав HTML dense-варианта заново, когда аудит
    # реально пройдёт.
    budget_summary = job.budget.summary() if job.budget is not None else None
    bundle = await asyncio.to_thread(
        export_bundle, state.pptx_path, profile, out_dir, deck_spec=state.deck_spec, budget=budget_summary,
    )
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
