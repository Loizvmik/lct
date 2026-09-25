"""Инфраструктура тестов API (Task 16).

Один реальный прогон всего пайплайна (`POST /api/templates` -> `POST /api/
decks` -> дождаться job -> `deck_id`) на МОДУЛЬ, не на тест — генерация не
бесплатна (сборка трёх вариантов + детерминированный аудит + PDF/PNG-рендер
`soffice` на каждый), и `test_three_variants_are_returned`/`test_fix_
applies_only_the_selected_findings` работают над ОДНОЙ и той же уже готовой
колодой, а не гоняют генерацию заново каждая.

Без ключа/сети (`YANDEX_API_KEY` не в окружении процесса `pytest` — `.env`
никто не грузит автоматически, см. `deckforge.settings.Settings.load`) —
outline/текст слайдов идут запасным вариантом, тот же принцип честной
деградации, что и у всего остального пайплайна; на итог теста (сама
генерация отработала, аудит нашёл что-то по-настоящему собранное) это не
влияет."""
from __future__ import annotations
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deckforge.api.app import create_app
from deckforge.api.jobs import JobStore
from deckforge.plan.outline import load_content_pack

from _profile_cache_isolation import write_isolated_app_yaml

# ЛЦТ2026 (не VK Tech) — разведано вручную: без ключа модели (запасной
# текст) сборка на этом шаблоне реально оставляет автопочинимые находки
# (L03/T03) на нескольких вариантах, VK Tech в этом же сценарии выходит
# идеально чистым (кроме неполнимой D05) — тесту `/fix` физически нечего
# было бы чинить.
TEMPLATE_PATH = Path("dataset/templates/ЛЦТ2026 Шаблон презентации.pptx")
CONTENT_PACK = Path("fixtures/content-packs/queue-latency")


@pytest.fixture(scope="module", autouse=True)
def _isolate_profile_cache_for_module(tmp_path_factory: pytest.TempPathFactory):
    """Генерация ниже идёт в фикстурах уровня модуля, то есть РАНЬШЕ
    функциональной изоляции кеша из `tests/conftest.py` — без этой
    фикстуры разбор шаблона без ключа модели ложился в общий
    `cache/profiles/` (см. докстроку `tests/_profile_cache_isolation.py`)."""
    root = tmp_path_factory.mktemp("deckforge-api-cache")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("deckforge.template.profile.APP_YAML_PATH", write_isolated_app_yaml(root))
        yield


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> JobStore:
    return JobStore(root=tmp_path_factory.mktemp("deckforge-api"))


@pytest.fixture(scope="module")
def client(store: JobStore):
    app = create_app(store=store)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def template_id(client: TestClient) -> str:
    with TEMPLATE_PATH.open("rb") as fh:
        response = client.post(
            "/api/templates",
            files={"file": (TEMPLATE_PATH.name, fh, "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
        )
    assert response.status_code == 200, response.text
    return response.json()["template_id"]


def _poll_job(client: TestClient, job_id: str, *, timeout_s: float = 280.0) -> dict:
    """Поллит `GET /api/jobs/{id}` до статуса `done`/`error` и возвращает
    последний снимок — тот же путь, каким читает прогресс UI без SSE
    (страховка на случай, если клиент опрашивает вместо подписки на поток)."""
    import time

    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.3)
    raise TimeoutError(f"Задание {job_id} не завершилось за {timeout_s}с: {last}")


@pytest.fixture(scope="module")
def job_result(client: TestClient, template_id: str) -> dict:
    """Один полный прогон генерации на весь модуль — и
    `test_generation_reports_progress_by_stage`, и `deck_id` (фикстура
    ниже) читают ОДИН и тот же результат, вместо того чтобы гонять
    генерацию дважды."""
    brief, sources, meta = load_content_pack(CONTENT_PACK)
    # autofix=False здесь намеренно: `test_fix_applies_only_the_selected_
    # findings` и обязательный обход интерфейса (см. отчёт задачи) проверяют
    # ИМЕННО экран выбора находок — с `autofix=True` починимые находки
    # исчезли бы ещё во время генерации, и экрану было бы нечего показать
    # (поведение `autofix=True` по умолчанию проверяет `tests/audit/test_
    # autofix.py` — тот же движок, `POST /api/decks` лишь вызывает его).
    response = client.post("/api/decks", json={
        "template_id": template_id, "brief": brief, "sources": [s.text for s in sources],
        "title": meta.get("title", "queue-latency"), "language": meta.get("language", "ru"),
        "target_slides": 12, "autofix": False,
    })
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    return _poll_job(client, job_id)


@pytest.fixture(scope="module")
def deck_id(job_result: dict) -> str:
    assert job_result["status"] == "done", job_result
    return job_result["deck_id"]
