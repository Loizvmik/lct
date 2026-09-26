"""FastAPI-приложение DeckForge (Task 16, интерфейс брифа дословно):

- `POST /api/templates` — загрузка .pptx, `template_id` + `TemplateProfile`.
- `GET /api/templates/{id}/profile` — дизайн-система (тот же профиль).
- `POST /api/decks` — `{template_id, brief, sources, target_slides, autofix}`,
  возвращает `job_id`; вся генерация — фоновая задача (`api.jobs`).
- `GET /api/jobs/{id}` — прогресс по этапам (снимок), `GET /api/jobs/{id}/
  events` — тот же прогресс потоком SSE (Step 2 брифа: "клиент читает их
  через SSE").
- `GET /api/decks/{id}/variants` — три варианта с превью-PNG и находками
  аудита (координаты рамки — доли холста, как несёт `Finding.box`).
- `POST /api/decks/{id}/fix` — применяет выбранные исправления и
  пересобирает (`api.jobs.fix_deck`).
- `GET /api/decks/{id}/export` — скачивание готового файла
  (`?format=pptx|pdf|html&variant=dense|airy|visual`; `variant` — не из
  буквального перечня брифа, но без него нечем выбрать ИЗ ТРЁХ вариантов,
  какой именно экспортировать: по умолчанию `dense`).

Файлы (шаблоны, собранные колоды, превью) отдаются как есть через
`StaticFiles` (`/artifacts/...`) — интерфейсу нужны URL картинок для тега
`<img>`, а не байты внутри JSON-ответа.
"""
from __future__ import annotations
import json
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from deckforge.api import schemas
from deckforge.api.jobs import JobError, JobRecord, JobStore, STAGES, VariantState, finding_id, fix_deck
from deckforge.audit.findings import Finding

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
        autofixed_count=state.autofixed_count, fidelity=state.fidelity,
    )


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
    ) -> schemas.TemplateUploadResponse:
        data = await file.read()
        try:
            record = await store.create_template(data, file.filename or "template.pptx")
        except JobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    @app.post("/api/decks", response_model=schemas.DeckCreateResponse)
    async def create_deck(
        request: schemas.DeckCreateRequest, store: JobStore = Depends(get_store),
    ) -> schemas.DeckCreateResponse:
        try:
            job = store.create_job(
                template_id=request.template_id, brief=request.brief, sources=request.sources,
                title=request.title, language=request.language, target_slides=request.target_slides,
                autofix=request.autofix,
            )
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return schemas.DeckCreateResponse(job_id=job.job_id)

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
        # Порядок вариантов стабилен (Variant enum: dense, airy, visual) —
        # интерфейс сравнивает их бок о бок в одном и том же порядке всегда.
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
        variant: str = Query("dense", pattern="^(dense|airy|visual)$"),
        store: JobStore = Depends(get_store),
    ) -> FileResponse:
        job = _deck_or_404(store, deck_id)
        state = job.variants.get(variant)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Варианта {variant!r} нет в колоде {deck_id!r}.")
        path = getattr(state, _FORMAT_ATTR[format])
        if path is None or not Path(path).exists():
            raise HTTPException(status_code=404, detail=f"Файл формата {format!r} для варианта {variant!r} ещё не готов.")
        return FileResponse(path, media_type=_FORMAT_MEDIA[format], filename=Path(path).name)

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "stages": list(STAGES)}

    return app


app = create_app()
