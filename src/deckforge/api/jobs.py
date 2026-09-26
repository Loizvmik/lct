"""Оркестратор задач API (Task 16) — единственное место, которое склеивает
весь пайплайн (`parse -> outline -> write -> compose -> audit -> export`)
в фоновую задачу, публикующую прогресс по этапам.

С задачи Q одно задание = одна презентация = один стиль (`dense`, `airy`
или `visual`) = один бюджет в 300 с на всё задание: разбор (готовый, из
загрузки шаблона), структура, раскладки на всю колоду, контракты, текст
под них, сборка, аудит, экспорт, аудит по картинке. Три стиля одной
кнопкой интерфейса это три задания (`JobStore.create_batch`), которые
идут параллельно, каждое со своим прогрессом, режимом и бюджетом.
Структуру задания пакета считают один раз (`OutlineShare`): первое
задание зовёт модель, остальные ждут тот же результат и помечают стадию
как переиспользованную. Текст у каждого стиля свой, потому что пишется
под свою композицию. Сами шаги пайплайна
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
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from deckforge.audit.autofix import SUPPORTED_CHECKS, apply_fixes
from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import run_deterministic
from deckforge.audit.fidelity import template_fidelity
from deckforge.audit.findings import Finding
from deckforge.compose.builder import build_deck
from deckforge.export.bundle import export_bundle
from deckforge.pattern.intent import intents_from_outline
from deckforge.plan.contracts import plan_contracts
from deckforge.plan.outline import Outline, SourceDoc, build_outline, outline_to_dict
from deckforge.plan.spec import DeckSpec, deck_spec_to_dict
from deckforge.plan.variants import GenerationStyle, Variant
from deckforge.plan.writer import AGENT_MAX_STEPS_DEFAULT, DEFAULT_WRITER_MAX_WORKERS, write_slides
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

# Стадии, которые задание может получить готовыми: разбор шаблона сделан
# при загрузке, структуру пакета стилей считает одно задание на всех. Их
# секунды пишет `JobRecord.close_stage`; остальные стадии пишет
# `_run_variant` сам, с разбивкой (`plan` отдельно от `write`).
SHARED_STAGES: tuple[str, ...] = ("parse", "outline")


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


def _build_writer() -> LLMProvider | None:
    """Провайдер писателя слайдов. Отдельной функцией, чтобы тесты
    подменяли модель, не трогая остальные роли."""
    return _build_role_provider("writer")


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
    # Задача M: средние оценки PPTEval (content/design) ЭТОГО варианта —
    # `None`, пока аудит по картинке не прошёл или не прислал ни одной
    # валидной оценки (см. `audit.visual._axis_average`). Нужны сводке
    # вариантов (`VariantSummary`), не только HTML-отчёту.
    content_avg: float | None = None
    design_avg: float | None = None
    # Задача T: метрики верности шаблону на вариант — снимок
    # (`dataclasses.asdict(FidelityReport)`) для `api.app._variant_summary`,
    # посчитан в `_export_variant` рядом с экспортом (см. её докстроку).
    fidelity: dict | None = None

    def set_findings(self, findings: list[Finding]) -> None:
        self.findings = findings + self.visual_findings
        self.finding_index = {finding_id(f): f for f in self.findings}


@dataclass
class JobRecord:
    job_id: str
    template_id: str
    dir: Path
    # Задача Q: стиль этой презентации и пакет, если задание создано вместе
    # с соседями одной кнопкой (`create_batch`); `None` у одиночного.
    style: str = GenerationStyle.dense.value
    batch_id: str | None = None
    status: str = "running"
    stage: str | None = None
    stages: list[str] = field(default_factory=list)
    error: str | None = None
    profile: TemplateProfile | None = None
    # Результат задания под ключом его стиля: с задачи Q запись одна, но
    # эндпоинты `variants`/`fix`/`export` адресуют её по имени стиля.
    variants: dict[str, VariantState] = field(default_factory=dict)
    # Бюджет задания (`workflow.budget.RunBudget`): его же часы меряют
    # секунды каждой стадии для интерфейса и лога. `visual_audit`:
    # сводка стадии аудита по картинке (`VisualStageOutcome.summary`) под
    # ключом стиля, та же форма `{стиль: сводка}`, что читает интерфейс.
    budget: RunBudget | None = None
    visual_audit: dict[str, dict] | None = None
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
        budget = self.budget.summary() if self.budget is not None else None
        return {
            "job_id": self.job_id, "template_id": self.template_id, "status": self.status,
            "stage": self.stage, "stages": list(self.stages), "deck_id": self.job_id,
            "error": self.error,
            "style": self.style, "batch_id": self.batch_id,
            # Задача Q: режим и секунды задания на верхнем уровне снимка,
            # чтобы список заданий не разбирал бюджет целиком.
            "mode": budget["mode"] if budget else None,
            "seconds": budget["elapsed_seconds"] if budget else None,
            "budget": budget,
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
        друг за другом, так что время стадии: от входа в неё до входа в
        следующую."""
        # Здесь пишутся только разбор и структура; стадии после них
        # `_run_variant` пишет сам, а стадия здесь только метка прогресса.
        if (
            self.budget is not None and self.stage in SHARED_STAGES and self._stage_started is not None
        ):
            self.budget.record(self.stage, self.budget.clock() - self._stage_started)
        self._stage_started = None

    def enter_stage(self, stage: str) -> None:
        self.close_stage()
        self.stage = stage
        self.stages.append(stage)
        if self.budget is not None:
            self._stage_started = self.budget.clock()
        self._notify()

    def reach_stage(self, stage: str) -> None:
        """Метка прогресса без дублей и без шагов назад: `fix_deck` и
        повторные вызовы стадий не должны раздувать список в снимке."""
        if stage in self.stages:
            return
        if self.stage in STAGES and STAGES.index(stage) < STAGES.index(self.stage):
            return
        self.enter_stage(stage)

    def finish(self, *, error: str | None = None) -> None:
        self.close_stage()
        self.status = "error" if error else "done"
        self.error = error
        self._notify()


