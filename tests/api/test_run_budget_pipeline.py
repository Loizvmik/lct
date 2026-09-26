"""Пакет из трёх стилей в API (задача Q) и бюджет задания (задача H): с
фейковыми моделями писателя и аудита по картинке каждое задание доходит до
конца в своём бюджете, структура считается один раз на пакет, текст
пишется у каждого стиля свой. Стадия аудита рискованных слайдов идёт,
пока бюджет позволяет, и пропускается с причиной, когда не позволяет.

Один пакет генерации на модуль (он не бесплатен, см. `conftest.py`);
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


class _FakeWriter(LLMProvider):
    """Писатель, который отвечает ровно по контракту слайда из запроса:
    ответ, который код обязан принять без ремонта. Считает вызовы по
    стилю, чтобы проверить, что текст пишется у каждого стиля свой."""

    def __init__(self) -> None:
        self.calls = 0
        self.styles: dict[str, int] = {}
        self._lock = threading.Lock()

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        from tests.plan.test_writer import _compliant_answer

        payload = json.loads(messages[1]["content"])
        with self._lock:
            self.calls += 1
            self.styles[payload.get("style")] = self.styles.get(payload.get("style"), 0) + 1
        return json.dumps(_compliant_answer(payload["contract"]), ensure_ascii=False)


@pytest.fixture(scope="module")
def fake_writer() -> _FakeWriter:
    return _FakeWriter()


class _OutlineSpy:
    """Считает вызовы структуры, отдавая настоящую (запасную) структуру."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()
        self._real = jobs.build_outline

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.calls += 1
        return self._real(*args, **kwargs)


@pytest.fixture(scope="module")
def outline_spy() -> _OutlineSpy:
    return _OutlineSpy()


@pytest.fixture(scope="module")
def budget_job(
    client: TestClient, template_id: str, store, fake_vlm: _FakeVision, fake_writer: _FakeWriter,
    outline_spy: _OutlineSpy,
):
    # Бюджет огромный (10_000с): все контрольные точки увидят щедрый
    # остаток и зафиксируют FULL.
    modes = {
        RunMode.FULL: ModeSpec(min_remaining=120, rerank=True, visual_audit_max_slides=3),
        RunMode.FAST: ModeSpec(min_remaining=75, rerank=False, visual_audit_max_slides=1),
        RunMode.EMERGENCY: ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=0),
    }
    policy = BudgetPolicy(budget_seconds=10_000, modes=modes, visual_audit_min_risk=0.0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jobs, "_build_visual_auditor", lambda: fake_vlm)
        mp.setattr(jobs, "_new_budget", lambda: RunBudget.from_policy(policy))
        mp.setattr(jobs, "_build_writer", lambda: fake_writer)
        mp.setattr(jobs, "build_outline", outline_spy)
        brief, sources, meta = load_content_pack(CONTENT_PACK)
        response = client.post("/api/decks/batch", json={
            "template_id": template_id, "brief": brief, "sources": [s.text for s in sources],
            "title": meta.get("title", "queue-latency"), "language": "ru",
            "target_slides": 8, "autofix": True,
        })
        assert response.status_code == 200, response.text
        body = response.json()
        snapshots = {}
        for job_id in body["job_ids"]:
            snapshot = _poll_job(client, job_id)
            snapshots[snapshot["style"]] = snapshot
    return body, snapshots, {style: store.get_job(s["job_id"]) for style, s in snapshots.items()}


def test_batch_creates_one_job_per_style_with_its_own_budget(budget_job, client: TestClient):
    """Задача Q: три стиля = три задания пакета, у каждого свой бюджет
    одним значением (без `variants`), свой режим и свои стадии."""
    body, snapshots, _jobs = budget_job
    assert len(body["job_ids"]) == 3 and len(set(body["job_ids"])) == 3
    assert set(snapshots) == {"dense", "airy", "visual"}
    listed = client.get("/api/jobs", params={"batch_id": body["batch_id"]}).json()
    assert [j["job_id"] for j in listed] == body["job_ids"]
    assert [j["style"] for j in listed] == ["dense", "airy", "visual"]
    for style, snapshot in snapshots.items():
        assert snapshot["status"] == "done", snapshot
        assert snapshot["batch_id"] == body["batch_id"]
        budget = snapshot["budget"]
        assert "variants" not in budget
        assert budget["budget_seconds"] == 10_000
        for stage in ("parse", "outline", "plan", "write", "compose", "audit", "export"):
            assert stage in budget["stage_seconds"], (style, budget)
        assert budget["skipped"] == {}
        assert budget["mode"] == "full" and snapshot["mode"] == "full", budget
        assert [e["checkpoint"] for e in budget["mode_history"]] == ["after_outline", "after_write", "after_compose"]
        assert "visual_audit" in budget["stage_seconds"]


