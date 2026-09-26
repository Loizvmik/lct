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
from deckforge.provider.base import LLMProvider, VisionProvider
from deckforge.workflow.budget import BudgetPolicy, ModeSpec, RunBudget, RunMode

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


class _FakeRealizer(LLMProvider):
    """Переписывает слайд одной строкой из всех чисел исходника: короче
    любого места и с инвариантом смысла, то есть ответ, который код обязан
    принять."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        with self._lock:
            self.calls += 1
        payload = json.loads(messages[1]["content"])
        text = "Цифры: " + ", ".join(payload["must_keep"]) if payload["must_keep"] else "Коротко"
        return json.dumps({
            "kind": payload["slide"]["kind"], "headline": payload["slide"]["headline"],
            "blocks": [{"type": "text", "text": text}],
            "source_note": payload["slide"].get("source_note") or "Источник: пакет",
        }, ensure_ascii=False)


@pytest.fixture(scope="module")
def fake_realizer() -> _FakeRealizer:
    return _FakeRealizer()


@pytest.fixture(scope="module")
def budget_job(client: TestClient, template_id: str, store, fake_vlm: _FakeVision, fake_realizer: _FakeRealizer):
    # Задача L: `visual_audit_max_slides` (3, как раньше) теперь строка режима
    # FULL, не плоское поле политики. Бюджет огромный (10_000с) — обе
    # контрольные точки увидят щедрый остаток и зафиксируют FULL.
    modes = {
        RunMode.FULL: ModeSpec(min_remaining=120, rerank=True, visual_audit_max_slides=3),
        RunMode.FAST: ModeSpec(min_remaining=75, rerank=False, visual_audit_max_slides=1),
        RunMode.EMERGENCY: ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=0),
    }
    policy = BudgetPolicy(budget_seconds=10_000, modes=modes, visual_audit_min_risk=0.0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jobs, "_build_visual_auditor", lambda: fake_vlm)
        mp.setattr(jobs, "_new_budget", lambda: RunBudget.from_policy(policy))
        # Задача N: переписывание под вариант с фейковой моделью, без сети.
        mp.setattr(jobs, "_build_realizer", lambda: fake_realizer)
        mp.setattr(jobs, "_realize_settings", lambda: (True, 4, 60.0))
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
    # Задача N: общие стадии в бюджете прогона, помечены как общие; всё
    # после текста — в бюджете каждого варианта.
    assert budget["shared_stages"] == ["parse", "outline", "write"], budget
    assert set(budget["stage_seconds"]) == {"parse", "outline", "write"}, budget
    assert set(budget["variants"]) == {"dense", "airy", "visual"}, budget
    for name, part in budget["variants"].items():
        for stage in ("compose", "audit", "export"):
            assert stage in part["stage_seconds"], (name, part)
        # Дедлайн варианта — бюджет минус общие стадии.
        assert part["budget_seconds"] <= budget["budget_seconds"]
        assert part["elapsed_seconds"] <= budget["elapsed_seconds"]
        assert part["skipped"] == {}
        # Задача L: бюджет огромный, латентность обычная — обе контрольные
        # точки варианта обязаны сойтись на FULL.
        assert part["mode"] == "full", part
        assert part["mode_checkpoint"] == "after_compose", part
        assert [entry["mode"] for entry in part["mode_history"]] == ["full", "full"]
    assert "visual_audit" in budget["variants"]["dense"]["stage_seconds"]
    for name in ("airy", "visual"):
        assert "realize" in budget["variants"][name]["stage_seconds"]
    assert "realize" not in budget["variants"]["dense"]["stage_seconds"]

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
    job.budget = RunBudget.from_policy(BudgetPolicy(budget_seconds=300), clock=clock)
    clock.now = 260.0  # осталось 40с из 300 — ниже порога FAST (75), режим EMERGENCY
    job.budget.decide_mode("after_compose")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jobs, "_build_visual_auditor", lambda: fake_vlm)
        asyncio.run(jobs._visual_audit_dense(job, job.profile, []))
    assert fake_vlm.calls == calls_before
    assert job.visual_audit["ran"] is False
    assert "осталось" in job.visual_audit["skipped_reason"]
    assert "visual_audit" in job.budget.skipped


def test_realize_runs_per_variant_and_keeps_numbers(budget_job, fake_realizer: _FakeRealizer):
    """Задача N: переписывание идёт у airy и visual, у dense нет; принятых
    ответов не больше, чем вызовов модели, и на каждом переписанном слайде
    раскладка та же, что выбрал вариант."""
    _snapshot, job = budget_job
    assert "realize_overflowing" not in job.variants["dense"].deck_spec.meta
    accepted = 0
    for name in ("airy", "visual"):
        meta = job.variants[name].deck_spec.meta
        assert "realize_overflowing" in meta, meta
        accepted += int(meta["realize_accepted"])
        for slide in job.variants[name].deck_spec.slides:
            if any("текст переписан" in f for f in slide.findings):
                assert slide.pattern_id and slide.pattern_id in {p.pattern_id for p in job.profile.patterns}
    assert fake_realizer.calls >= accepted
