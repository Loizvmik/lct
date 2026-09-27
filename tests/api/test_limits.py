"""Пределы памяти и загрузки API (ревью 27 сентября 2026)."""
from __future__ import annotations
from types import SimpleNamespace

from deckforge.api import app as app_module
from deckforge.api import jobs as jobs_module


def test_finished_jobs_are_evicted_beyond_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_JOBS_IN_MEMORY", 3)
    store = jobs_module.JobStore(root=tmp_path)
    for i in range(5):
        store.jobs[f"j{i}"] = SimpleNamespace(job_id=f"j{i}", template_id="t", status="done" if i < 4 else "running")
    store._evict_finished()
    assert list(store.jobs) == ["j2", "j3", "j4"], "самые старые завершённые забыты, живое остаётся"


def test_templates_referenced_by_running_jobs_survive_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "MAX_TEMPLATES_IN_MEMORY", 1)
    store = jobs_module.JobStore(root=tmp_path)
    store.templates["old"] = SimpleNamespace(template_id="old")
    store.templates["live"] = SimpleNamespace(template_id="live")
    store.templates["new"] = SimpleNamespace(template_id="new")
    store.jobs["j"] = SimpleNamespace(job_id="j", template_id="live", status="running")
    store._evict_finished()
    assert "live" in store.templates and "old" not in store.templates


def test_oversized_template_upload_is_refused(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_TEMPLATE_BYTES", 1024)
    response = client.post("/api/templates", files={"file": ("big.pptx", b"P" * 4096, "application/octet-stream")})
    assert response.status_code == 413