def test_outline_is_computed_once_and_reused_by_the_other_styles(budget_job, outline_spy, fake_writer):
    """Структура считается один раз на пакет, текст пишет каждый стиль
    свой: писатель видел все три стиля, структура вызвана однажды, а два
    задания из трёх помечают её как переиспользованную."""
    _body, snapshots, jobs_by_style = budget_job
    assert outline_spy.calls == 1
    assert set(fake_writer.styles) == {"dense", "airy", "visual"}
    marks = [s["budget"]["shared_stages"] for s in snapshots.values()]
    assert all(m["parse"] == "переиспользовано" for m in marks)
    assert sorted("outline" in m for m in marks) == [False, True, True]
    outlines = {(job.dir / "outline.json").read_text(encoding="utf-8") for job in jobs_by_style.values()}
    assert len(outlines) == 1


def test_pipeline_with_fake_model_runs_visual_audit(budget_job, fake_vlm: _FakeVision, client: TestClient):
    """Аудит по картинке идёт в каждом задании пакета, находки модели
    лежат в отчёте каждого стиля."""
    _body, snapshots, jobs_by_style = budget_job
    expected_calls = 0
    for style, snapshot in snapshots.items():
        visual = snapshot["visual_audit"]
        assert set(visual) == {style}, visual
        summary = visual[style]
        assert summary["ran"] is True, summary
        assert 1 <= len(summary["slides"]) <= 3
        # Один вызов на рискованный слайд плюс один на коллаж C09/C11.
        expected_calls += len(summary["slides"]) + 1

        entries = client.get(f"/api/decks/{snapshot['deck_id']}/variants").json()
        assert [e["variant"] for e in entries] == [style]
        entry = entries[0]
        c08 = [f for f in entry["findings"] if f["check_id"] == "C08"]
        assert {f["slide_index"] for f in c08} == set(summary["slides"]), (style, entry["findings"])
        assert len(jobs_by_style[style].variants[style].visual_findings) == len(c08)
        assert entry["content_avg"] == pytest.approx(3.0)
        assert entry["design_avg"] == pytest.approx(3.0)
    assert fake_vlm.calls == expected_calls


def test_visual_audit_skipped_when_budget_exhausted(budget_job, fake_vlm: _FakeVision):
    """В режиме EMERGENCY аудит по картинке задания пропускается с
    причиной, модель не зовётся."""
    _body, _snapshots, jobs_by_style = budget_job
    calls_before = fake_vlm.calls

    class _Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    for style, job in jobs_by_style.items():
        clock = _Clock()
        job.budget = RunBudget.from_policy(BudgetPolicy(budget_seconds=300), clock=clock)
        clock.now = 260.0  # осталось 40с из 300 — ниже порога FAST (75), режим EMERGENCY
        job.budget.decide_mode("after_compose")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(jobs, "_build_visual_auditor", lambda: fake_vlm)
            asyncio.run(jobs._visual_audit_variant(job, style, job.profile, [], job.budget))
        summary = job.visual_audit[style]
        assert summary["ran"] is False
        assert "осталось" in summary["skipped_reason"]
        assert "visual_audit" in job.budget.skipped
    assert fake_vlm.calls == calls_before


def test_each_style_writes_its_own_text_under_its_own_layouts(budget_job):
    """Текст ложится в контракт (ремонтов нет, все места в пределах),
    раскладки колоды стиля из шаблона, и у стилей они разные."""
    _body, _snapshots, jobs_by_style = budget_job
    layouts = {}
    for style, job in jobs_by_style.items():
        known = {p.pattern_id for p in job.profile.patterns}
        deck = job.variants[style].deck_spec
        assert deck.meta["contract_repairs"] == "0", deck.meta
        assert deck.meta["contract_places_ok"] == deck.meta["contract_places"], deck.meta
        assert all(s.pattern_id in known for s in deck.slides)
        layouts[style] = [s.pattern_id for s in deck.slides]
        assert (job.dir / style / "deck.json").exists()
    assert layouts["dense"] != layouts["visual"]
