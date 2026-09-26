"""Задача W: три задания пакета стилей в своих 300 с при общем
планировщике вызовов модели. Модель фейковая: быстрая (все три
укладываются, вызовы и ожидание очереди видны в снимке) и зависшая (жёсткий
потолок обрывает письмо, задание доходит до экспорта с предупреждениями).

Пакет генерации дорогой (сборка и экспорт `soffice`), поэтому по одному на
сценарий и на модуль."""
from __future__ import annotations
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import deckforge.provider.scheduler as scheduler_module
from deckforge.api import jobs
from deckforge.plan.outline import load_content_pack
from deckforge.provider.base import LLMProvider
from deckforge.provider.scheduler import ModelScheduler
from deckforge.workflow.budget import BudgetPolicy, RunBudget

from .conftest import CONTENT_PACK, _poll_job


class _Writer(LLMProvider):
    """Отвечает по контракту через `delay` секунд. `hang` держит вызов,
    пока тест его не отпустит: так ведёт себя зависший запрос к модели."""

    def __init__(self, delay: float = 0.0, hang: threading.Event | None = None) -> None:
        self.delay = delay
        self.hang = hang
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        from tests.plan.test_writer import _compliant_answer

        with self._lock:
            self.calls += 1
        if self.hang is not None:
            self.hang.wait(120.0)
            raise RuntimeError("отпущен тестом")
        time.sleep(self.delay)
        payload = json.loads(messages[1]["content"])
        return json.dumps(_compliant_answer(payload["contract"]), ensure_ascii=False)


def _run_batch(client: TestClient, template_id: str, policy: BudgetPolicy, writer: _Writer, scheduler):
    brief, sources, meta = load_content_pack(CONTENT_PACK)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scheduler_module, "_DEFAULT", scheduler)
        mp.setattr(jobs, "_build_writer", lambda: writer)
        mp.setattr(jobs, "_build_visual_auditor", lambda: None)
        mp.setattr(jobs, "_new_budget", lambda: RunBudget.from_policy(policy))
        response = client.post("/api/decks/batch", json={
            "template_id": template_id, "brief": brief, "sources": [s.text for s in sources],
            "title": meta.get("title", "queue-latency"), "language": "ru",
            "target_slides": 8, "autofix": True,
        })
        assert response.status_code == 200, response.text
        return {
            snap["style"]: snap for snap in (_poll_job(client, job_id) for job_id in response.json()["job_ids"])
        }


@pytest.fixture(scope="module")
def fast_batch(client: TestClient, template_id: str):
    scheduler = ModelScheduler(6)
    writer = _Writer(delay=0.05)
    snapshots = _run_batch(client, template_id, BudgetPolicy(budget_seconds=300.0), writer, scheduler)
    return snapshots, scheduler, writer


# Бюджет для зависшей модели: отметка писателя на 45 - 30 = 15с, жёсткий
# потолок на 45 - 25 = 20с, дальше 25с на сборку, аудит и экспорт.
SLOW_BUDGET = 45.0
SLOW_POLICY = BudgetPolicy(
    budget_seconds=SLOW_BUDGET, compose_export_reserve_seconds=30.0, export_reserve_seconds=25.0,
    writer_call_seconds=1.0,
)


@pytest.fixture(scope="module")
def slow_batch(client: TestClient, template_id: str):
    release = threading.Event()
    scheduler = ModelScheduler(6)
    writer = _Writer(hang=release)
    try:
        snapshots = _run_batch(client, template_id, SLOW_POLICY, writer, scheduler)
    finally:
        release.set()
    return snapshots, writer


def test_three_jobs_with_a_fake_model_fit_their_budget(fast_batch):
    snapshots, scheduler, writer = fast_batch
    assert set(snapshots) == {"dense", "airy", "visual"}
    for style, snap in snapshots.items():
        assert snap["status"] == "done", (style, snap["error"])
        assert snap["outcome"] == "done" and snap["warnings"] == [], style
        assert snap["seconds"] < 300, style
        budget = snap["budget"]
        assert budget["calls_by_role"]["writer"]["calls"] > 0, style
        assert "queue_wait_seconds" in budget and budget["time_skipped"] == {}, style
    assert writer.calls == sum(s["budget"]["model_calls"] for s in snapshots.values())
    assert scheduler.peak <= 6


def test_hard_ceiling_finishes_the_job_when_the_model_hangs(slow_batch):
    """Модель зависла на каждом вызове. Задание не ждёт её за потолком:
    незаконченные слайды идут запасным вариантом, экспорт доходит до конца,
    итог `done_with_warnings` с перечнем."""
    snapshots, writer = slow_batch
    assert writer.calls > 0, "вызовы начались до отметки писателя"
    for style, snap in snapshots.items():
        assert snap["status"] == "done", (style, snap["error"])
        assert snap["outcome"] == "done_with_warnings", style
        assert any("время на текст вышло" in w for w in snap["warnings"]), (style, snap["warnings"])
        stages = snap["budget"]["stage_seconds"]
        # Письмо оборвано на потолке (20с от начала задания), а не ждёт
        # зависшую модель 120с.
        assert stages["write"] <= SLOW_BUDGET - SLOW_POLICY.export_reserve_seconds + 3.0, (style, stages)
        assert "export" in stages, style
        assert snap["seconds"] <= SLOW_BUDGET + 5.0, (style, snap["seconds"], stages)