class OutlineShare:
    """Структура, общая для заданий одного пакета стилей (задача Q).

    Первое задание, дошедшее до структуры, запускает её расчёт, остальные
    ждут тот же результат. Входы у заданий пакета одинаковые (бриф,
    источники, шаблон), поэтому и структура одна; текст каждый стиль всё
    равно пишет свой, под свою композицию. Расчёт закрыт `asyncio.shield`:
    если одно задание отменят, соседи свою структуру всё равно получат.
    Проверка и запуск идут без `await` между ними, поэтому в одном цикле
    событий гонки за «кто первый» нет. Ошибка расчёта достаётся каждому
    ждущему заданию, и каждое честно падает с ней."""

    def __init__(self) -> None:
        self._task: asyncio.Future | None = None
        self._owner: str | None = None

    async def get(self, job_id: str, compute) -> tuple[Outline, bool]:
        """Структура и признак «получена готовой» (не этим заданием)."""
        if self._task is None:
            self._owner = job_id
            self._task = asyncio.ensure_future(compute())
        outline = await asyncio.shield(self._task)
        return outline, self._owner != job_id


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
        style: str | GenerationStyle = GenerationStyle.dense,
        batch_id: str | None = None, outline_share: OutlineShare | None = None,
    ) -> JobRecord:
        """Одно задание, одна презентация одного стиля. Без `outline_share`
        задание считает структуру само."""
        template = self.get_template(template_id)
        try:
            chosen = GenerationStyle.parse(style)
        except ValueError as exc:
            raise JobError(str(exc)) from exc
        job_id = uuid4().hex
        job_dir = self.root / "decks" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = JobRecord(
            job_id=job_id, template_id=template_id, dir=job_dir, profile=template.profile,
            style=chosen.value, batch_id=batch_id,
        )
        self.jobs[job_id] = job
        asyncio.create_task(_run_job(
            job=job, template=template, brief=brief, sources=sources, title=title,
            language=language, target_slides=target_slides, autofix=autofix,
            outline_share=outline_share if outline_share is not None else OutlineShare(),
        ))
        return job

    def create_batch(
        self, *, styles: list[str | GenerationStyle] | None = None, **job_args,
    ) -> tuple[str, list[JobRecord]]:
        """Несколько стилей одним запросом: по заданию на стиль, все
        параллельно и каждое в своём бюджете. Общая у них только структура
        (один `OutlineShare` на пакет): входы одинаковые, и платить за один
        и тот же вызов модели трижды незачем. Стили проверяются до создания
        первого задания, чтобы опечатка не оставляла пакет наполовину."""
        wanted = styles if styles else list(GenerationStyle)
        try:
            chosen = list(dict.fromkeys(GenerationStyle.parse(s) for s in wanted))
        except ValueError as exc:
            raise JobError(str(exc)) from exc
        self.get_template(job_args["template_id"])
        batch_id = uuid4().hex
        share = OutlineShare()
        jobs = [
            self.create_job(style=style, batch_id=batch_id, outline_share=share, **job_args)
            for style in chosen
        ]
        return batch_id, jobs

    def list_jobs(self, batch_id: str | None = None) -> list[JobRecord]:
        """Задания в порядке создания; с `batch_id` только задания пакета."""
        return [job for job in self.jobs.values() if batch_id is None or job.batch_id == batch_id]

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
    language: str, target_slides: int | None, autofix: bool, outline_share: OutlineShare,
) -> None:
    # Бюджет задания создаётся первым делом: пять минут ТЗ считаются от
    # начала генерации, всё, что было до (загрузка и разбор шаблона),
    # в него не входит.
    job.budget = _new_budget()
    style = GenerationStyle.parse(job.style)
    try:
        job.enter_stage("parse")
        profile = template.profile  # уже разобран при загрузке шаблона (POST /api/templates)
        job.profile = profile
        job.budget.mark_reused("parse")

        job.enter_stage("outline")
        source_docs = [SourceDoc(name=f"source-{i + 1}.md", text=text) for i, text in enumerate(sources)]

        async def compute_outline() -> Outline:
            return await asyncio.to_thread(
                build_outline, brief, source_docs, profile, _build_role_provider("outline"), target_slides,
                title=title or "Презентация", language=language,
            )

        outline, reused = await outline_share.get(job.job_id, compute_outline)
        if reused:
            job.budget.mark_reused("outline")

        # Структура на диск рядом с результатом задания; план стиля
        # (раскладки и текст) ляжет в `<стиль>/deck.json`.
        try:
            (job.dir / "outline.json").write_text(
                json.dumps(outline_to_dict(outline), ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — отладочный артефакт не вправе ронять генерацию
            pass

        # Первая контрольная точка: сколько осталось на раскладки, текст и
        # всё после них, если структура (своя или общая) шла долго.
        job.close_stage()
        job.budget.decide_mode("after_outline")
        config = AuditConfig.load()
        job.enter_stage("write")
        await _run_variant(
            job, style, outline, profile, template.path, source_docs, config, autofix, job.budget,
        )

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

        job.finish()
    except Exception as exc:  # noqa: BLE001 — любая причина отказа обязана дойти до пользователя API,
        # не остаться молчаливым "running" навсегда
        job.budget.stop()
        job.finish(error=str(exc) or type(exc).__name__)


async def _run_variant(
    job: JobRecord, variant: Variant, outline: Outline, profile: TemplateProfile, template_path: Path,
    sources: list[SourceDoc], config: AuditConfig, autofix: bool, budget: RunBudget,
) -> None:
    """Всё после структуры для стиля задания, в бюджете задания: раскладки
    на всю колоду (`pattern.plan_patterns`, миллисекунды, без модели),
    контракты, текст под них, сборка, аудит, экспорт, аудит по картинке.

    Контрольные точки режима после структуры: `after_write` после текста,
    `after_compose` перед аудитом по картинке."""
    started = budget.clock()
    _assignments, contracts = await asyncio.to_thread(
        plan_contracts, intents_from_outline(outline), profile, variant,
    )
    budget.record("plan", budget.clock() - started)

    started = budget.clock()
    variant_deck = await asyncio.to_thread(
        write_slides, outline, contracts, sources, profile, _build_writer(),
        max_workers=_writer_max_workers(), agent_max_steps=_writer_agent_max_steps(), style=variant,
    )
    budget.record("write", budget.clock() - started)
    budget.decide_mode("after_write")
    job.reach_stage("compose")

    started = budget.clock()
    built_path = await asyncio.to_thread(build_deck, variant_deck, profile, template_path, variant)
    dest_dir = job.dir / variant.value
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "deck.pptx"
    await asyncio.to_thread(shutil.copy2, built_path, dest)
    # План стиля рядом с его файлом: раскладки и текст у каждого стиля свои.
    # План dense ещё и под старым общим именем `deck.json` рядом с каталогами
    # стилей: так разбор жалобы «слайд выглядит плохо» начинается с того же
    # файла, что и раньше (25 сентября 2026: без плана это гадание).
    try:
        plan_json = json.dumps(deck_spec_to_dict(variant_deck), ensure_ascii=False, indent=2)
        (dest_dir / "deck.json").write_text(plan_json, encoding="utf-8")
        if variant is Variant.dense:
            (job.dir / "deck.json").write_text(plan_json, encoding="utf-8")
    except Exception:  # noqa: BLE001 — отладочный артефакт не вправе ронять генерацию
        pass
    job.variants[variant.value] = VariantState(variant=variant, deck_spec=variant_deck, pptx_path=dest)
    budget.record("compose", budget.clock() - started)

    budget.decide_mode("after_compose")

    job.reach_stage("audit")
    started = budget.clock()
    await _audit_variant(job, variant.value, profile, config, autofix)
    budget.record("audit", budget.clock() - started)

    job.reach_stage("export")
    started = budget.clock()
    await _export_variant(job, variant.value, profile)
    budget.record("export", budget.clock() - started)

    # Аудит по картинке идёт у каждого стиля: вёрстка своя, и вопросы
    # уровня колоды (C09/C11) читают именно её. Бюджет у задания свой
    # объект, общего лока с соседними заданиями не нужно.
    await _visual_audit_variant(job, variant.value, profile, sources, budget)
    budget.stop()


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


async def _visual_audit_variant(
    job: JobRecord, variant_name: str, profile: TemplateProfile, sources: list[SourceDoc], budget: RunBudget,
) -> None:
    """Аудит по картинке презентации задания, в бюджете задания (задача Q:
    один `RunBudget` на задание, соседние стили идут своими заданиями со
    своими бюджетами, поэтому общий лок не нужен). Вопросы уровня колоды
    (C09/C11, `workflow.visual_stage`) читают вёрстку этого стиля.

    Находки ложатся в тот же список, что и детерминированные; HTML-отчёт
    варианта перерисовывается с оценками модели. Сбой не роняет готовую
    колоду: стадия необязательная."""
    state = job.variants.get(variant_name)
    if state is None:
        return
    if job.visual_audit is None:
        job.visual_audit = {}
    try:
        deterministic = [f for f in state.findings if f not in state.visual_findings]
        outcome = await asyncio.to_thread(
            run_visual_stage, budget, state.deck_spec, deterministic,
            _build_visual_auditor(),
            lambda: list(state.preview_pngs), sources=sources,
            autofixed_slides=state.autofixed_slides, pptx_path=state.pptx_path,
        )
    except Exception as exc:  # noqa: BLE001: необязательная стадия не вправе ронять готовую колоду
        job.visual_audit[variant_name] = {"ran": False, "skipped_reason": f"аудит по картинке упал: {exc}"}
        return
    summary = outcome.summary()
    job.visual_audit[variant_name] = summary
    if outcome.result is not None:
        state.visual_findings = outcome.findings
        state.set_findings(deterministic)
        state.content_avg = outcome.result.content_avg
        state.design_avg = outcome.result.design_avg
    # HTML варианта перерисовывается в любом случае (не только когда модель
    # реально ответила): итоговый снимок бюджета (стадия visual_audit уже
    # посчитана `run_visual_stage`) и причина пропуска, если аудит не пошёл,
    # тоже часть отчёта задачи L, не только оценки модели.
    if state.html_path is not None:
        try:
            # `state.fidelity` — уже снятый слепок (`_export_variant`), не
            # пересчитывается здесь заново: тот же приём, что и с оценками
            # PPTEval, — перерисовка HTML не должна повторять дорогую работу.
            fidelity_obj = SimpleNamespace(**state.fidelity) if state.fidelity else None
            await asyncio.to_thread(
                to_html_report, state.deck_spec, profile, state.pptx_path, state.html_path,
                visual=outcome.result, budget=budget.summary(),
                risky_slides=summary.get("risk"), fidelity=fidelity_obj,
            )
        except Exception:  # noqa: BLE001: HTML без оценок лучше, чем упавшее задание
            pass


async def _export_variant(job: JobRecord, variant_name: str, profile: TemplateProfile) -> None:
    state = job.variants[variant_name]
    out_dir = job.dir / variant_name
    # Задача L: режим прогона уже решён (`decide_mode("after_compose")` в
    # `_run_variant` идёт до этой стадии) — снимок бюджета попадает в HTML сразу,
    # список рискованных слайдов (`risky_slides`) допишет только `_visual_
    # audit_dense`, перерисовав HTML dense-варианта заново, когда аудит
    # реально пройдёт.
    budget_summary = job.budget.summary() if job.budget is not None else None
    # Задача T: метрики верности шаблону — рядом с экспортом (не отдельная
    # стадия пайплайна), необязательны для готовой колоды: сбой метрики не
    # должен ронять экспорт (та же честная деградация, что и у остальных
    # необязательных шагов этого файла).
    fidelity_report = None
    try:
        fidelity_report = await asyncio.to_thread(template_fidelity, state.pptx_path, state.deck_spec, profile)
        state.fidelity = asdict(fidelity_report)
    except Exception:  # noqa: BLE001 — метрика необязательна, экспорт не вправе упасть из-за неё
        state.fidelity = None
    bundle = await asyncio.to_thread(
        export_bundle, state.pptx_path, profile, out_dir, deck_spec=state.deck_spec, budget=budget_summary,
        fidelity=fidelity_report,
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
