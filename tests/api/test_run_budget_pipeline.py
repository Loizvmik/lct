"""Бюджет прогона в API (задача H): с фейковой моделью аудита по картинке
пайплайн доходит до конца, стадия аудита рискованных слайдов идёт, пока
бюджет позволяет, и пропускается с причиной, когда не позволяет.

Один прогон генерации на модуль (она не бесплатна, см. `conftest.py`);
случай «бюджет исчерпан» проверяется повторным вызовом той же стадии на
уже готовом задании."""
from __future__ import annotations
import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from deckforge.api import jobs
from deckforge.plan.outline import load_content_pack
from deckforge.provider.base import VisionProvider
from deckforge.workflow.budget import BudgetPolicy, RunBudget

from .conftest import CONTENT_PACK, _poll_job


class _FakeVision(VisionProvider):
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        with self._lock:
            self.calls += 1
        keys = json.loads(prompt.rsplit("Служебные данные:\n", 1)[1])["answer_only_keys"]
        answer = {k: {"ok": True} for k in keys}
        answer["C08"] = {"ok": False, "where": "опечатка в заголовке"}
        return json.dumps({**answer, "scores": {"content": 3, "design": 3}})


@pytest.fixture(scope="module")
def fake_vlm() -> _FakeVision:
    return _FakeVision()


@pytest.fixture(scope="module")
def budget_job(client: TestClient, template_id: str, store, fake_vlm: _FakeVision):
    policy = BudgetPolicy(
        budget_seconds=10_000, rerank_min_remaining=120, visual_audit_min_remaining=75,
        visual_audit_max_slides=3, visual_audit_min_risk=0.0,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jobs, "_build_visual_auditor", lambda: fake_vlm)
        mp.setattr(jobs, "_new_budget", lambda: RunBudget.from_policy(policy))
        brief, sources, meta = load_content_pack(CONTENT_PACK)
        response = client.post("/api/decks", json={
            "template_id": template_id, "brief": brief, "sources": [s.text for s in sources],
            "title": meta.get("title", "queue-latency"), "language": "ru",
            "target_slides": 8, "autofix": True,
        })
        assert response.status_code == 200, response.text
        snapshot = _poll_job(client, response.json()["job_id"])
    return snapshot, store.get_job(snapshot["job_id"])


def test_pipeline_with_fake_model_runs_visual_audit(budget_job, fake_vlm: _FakeVision, client: TestClient):
    snapshot, job = budget_job
    assert snapshot["status"] == "done", snapshot
    visual = snapshot["visual_audit"]
    assert visual["ran"] is True, visual
    assert 1 <= len(visual["slides"]) <= 3
    assert fake_vlm.calls == len(visual["slides"])  # без коллажа C09/C11 и без повторов

    budget = snapshot["budget"]
    for stage in ("parse", "outline", "write", "compose", "audit", "export", "visual_audit"):
        assert stage in budget["stage_seconds"], budget
    assert budget["skipped"] == {}

    # Находки модели лежат в том же отчёте варианта dense, что и детерминированные.
    variants = client.get(f"/api/decks/{snapshot['deck_id']}/variants").json()
    dense = next(v for v in variants if v["variant"] == "dense")
    c08 = [f for f in dense["findings"] if f["check_id"] == "C08"]
    assert {f["slide_index"] for f in c08} == set(visual["slides"])
    assert len(job.variants["dense"].visual_findings) == len(c08)
    # Находки модели есть только у варианта, по которому шёл аудит.
    for other in variants:
        if other["variant"] != "dense":
            assert not any(f["check_id"] == "C08" for f in other["findings"])


def test_visual_audit_skipped_when_budget_exhausted(budget_job, fake_vlm: _FakeVision):
    _snapshot, job = budget_job
    calls_before = fake_vlm.calls

    class _Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = _Clock()
    job.budget = RunBudget.from_policy(BudgetPolicy(budget_seconds=300, visual_audit_min_remaining=75), clock=clock)
    clock.now = 260.0  # осталось 40с из 300
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jobs, "_build_visual_auditor", lambda: fake_vlm)
        asyncio.run(jobs._visual_audit_dense(job, job.profile, []))
    assert fake_vlm.calls == calls_before
    assert job.visual_audit["ran"] is False
    assert "осталось" in job.visual_audit["skipped_reason"]
    assert "visual_audit" in job.budget.skipped
