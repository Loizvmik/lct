"""FastAPI-приложение DeckForge (Task 16, интерфейс брифа дословно):

- `POST /api/templates` — загрузка .pptx, `template_id` + `TemplateProfile`.
- `GET /api/templates/{id}/profile` — дизайн-система (тот же профиль).
- `POST /api/decks` — `{template_id, brief, sources, target_slides, autofix,
  style}`, возвращает `job_id`: одно задание = одна презентация одного
  стиля в своём бюджете 300 с (задача Q); генерация — фоновая задача.
- `POST /api/decks/batch` — то же с `styles` (по умолчанию все три):
  по заданию на стиль, параллельно, структура считается один раз;
  возвращает `batch_id` и `job_ids`.
- `POST /api/photos` — фотографии пользователя (multipart `files`, до 10
  jpg/png по 10 МБ, `captions` по порядку файлов), возвращает `photo_id`;
  задание ссылается на них полем `photos`. `POST /api/examples/{name}/
  photos` загружает так же фото контент-пакета примера из `fixtures/`.
- `GET /api/jobs` — список заданий (стиль, стадия, режим, секунды),
  `?batch_id=` оставляет только задания пакета.
- `GET /api/jobs/{id}` — прогресс по этапам (снимок), `GET /api/jobs/{id}/
  events` — тот же прогресс потоком SSE (Step 2 брифа: "клиент читает их
  через SSE").
- `GET /api/decks/{id}/variants` — презентация задания (список из одной
  записи со стилем задания) с превью-PNG и находками аудита.
- `POST /api/decks/{id}/fix` — применяет выбранные исправления и
  пересобирает (`api.jobs.fix_deck`).
- `POST /api/credentials/check` — проверка ключа Yandex AI Studio из
  заголовков `X-Yandex-Api-Key`/`X-Yandex-Folder-Id`. Те же заголовки на
  загрузке шаблона и создании заданий пускают модель на ключе пользователя
  вместо ключа сервера (`api.credentials`).
- `GET /api/decks/{id}/export` — скачивание готового файла
  (`?format=pptx|pdf|html`, `variant` необязателен: по умолчанию стиль
  задания).

Файлы (шаблоны, собранные колоды, превью) отдаются как есть через
`StaticFiles` (`/artifacts/...`) — интерфейсу нужны URL картинок для тега
`<img>`, а не байты внутри JSON-ответа.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile

# Предел загрузки шаблона. Самый крупный шаблон датасета 34 МБ (ЛЦТ2026);
# корпоративные шаблоны с фотографиями доходят до сотни.
MAX_TEMPLATE_BYTES = 150 * 1024 * 1024
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from deckforge.api import credentials as user_credentials
from deckforge.api import schemas
from deckforge.api.jobs import (
    APP_YAML_PATH, MAX_PHOTO_BYTES, MAX_PHOTOS_PER_UPLOAD, JobError, JobRecord, JobStore, PhotoRecord, PhotoRejected, STAGES,
    VariantState, finding_id, fix_deck,
)
from deckforge.audit.findings import Finding
from deckforge.settings import Settings

_FORMAT_ATTR = {"pptx": "pptx_path", "pdf": "pdf_path", "html": "html_path"}
_FORMAT_MEDIA = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf", "html": "text/html",
}


def _finding_model(f: Finding) -> schemas.FindingModel:
    box = None
    if f.box is not None:
        box = {"left": f.box.left, "top": f.box.top, "width": f.box.width, "height": f.box.height}
    return schemas.FindingModel(
        id=finding_id(f), check_id=f.check_id, severity=f.severity, slide_index=f.slide_index,
        shape_ref=f.shape_ref, message=f.message, box=box, fixable=f.fixable, fix_hint=f.fix_hint,
        repair=f.repair,
    )


def _png_url(store: JobStore, path: Path) -> str:
    return "/artifacts/" + str(path.resolve().relative_to(store.root.resolve())).replace("\\", "/")


def _variant_summary(store: JobStore, state: VariantState) -> schemas.VariantSummary:
    findings = [_finding_model(f) for f in state.findings]
    by_severity: dict[str, int] = {}
    by_check: dict[str, int] = {}
    for f in state.findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_check[f.check_id] = by_check.get(f.check_id, 0) + 1
    return schemas.VariantSummary(
        variant=state.variant.value, slide_count=len(state.deck_spec.slides),
        preview_pngs=[_png_url(store, p) for p in state.preview_pngs],
        findings=findings, by_severity=by_severity, by_check=by_check,
        autofixed_count=state.autofixed_count,
        content_avg=state.content_avg, design_avg=state.design_avg,
        fidelity=state.fidelity,
    )


def _photos_response(store: JobStore, records: list[PhotoRecord]) -> schemas.PhotoUploadResponse:
    return schemas.PhotoUploadResponse(photos=[
        schemas.UploadedPhoto(
            photo_id=r.photo_id, name=r.name, caption=r.caption, url=quote(_png_url(store, r.path)),
        )
        for r in records
    ])


def _job_args(request: schemas.DeckInput) -> dict:
    return dict(
        template_id=request.template_id, brief=request.brief, sources=request.sources,
        title=request.title, language=request.language, target_slides=request.target_slides,
        autofix=request.autofix, photos=[p.model_dump() for p in request.photos],
    )


def _request_credentials(
    x_yandex_api_key: str | None = Header(None), x_yandex_folder_id: str | None = Header(None),
) -> user_credentials.YandexCredentials | None:
    """Ключ пользователя из заголовков запроса, если пришла вся пара."""
    return user_credentials.from_headers(x_yandex_api_key, x_yandex_folder_id)


# Сервер без своего ключа требует ключ пользователя (`DECKFORGE_REQUIRE_
# MODEL_KEY=1` в юните systemd): иначе презентация молча собралась бы
# запасными путями без модели. Локально и в тестах выключено, там запасной
# путь работает как раньше.
MSG_NO_KEY = (
    "Не указан ключ Yandex AI Studio. Откройте «Настройки» (значок в правом верхнем углу) → "
    "раздел «Yandex AI Studio», вставьте API-ключ и ID каталога и нажмите «Проверить»."
)
_KEY_CHECK_TTL = 600.0
_key_checks: dict[str, tuple[float, bool, str]] = {}


def _require_key() -> bool:
    return os.environ.get("DECKFORGE_REQUIRE_MODEL_KEY", "").strip() in ("1", "true", "yes")


async def _ensure_model_key(creds: user_credentials.YandexCredentials | None) -> None:
    """Понятная ошибка до начала работы, а не презентация без модели: ключ
    не указан (и у сервера своего нет) или Yandex его не принял. Проверка
    ключа кэшируется на 10 минут по отпечатку, сам ключ не хранится."""
    if not _require_key():
        return
    if creds is None:
        if _server_has_key():
            return
        raise HTTPException(status_code=400, detail=MSG_NO_KEY)
    import hashlib
    import time as _time
    fingerprint = hashlib.sha256(f"{creds.api_key}\n{creds.folder_id}".encode()).hexdigest()
    cached = _key_checks.get(fingerprint)
    if cached is not None and _time.monotonic() - cached[0] < _KEY_CHECK_TTL:
        ok, message = cached[1], cached[2]
    else:
        ok, message = await asyncio.to_thread(user_credentials.check_credentials, creds)
        if ok or message != user_credentials.MSG_NO_NETWORK:
            _key_checks[fingerprint] = (_time.monotonic(), ok, message)
    if ok:
        return
    if message == user_credentials.MSG_NO_NETWORK:
        raise HTTPException(status_code=503, detail="Нет связи с Yandex Cloud. Попробуйте ещё раз через минуту.")
    raise HTTPException(
        status_code=400,
        detail=f"Ключ Yandex AI Studio не принят: {message}. Проверьте API-ключ и ID каталога в «Настройках».",
    )


def _server_has_key() -> bool:
    try:
        settings = Settings.load(APP_YAML_PATH)
    except Exception:  # noqa: BLE001 — нечитаемый конфиг для интерфейса значит «ключа нет»
        return False
    return bool(settings.yandex_api_key and settings.yandex_folder_id)


def _model_info() -> dict | None:
    """Модель генерации по умолчанию из реестра (`config/models.yaml`):
    интерфейс показывает её в настройках. Секретов здесь нет."""
    try:
        settings = Settings.load(APP_YAML_PATH)
        from deckforge.provider.registry import assert_allowed
        card = assert_allowed(settings.llm.model)
    except Exception:  # noqa: BLE001 — сведения для интерфейса, не повод ронять health
        return None
    return {
        "id": card.id, "hf": card.hf, "license": card.license, "params_b": card.params_b,
        "active_params_b": card.active_params_b, "vision": card.vision, "provider": "Yandex AI Studio",
    }


def create_app(store: JobStore | None = None) -> FastAPI:
    app = FastAPI(title="DeckForge API", description="Генерация презентаций по .pptx-шаблону")
    app.state.store = store if store is not None else JobStore()
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )

    def get_store() -> JobStore:
        return app.state.store

    def _mounted_static_root() -> Path:
        root = app.state.store.root
        root.mkdir(parents=True, exist_ok=True)
        return root

    app.mount("/artifacts", StaticFiles(directory=str(_mounted_static_root())), name="artifacts")

    def _job_or_404(store: JobStore, job_id: str) -> JobRecord:
        try:
            return store.get_job(job_id)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _deck_or_404(store: JobStore, deck_id: str) -> JobRecord:
        try:
            return store.get_deck(deck_id)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/templates", response_model=schemas.TemplateUploadResponse)
    async def upload_template(
        file: UploadFile = File(...), store: JobStore = Depends(get_store),
        creds: user_credentials.YandexCredentials | None = Depends(_request_credentials),
    ) -> schemas.TemplateUploadResponse:
        # Тело читается не дальше предела (ревью 27 сентября 2026): без
        # него файл любого размера целиком ложился в память процесса.
        data = await file.read(MAX_TEMPLATE_BYTES + 1)
        if len(data) > MAX_TEMPLATE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Шаблон больше {MAX_TEMPLATE_BYTES // (1024 * 1024)} МБ.",
            )
        await _ensure_model_key(creds)
        # Ключ ставится на время разбора: провайдеры строятся внутри
        # `create_template` и берут его из контекста (`api.credentials`).
        token = user_credentials.use(creds)
        try:
            record = await store.create_template(data, file.filename or "template.pptx")
        except JobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            user_credentials.reset(token)
        return schemas.TemplateUploadResponse(
            template_id=record.template_id, profile=json.loads(record.profile.to_json()),
        )

    @app.get("/api/templates/{template_id}/profile", response_model=schemas.ProfileResponse)
    async def get_profile(template_id: str, store: JobStore = Depends(get_store)) -> schemas.ProfileResponse:
        try:
            record = store.get_template(template_id)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return schemas.ProfileResponse(template_id=template_id, profile=json.loads(record.profile.to_json()))

    @app.post("/api/photos", response_model=schemas.PhotoUploadResponse)
    async def upload_photos(
        files: list[UploadFile] = File(...), captions: list[str] = Form(default=[]),
        store: JobStore = Depends(get_store),
    ) -> schemas.PhotoUploadResponse:
        # Число файлов проверяется до чтения тел (ревью codex 27 сентября
        # 2026): иначе пачка из сотни файлов по 10 МБ ложилась в память до
        # отказа. Читается на байт больше предела: этого хватает, чтобы
        # отличить «ровно 10 МБ» от «больше», не держа весь лишний файл.
        if len(files) > MAX_PHOTOS_PER_UPLOAD:
            raise HTTPException(
                status_code=400,
                detail=f"Фотографий {len(files)}, а за одну загрузку можно не больше {MAX_PHOTOS_PER_UPLOAD}.",
            )
        payload = [(f.filename or "photo", await f.read(MAX_PHOTO_BYTES + 1)) for f in files]
        try:
            records = await asyncio.to_thread(store.save_photos, payload, list(captions))
        except PhotoRejected as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return _photos_response(store, records)

    @app.post("/api/examples/{name}/photos", response_model=schemas.PhotoUploadResponse)
    async def upload_example_photos(name: str, store: JobStore = Depends(get_store)) -> schemas.PhotoUploadResponse:
        try:
            records = await asyncio.to_thread(store.save_example_photos, name)
        except PhotoRejected as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _photos_response(store, records)

    @app.post("/api/decks", response_model=schemas.DeckCreateResponse)
    async def create_deck(
        request: schemas.DeckCreateRequest, store: JobStore = Depends(get_store),
        creds: user_credentials.YandexCredentials | None = Depends(_request_credentials),
    ) -> schemas.DeckCreateResponse:
        await _ensure_model_key(creds)
        # Фоновая задача задания копирует контекст при создании
        # (`asyncio.create_task`) и уносит ключ с собой; сброс после
        # создания её копию не трогает.
        token = user_credentials.use(creds)
        try:
            job = store.create_job(style=request.style, **_job_args(request))
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            user_credentials.reset(token)
        return schemas.DeckCreateResponse(job_id=job.job_id)

    @app.post("/api/decks/batch", response_model=schemas.DeckBatchResponse)
    async def create_deck_batch(
        request: schemas.DeckBatchRequest, store: JobStore = Depends(get_store),
        creds: user_credentials.YandexCredentials | None = Depends(_request_credentials),
    ) -> schemas.DeckBatchResponse:
        await _ensure_model_key(creds)
        token = user_credentials.use(creds)
        try:
            batch_id, jobs = store.create_batch(styles=list(request.styles), **_job_args(request))
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            user_credentials.reset(token)
        return schemas.DeckBatchResponse(batch_id=batch_id, job_ids=[job.job_id for job in jobs])

    @app.get("/api/jobs", response_model=list[schemas.JobResponse])
    async def list_jobs(
        batch_id: str | None = Query(None), store: JobStore = Depends(get_store),
    ) -> list[schemas.JobResponse]:
        return [schemas.JobResponse(**job.snapshot()) for job in store.list_jobs(batch_id)]

    @app.get("/api/jobs/{job_id}", response_model=schemas.JobResponse)
    async def get_job(job_id: str, store: JobStore = Depends(get_store)) -> schemas.JobResponse:
        job = _job_or_404(store, job_id)
        return schemas.JobResponse(**job.snapshot())

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, store: JobStore = Depends(get_store)) -> StreamingResponse:
        job = _job_or_404(store, job_id)

        async def gen():
            queue = job.subscribe()
            try:
                while True:
                    snapshot = await queue.get()
                    yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                    if snapshot["status"] in ("done", "error"):
                        break
            finally:
                job.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/decks/{deck_id}/variants", response_model=list[schemas.VariantSummary])
    async def get_variants(deck_id: str, store: JobStore = Depends(get_store)) -> list[schemas.VariantSummary]:
        job = _deck_or_404(store, deck_id)
        # С задачи Q в задании одна презентация; список оставлен, чтобы
        # экран аудита и старые клиенты читали ту же форму ответа.
        return [_variant_summary(store, job.variants[name]) for name in ("dense", "airy", "visual") if name in job.variants]

    @app.post("/api/decks/{deck_id}/fix", response_model=schemas.FixResponse)
    async def fix(
        deck_id: str, request: schemas.FixRequest, store: JobStore = Depends(get_store),
    ) -> schemas.FixResponse:
        job = _deck_or_404(store, deck_id)
        try:
            outcome = await fix_deck(job, request.variant, request.finding_ids)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return schemas.FixResponse(
            variant=request.variant, applied=outcome.applied, skipped=outcome.skipped,
            findings=[_finding_model(f) for f in outcome.findings],
            preview_pngs=[_png_url(store, p) for p in outcome.preview_pngs],
        )

    @app.get("/api/decks/{deck_id}/export")
    async def export(
        deck_id: str,
        format: str = Query(..., pattern="^(pptx|pdf|html)$"),
        variant: str | None = Query(None, pattern="^(dense|airy|visual)$"),
        store: JobStore = Depends(get_store),
    ) -> FileResponse:
        job = _deck_or_404(store, deck_id)
        variant = variant or job.style
        state = job.variants.get(variant)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Варианта {variant!r} нет в колоде {deck_id!r}.")
        path = getattr(state, _FORMAT_ATTR[format])
        if path is None or not Path(path).exists():
            raise HTTPException(status_code=404, detail=f"Файл формата {format!r} для варианта {variant!r} ещё не готов.")
        return FileResponse(path, media_type=_FORMAT_MEDIA[format], filename=Path(path).name)

    @app.post("/api/credentials/check", response_model=schemas.CredentialsCheckResponse)
    async def check_credentials(
        creds: user_credentials.YandexCredentials | None = Depends(_request_credentials),
    ) -> schemas.CredentialsCheckResponse:
        if creds is None:
            raise HTTPException(
                status_code=400, detail="Нужны оба заголовка: X-Yandex-Api-Key и X-Yandex-Folder-Id.",
            )
        ok, message = await asyncio.to_thread(user_credentials.check_credentials, creds)
        return schemas.CredentialsCheckResponse(ok=ok, message=message)

    @app.get("/api/health")
    async def health() -> dict:
        # `server_key`: интерфейс по нему решает, просить ли человека
        # вставить свой ключ (без ключа генерация идёт запасными путями).
        return {"status": "ok", "stages": list(STAGES), "server_key": _server_has_key(), "model": _model_info(),
                "key_required": _require_key() and not _server_has_key()}

    return app


app = create_app()
