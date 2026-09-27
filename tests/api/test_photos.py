"""Задача D2: фотографии через API. Загрузка (`POST /api/photos`), отказ
по размеру и типу, задание с фото, которое доходит до файла тем же путём,
что `photos/` контент-пакета в CLI.

Модель не зовётся: распределение фото подменено детерминированным (одно
фото на первый слайд после титульного), иначе без ключа планировщик фото
честно ничего не распределяет, и проверять было бы нечего."""
from __future__ import annotations
from io import BytesIO
from pathlib import Path

import PIL.Image
import pytest
from fastapi.testclient import TestClient

from deckforge.api import jobs
from deckforge.plan.outline import load_content_pack
from deckforge.plan.photos import PhotoAssignmentReport

from .conftest import CONTENT_PACK, _poll_job

PHOTOS_DIR = CONTENT_PACK / "photos"


def _png_bytes(size: tuple[int, int] = (40, 30)) -> bytes:
    buf = BytesIO()
    PIL.Image.new("RGB", size, (200, 80, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _upload(client: TestClient, files: list[tuple[str, bytes, str]], captions: list[str] | None = None):
    return client.post(
        "/api/photos",
        files=[("files", item) for item in files],
        data={"captions": captions or []},
    )


def test_upload_returns_photo_ids_and_serves_the_file(client: TestClient) -> None:
    jpg = PHOTOS_DIR / "approver-laptop-review.jpg"
    response = _upload(
        client, [(jpg.name, jpg.read_bytes(), "image/jpeg"), ("схема.png", _png_bytes(), "image/png")],
        captions=["Согласующий смотрит заявку", ""],
    )
    assert response.status_code == 200, response.text
    photos = response.json()["photos"]
    assert [p["name"] for p in photos] == [jpg.name, "схема.png"]
    assert photos[0]["caption"] == "Согласующий смотрит заявку"
    assert photos[1]["caption"] is None
    assert len({p["photo_id"] for p in photos}) == 2
    served = client.get(photos[0]["url"])
    assert served.status_code == 200 and served.content == jpg.read_bytes()
    assert client.get(photos[1]["url"]).status_code == 200


def test_upload_rejects_wrong_type(client: TestClient) -> None:
    response = _upload(client, [("notes.txt", b"hello", "text/plain")])
    assert response.status_code == 415
    # Расширение картинки, внутри не картинка.
    response = _upload(client, [("fake.jpg", b"not really a jpeg", "image/jpeg")])
    assert response.status_code == 415
    # PNG под именем .jpg: сборка вставила бы не то, что обещано.
    response = _upload(client, [("mislabeled.jpg", _png_bytes(), "image/jpeg")])
    assert response.status_code == 415


def test_upload_rejects_oversized_and_too_many(client: TestClient, store: jobs.JobStore) -> None:
    before = len(store.photos)
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (jobs.MAX_PHOTO_BYTES + 1)
    response = _upload(client, [("ok.png", _png_bytes(), "image/png"), ("big.png", big, "image/png")])
    assert response.status_code == 413
    # Пачка проверяется целиком: годный первый файл тоже не сохранён.
    assert len(store.photos) == before
    many = [(f"p{i}.png", _png_bytes(), "image/png") for i in range(jobs.MAX_PHOTOS_PER_UPLOAD + 1)]
    assert _upload(client, many).status_code == 400
    assert len(store.photos) == before


def test_upload_keeps_file_inside_its_folder(store: jobs.JobStore) -> None:
    [record] = store.save_photos([("../../evil.png", _png_bytes())], [None])
    assert record.name == "evil.png"
    assert record.path.parent.parent == store.root / "photos"


def test_example_photos_come_from_the_content_pack(client: TestClient) -> None:
    response = client.post("/api/examples/queue-latency/photos")
    assert response.status_code == 200, response.text
    photos = response.json()["photos"]
    assert sorted(p["name"] for p in photos) == sorted(p.name for p in PHOTOS_DIR.glob("*.jpg"))
    assert all(p["caption"] for p in photos)
    assert client.post("/api/examples/..%2F..%2Fsrc/photos").status_code == 404
    assert client.post("/api/examples/нет-такого/photos").status_code == 404


def test_unknown_photo_id_is_rejected(client: TestClient, template_id: str) -> None:
    response = client.post("/api/decks", json={
        "template_id": template_id, "brief": "x", "photos": [{"photo_id": "нет-такого"}],
    })
    assert response.status_code == 404
    assert "загрузите" in response.json()["detail"]


def test_same_file_name_twice_gets_distinct_names(store: jobs.JobStore) -> None:
    a, b = store.save_photos([("IMG_1.png", _png_bytes()), ("IMG_1.png", _png_bytes())], [None, None])
    photos = store.resolve_photos([{"photo_id": a.photo_id}, {"photo_id": b.photo_id, "caption": "вторая"}])
    assert [p.name for p in photos] == ["IMG_1.png", "IMG_1-2.png"]
    assert photos[1].caption == "вторая"


# У шаблона VK Education есть раскладки с местом под фото; у ЛЦТ2026 из
# общей фикстуры без модели фото ложится на раскладку без такого места.
PHOTO_TEMPLATE = Path("dataset/templates/Шаблон презентации VK Education.pptx")


@pytest.fixture(scope="module")
def photo_template_id(client: TestClient) -> str:
    with PHOTO_TEMPLATE.open("rb") as fh:
        response = client.post("/api/templates", files={"file": (PHOTO_TEMPLATE.name, fh)})
    assert response.status_code == 200, response.text
    return response.json()["template_id"]


@pytest.fixture(scope="module")
def photo_job(client: TestClient, photo_template_id: str) -> dict:
    """Задание стиля visual с двумя фото из фикстуры контент-пакета.
    Распределение фото подменено: первое фото на слайд 1, второе без
    места. Подмена запоминает, что пришло в пайплайн."""
    seen: dict = {}

    def fake_assign(outline, photos, llm):
        seen["photos"] = list(photos)
        report = PhotoAssignmentReport(assigned={1: photos[0].name}, unused_photos=[photos[1].name])
        return {1: (photos[0].name, photos[0].caption)}, report

    files = [(p.name, p.read_bytes(), "image/jpeg") for p in sorted(PHOTOS_DIR.glob("*.jpg"))]
    uploaded = _upload(client, files).json()["photos"]
    brief, sources, meta = load_content_pack(CONTENT_PACK)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jobs, "assign_photos_to_outline", fake_assign)
        response = client.post("/api/decks", json={
            "template_id": photo_template_id, "brief": brief, "sources": [s.text for s in sources],
            "title": meta.get("title", "queue-latency"), "target_slides": 8, "style": "visual",
            "photos": [
                {"photo_id": uploaded[0]["photo_id"], "caption": "Подпись из задания"},
                {"photo_id": uploaded[1]["photo_id"]},
            ],
        })
        assert response.status_code == 200, response.text
        result = _poll_job(client, response.json()["job_id"])
    return {"result": result, "seen": seen}


def test_job_with_photos_puts_them_into_the_deck(photo_job: dict) -> None:
    result = photo_job["result"]
    assert result["status"] == "done", result
    seen = photo_job["seen"]["photos"]
    assert [p.caption for p in seen][0] == "Подпись из задания"
    assert all(Path(p.path).is_file() for p in seen)
    photos = result["photos"]
    assert photos["sent"] == 2 and photos["planned"] == 1
    assert photos["embedded"] == 1
    # Второе фото не легло: это видно в предупреждениях, а итог честный.
    assert result["outcome"] == "done_with_warnings"
    assert any("фотографий на слайдах 1 из 2" in w for w in result["warnings"])
    # И какое именно фото не легло, с причиной (задача T4).
    second = photo_job["seen"]["photos"][1].name
    assert any(w.startswith(f"фото {second!r} не на слайдах: ") for w in result["warnings"]), result["warnings"]



def test_too_many_photos_are_refused_before_their_bodies_are_read(client, monkeypatch):
    """Число файлов проверяется до чтения тел (ревью codex 27 сентября
    2026): сотня файлов по 10 МБ не должна лечь в память ради отказа."""
    from deckforge.api import app as app_module

    monkeypatch.setattr(app_module, "MAX_PHOTOS_PER_UPLOAD", 2)
    files = [("files", (f"p{i}.png", b"x" * 10, "image/png")) for i in range(3)]
    response = client.post("/api/photos", files=files)
    assert response.status_code == 400
