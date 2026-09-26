"""Тесты API (Task 16, брифом дословно, Step 1) — адаптировано под реальные
имена полей `TemplateProfile`/`Finding` (бриф давал тест как псевдокод
интерфейса, не буквальную JSON-форму: `profile.palette_roles`, не
`profile.palette.roles`, см. `template/profile.py`).

Один реальный прогон генерации на весь модуль (`conftest.job_result`/
`deck_id`) — `test_one_job_returns_one_presentation_of_its_style` и `test_fix_applies_only_
the_selected_findings` работают над результатом ОДНОЙ и той же колоды,
поднятой `test_generation_reports_progress_by_stage`/`deck_id` фикстурой."""
from __future__ import annotations

from fastapi.testclient import TestClient

STAGES_IN_ORDER = ["parse", "outline", "write", "compose", "audit", "export"]


def test_upload_template_returns_profile(client: TestClient) -> None:
    from pathlib import Path

    template_path = Path("dataset/templates/VK Tech шаблон.pptx")
    with template_path.open("rb") as fh:
        response = client.post("/api/templates", files={"file": (template_path.name, fh)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template_id"]
    assert body["profile"]["patterns"]
    assert body["profile"]["palette_roles"]["brand"].startswith("#")
    assert body["profile"]["layouts"]
    assert "provenance" in body["profile"] and body["profile"]["provenance"]


def test_template_profile_is_fetchable_by_id(client: TestClient, template_id: str) -> None:
    response = client.get(f"/api/templates/{template_id}/profile")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template_id"] == template_id
    assert body["profile"]["patterns"]


def test_rejected_template_gives_a_readable_error(client: TestClient) -> None:
    response = client.post("/api/templates", files={"file": ("x.txt", b"not a pptx")})
    assert response.status_code == 400
    assert "pptx" in response.json()["detail"].lower()


def test_generation_reports_progress_by_stage(job_result: dict) -> None:
    assert job_result["status"] == "done", job_result
    assert job_result["stages"] == STAGES_IN_ORDER


def test_one_job_returns_one_presentation_of_its_style(client: TestClient, deck_id: str, job_result: dict) -> None:
    """Задача Q: одно задание = одна презентация одного стиля, бюджет
    одним значением, без разбивки по вариантам."""
    assert job_result["style"] == "dense"
    assert job_result["mode"] in ("full", "fast", "emergency")
    assert job_result["seconds"] > 0
    budget = job_result["budget"]
    assert "variants" not in budget
    # Разбор сделан при загрузке шаблона; структуру задание считало само.
    assert budget["shared_stages"] == {"parse": "переиспользовано"}
    assert [e["checkpoint"] for e in budget["mode_history"]] == ["after_outline", "after_write", "after_compose"]

def test_snapshot_counts_slides_per_build_ladder_rung(job_result: dict) -> None:
    """Задача U: в снимке задания сколько слайдов какой ступенью лестницы
    собрано; после задачи Q задание несёт один стиль, и счётчик один."""
    ladder = job_result["ladder"]
    assert set(ladder) == {job_result["style"]}
    for counts in ladder.values():
        assert list(counts) == ["clone", "adapt", "shorten", "split", "scratch"]
        assert sum(counts.values()) >= 10


def test_three_variants_are_returned(client: TestClient, deck_id: str) -> None:
    response = client.get(f"/api/decks/{deck_id}/variants")
    assert response.status_code == 200, response.text
    variants = response.json()
    assert [v["variant"] for v in variants] == ["dense"]
    assert all(v["preview_pngs"] for v in variants)
    assert all(v["slide_count"] >= 10 for v in variants)
    for v in variants:
        for finding in v["findings"]:
            assert finding["id"]
            assert finding["severity"] in ("critical", "major", "minor")


def test_jobs_list_shows_style_stage_mode_and_seconds(client: TestClient, deck_id: str) -> None:
    listed = {j["job_id"]: j for j in client.get("/api/jobs").json()}
    entry = listed[deck_id]
    assert entry["style"] == "dense" and entry["status"] == "done"
    assert entry["stage"] == "export" and entry["mode"] and entry["seconds"] > 0
    assert entry["batch_id"] is None
    assert client.get("/api/jobs", params={"batch_id": "нет-такого"}).json() == []


def test_unknown_style_is_rejected(client: TestClient, template_id: str) -> None:
    response = client.post("/api/decks", json={"template_id": template_id, "brief": "x", "style": "bold"})
    assert response.status_code == 422
    response = client.post("/api/decks/batch", json={"template_id": template_id, "brief": "x", "styles": []})
    assert response.status_code == 422


def test_fix_applies_only_the_selected_findings(client: TestClient, deck_id: str) -> None:
    variants = client.get(f"/api/decks/{deck_id}/variants").json()
    before, chosen_id = None, None
    for candidate in variants:
        fixable = [f for f in candidate["findings"] if f["fixable"]]
        if fixable:
            before, chosen_id = candidate, fixable[0]["id"]
            break
    if before is None:
        import pytest

        pytest.skip("презентация задания не содержит автопочинимой находки (autofix=False у фикстуры)")
    variant = before["variant"]

    response = client.post(f"/api/decks/{deck_id}/fix", json={"variant": variant, "finding_ids": [chosen_id]})
    assert response.status_code == 200, response.text
    after = response.json()

    # "Применяет ТОЛЬКО выбранные исправления": находка исчезла, а находки
    # НА ДРУГИХ слайдах остались на месте буквально (правка одной фигуры не
    # может задеть чужой слайд). На ТОМ ЖЕ слайде возможен честный побочный
    # эффект — например, D05 (заполненность слайда) — метрика ВСЕГО слайда,
    # и починка переполненного текста может сама вернуть её в допуск; ровно
    # однодефектный сценарий ("минус один, ничего больше") разбирает
    # прицельно `tests/audit/test_autofix.py`, здесь проверяется контракт
    # эндпоинта на реальной колоде, а не идеальная изоляция каждой находки.
    chosen_slide = next(f["slide_index"] for f in before["findings"] if f["id"] == chosen_id)
    after_ids = {f["id"] for f in after["findings"]}
    other_slide_ids = {f["id"] for f in before["findings"] if f["slide_index"] != chosen_slide}
    assert chosen_id in after["applied"]
    assert chosen_id not in after_ids
    assert other_slide_ids <= after_ids


def test_export_pptx_is_downloadable(client: TestClient, deck_id: str) -> None:
    response = client.get(f"/api/decks/{deck_id}/export", params={"format": "pptx", "variant": "dense"})
    assert response.status_code == 200
    assert response.content[:2] == b"PK"  # .pptx — ZIP-контейнер
    # Без `variant` отдаётся стиль задания.
    assert client.get(f"/api/decks/{deck_id}/export", params={"format": "pptx"}).content == response.content


def test_export_unknown_format_is_rejected(client: TestClient, deck_id: str) -> None:
    response = client.get(f"/api/decks/{deck_id}/export", params={"format": "docx", "variant": "dense"})
    assert response.status_code == 422
