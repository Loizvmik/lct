"""Тесты API (Task 16, брифом дословно, Step 1) — адаптировано под реальные
имена полей `TemplateProfile`/`Finding` (бриф давал тест как псевдокод
интерфейса, не буквальную JSON-форму: `profile.palette_roles`, не
`profile.palette.roles`, см. `template/profile.py`).

Один реальный прогон генерации на весь модуль (`conftest.job_result`/
`deck_id`) — `test_three_variants_are_returned` и `test_fix_applies_only_
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


def test_snapshot_counts_slides_per_build_ladder_rung(job_result: dict) -> None:
    """Задача U: в снимке задания по каждому стилю сколько слайдов какой
    ступенью лестницы собрано; каждый слайд ровно на одной ступени."""
    ladder = job_result["ladder"]
    assert set(ladder) == {"dense", "airy", "visual"}
    for counts in ladder.values():
        assert list(counts) == ["clone", "adapt", "shorten", "split", "scratch"]
        assert sum(counts.values()) >= 10


def test_three_variants_are_returned(client: TestClient, deck_id: str) -> None:
    response = client.get(f"/api/decks/{deck_id}/variants")
    assert response.status_code == 200, response.text
    variants = response.json()
    assert len(variants) == 3
    assert {v["variant"] for v in variants} == {"dense", "airy", "visual"}
    assert all(v["preview_pngs"] for v in variants)
    assert all(v["slide_count"] >= 10 for v in variants)
    for v in variants:
        for finding in v["findings"]:
            assert finding["id"]
            assert finding["severity"] in ("critical", "major", "minor")


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

        pytest.skip("ни один из трёх вариантов не содержит автопочинимой находки (autofix=False у фикстуры)")
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


def test_export_unknown_format_is_rejected(client: TestClient, deck_id: str) -> None:
    response = client.get(f"/api/decks/{deck_id}/export", params={"format": "docx", "variant": "dense"})
    assert response.status_code == 422
