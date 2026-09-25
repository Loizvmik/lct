"""Инфраструктура тестов API (Task 16).

Один реальный прогон всего пайплайна (`POST /api/templates` -> `POST /api/
decks` -> дождаться job -> `deck_id`) на МОДУЛЬ, не на тест — генерация не
бесплатна (сборка трёх вариантов + детерминированный аудит + PDF/PNG-рендер
`soffice` на каждый), и `test_three_variants_are_returned`/`test_fix_
applies_only_the_selected_findings` работают над ОДНОЙ и той же уже готовой
колодой, а не гоняют генерацию заново каждая.

Сетевую модель тест заменяет детерминированным провайдером: проверяется
весь API/OOXML-путь без расхода токенов и зависимости от сети."""
from __future__ import annotations
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deckforge.api.app import create_app
from deckforge.api.jobs import JobStore
from deckforge.plan.coverage import extract_source_facts

# ЛЦТ2026 (не VK Tech) — разведано вручную: без ключа модели (запасной
# текст) сборка на этом шаблоне реально оставляет автопочинимые находки
# (L03/T03) на нескольких вариантах, VK Tech в этом же сценарии выходит
# идеально чистым (кроме неполнимой D05) — тесту `/fix` физически нечего
# было бы чинить.
TEMPLATE_PATH = Path("dataset/templates/ЛЦТ2026 Шаблон презентации.pptx")
TEST_BRIEF = "Показать руководителям результаты пилота и согласовать следующий этап."
TEST_SOURCES = (
    "В пилоте участвовали 4 отдела и 410 заявок. Срок обработки сократился "
    "на 27%. Доля просроченных заявок снизилась с 23% до 6%."
)


class DeterministicGenerationProvider:
    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        payload = json.loads(messages[1]["content"])
        if "brief" in payload:
            count = int(payload["target_slides"])
            slides = []
            for index in range(count):
                if index == 0:
                    kind, intent = "title", "Пилот подтвердил измеримый эффект"
                elif index == count - 1:
                    kind, intent = "closing", "Следующий этап можно согласовать на подтверждённых данных"
                else:
                    kind, intent = "data", f"Подтверждённый результат пилота — часть {index}"
                slides.append({"kind": kind, "intent": intent, "needs": []})
            return json.dumps({"slides": slides}, ensure_ascii=False)

        source_text = payload.get("sources", "")
        facts = extract_source_facts([source_text])
        position = payload.get("position", {})
        index = int(position.get("index", 0))
        fact = facts[index % len(facts)] if facts else None
        body = fact.context if fact else "Исходные материалы подтверждают вывод презентации."
        return json.dumps({
            "kind": "bullets",
            "headline": payload.get("intent", "Подтверждённый вывод"),
            "blocks": [{"type": "bullets", "items": [body]}],
            "source_note": "Источник: материалы пользователя" if any(ch.isdigit() for ch in body) else None,
        }, ensure_ascii=False)


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> JobStore:
    provider = DeterministicGenerationProvider()
    return JobStore(
        root=tmp_path_factory.mktemp("deckforge-api"),
        provider_factory=lambda _role: provider,
        template_provider_factory=lambda _role: None,
    )


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
    # autofix=False здесь намеренно: `test_fix_applies_only_the_selected_
    # findings` и обязательный обход интерфейса (см. отчёт задачи) проверяют
    # ИМЕННО экран выбора находок — с `autofix=True` починимые находки
    # исчезли бы ещё во время генерации, и экрану было бы нечего показать
    # (поведение `autofix=True` по умолчанию проверяет `tests/audit/test_
    # autofix.py` — тот же движок, `POST /api/decks` лишь вызывает его).
    response = client.post("/api/decks", json={
        "template_id": template_id, "brief": TEST_BRIEF, "sources": [TEST_SOURCES],
        "title": "Итоги пилота", "language": "ru",
        "target_slides": 6, "autofix": False,
    })
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    return _poll_job(client, job_id)


@pytest.fixture(scope="module")
def deck_id(job_result: dict) -> str:
    assert job_result["status"] == "done", job_result
    return job_result["deck_id"]
