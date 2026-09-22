"""Тесты `plan.photos` (Task 20 — встраивание пользовательских фотографий):
чтение фотографий контент-пакета и распределение их по слайдам моделью.

Ровно тот же стиль, что `tests/plan/test_outline.py`/`test_writer.py`:
`_FakeLLM`/`_QueueLLM`, фикстура-контент-пакет из `fixtures/content-packs`,
проверка и "модель предложила валидно", и "модель предложила чушь — код
отверг", и "модели нет вовсе — деградация без падения"."""
from __future__ import annotations
import json
from pathlib import Path

from deckforge.plan.photos import (
    ContentPhoto, PhotoAssignmentReport, assign_photos, load_content_pack_photos,
)
from deckforge.plan.spec import DeckSpec, SlideSpec, TableVisual, Visual
from deckforge.provider.base import LLMProvider

CONTENT_PACK = Path("fixtures/content-packs/queue-latency")


class _FakeLLM(LLMProvider):
    def __init__(self, response: str | Exception):
        self._response = response
        self.calls = 0

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _deck(*slides: SlideSpec) -> DeckSpec:
    return DeckSpec(title="Тест", language="ru", slides=list(slides))


# ---------------------------------------------------------------------------
# load_content_pack_photos
# ---------------------------------------------------------------------------


def test_load_content_pack_photos_reads_files_and_sidecar_captions():
    photos = load_content_pack_photos(CONTENT_PACK)
    assert len(photos) == 2
    names = {p.name for p in photos}
    assert names == {"approver-laptop-review.jpg", "team-standup-tablet.jpg"}
    for p in photos:
        assert p.path.is_file()
        assert p.caption  # у обеих фотографий фикстуры есть подпись рядом


def test_load_content_pack_photos_returns_empty_list_without_photos_dir(tmp_path):
    assert load_content_pack_photos(tmp_path) == []


def test_load_content_pack_photos_ignores_non_image_files(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    (photos_dir / "note.txt").write_text("не фото", encoding="utf-8")
    (photos_dir / "photo.gif").write_bytes(b"gif89a")  # формат вне PNG/JPEG
    (photos_dir / "real.jpg").write_bytes(b"\xff\xd8\xff")
    photos = load_content_pack_photos(tmp_path)
    assert [p.name for p in photos] == ["real.jpg"]


def test_load_content_pack_photos_caption_is_none_without_sidecar(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    (photos_dir / "solo.png").write_bytes(b"\x89PNG")
    photos = load_content_pack_photos(tmp_path)
    assert photos[0].caption is None


# ---------------------------------------------------------------------------
# assign_photos — деградация без фото/без модели/без слайдов-кандидатов
# ---------------------------------------------------------------------------


def test_assign_photos_without_photos_is_a_no_op():
    deck = _deck(SlideSpec(index=0, kind="bullets", headline="Слайд"))
    new_deck, report = assign_photos(deck, [], llm=_FakeLLM("{}"))
    assert new_deck is deck
    assert report == PhotoAssignmentReport()


def test_assign_photos_without_llm_reports_photos_arrived_but_not_placed():
    deck = _deck(SlideSpec(index=0, kind="bullets", headline="Слайд"))
    photo = ContentPhoto(name="a.jpg", path=Path("a.jpg"))
    new_deck, report = assign_photos(deck, [photo], llm=None)
    assert new_deck.slides[0].visual is None
    assert report.placed_count == 0
    assert report.unused_photos == ["a.jpg"]
    assert report.notes  # молчаливая тишина запрещена — причина названа


def test_assign_photos_without_candidate_slides_reports_honestly():
    """Все слайды заняли свой единственный визуальный слот таблицей —
    фотографии физически некуда ставить, отчёт обязан сказать об этом
    (бриф задачи, п.4), а не тихо вернуть пустой список размещений."""
    deck = _deck(
        SlideSpec(
            index=0, kind="table", headline="Таблица",
            visual=Visual(kind="table", table=TableVisual(rows=[["A"], ["1"]])),
        ),
    )
    photo = ContentPhoto(name="a.jpg", path=Path("a.jpg"))
    new_deck, report = assign_photos(deck, [photo], llm=_FakeLLM("{}"))
    assert report.placed_count == 0
    assert report.unused_photos == ["a.jpg"]
    assert report.notes


# ---------------------------------------------------------------------------
# assign_photos — модель предлагает валидно
# ---------------------------------------------------------------------------


def test_assign_photos_places_photo_on_model_chosen_slide():
    deck = _deck(
        SlideSpec(index=0, kind="section", headline="Титул"),
        SlideSpec(index=1, kind="bullets", headline="Команда на стендапе"),
    )
    photo = ContentPhoto(name="team.jpg", path=Path("team.jpg"), caption="Команда у доски")
    llm = _FakeLLM(json.dumps({"assignments": [{"photo": "team.jpg", "slide_index": 1}]}))

    new_deck, report = assign_photos(deck, [photo], llm=llm)

    assert llm.calls == 1  # один вызов на всю колоду, не на слайд
    assert report.assigned == {1: "team.jpg"}
    assert report.unused_photos == []
    slide = new_deck.slides[1]
    assert slide.visual is not None
    assert slide.visual.kind == "photo"
    assert slide.visual.photo_name == "team.jpg"
    assert slide.visual.caption == "Команда у доски"
    # слайд 0 не тронут
    assert new_deck.slides[0].visual is None


def test_assign_photos_keeps_slide_writer_caption_over_photo_caption():
    """Если слайд уже нёс свою (написанную slide-writer) подпись визуала —
    она остаётся: подпись фотографии — только запасной вариант, когда у
    слайда подписи не было вовсе (см. докстроку `assign_photos`)."""
    deck = _deck(
        SlideSpec(
            index=0, kind="photo_text", headline="Демо",
            visual=Visual(kind="photo", caption="Скриншот интерфейса"),
        ),
    )
    photo = ContentPhoto(name="p.jpg", path=Path("p.jpg"), caption="Подпись фото")
    llm = _FakeLLM(json.dumps({"assignments": [{"photo": "p.jpg", "slide_index": 0}]}))

    new_deck, report = assign_photos(deck, [photo], llm=llm)

    assert new_deck.slides[0].visual.caption == "Скриншот интерфейса"
    assert new_deck.slides[0].visual.photo_name == "p.jpg"
    assert report.placed_count == 1


def test_assign_photos_reports_unused_photo_the_model_did_not_place():
    deck = _deck(SlideSpec(index=0, kind="bullets", headline="Слайд"))
    photos = [
        ContentPhoto(name="used.jpg", path=Path("used.jpg")),
        ContentPhoto(name="unused.jpg", path=Path("unused.jpg")),
    ]
    llm = _FakeLLM(json.dumps({"assignments": [{"photo": "used.jpg", "slide_index": 0}]}))
    _new_deck, report = assign_photos(deck, photos, llm=llm)
    assert report.assigned == {0: "used.jpg"}
    assert report.unused_photos == ["unused.jpg"]


# ---------------------------------------------------------------------------
# assign_photos — код проверяет ответ модели, не доверяет слепо
# ---------------------------------------------------------------------------


def test_assign_photos_rejects_photo_name_outside_the_supplied_list():
    deck = _deck(SlideSpec(index=0, kind="bullets", headline="Слайд"))
    photo = ContentPhoto(name="real.jpg", path=Path("real.jpg"))
    llm = _FakeLLM(json.dumps({"assignments": [{"photo": "выдуманное.jpg", "slide_index": 0}]}))
    new_deck, report = assign_photos(deck, [photo], llm=llm)
    assert report.assigned == {}
    assert new_deck.slides[0].visual is None
    assert any("вне присланного списка" in n for n in report.notes)


def test_assign_photos_rejects_slide_index_outside_candidates():
    deck = _deck(
        SlideSpec(
            index=0, kind="table", headline="Таблица",
            visual=Visual(kind="table", table=TableVisual(rows=[["A"], ["1"]])),
        ),
        SlideSpec(index=1, kind="bullets", headline="Обычный слайд"),
    )
    photo = ContentPhoto(name="p.jpg", path=Path("p.jpg"))
    # Модель целит в слайд 0 — он занят таблицей, не кандидат.
    llm = _FakeLLM(json.dumps({"assignments": [{"photo": "p.jpg", "slide_index": 0}]}))
    new_deck, report = assign_photos(deck, [photo], llm=llm)
    assert report.assigned == {}
    assert new_deck.slides[1].visual is None


def test_assign_photos_rejects_same_photo_on_two_slides():
    deck = _deck(
        SlideSpec(index=0, kind="bullets", headline="Первый"),
        SlideSpec(index=1, kind="bullets", headline="Второй"),
    )
    photo = ContentPhoto(name="p.jpg", path=Path("p.jpg"))
    llm = _FakeLLM(json.dumps({
        "assignments": [
            {"photo": "p.jpg", "slide_index": 0},
            {"photo": "p.jpg", "slide_index": 1},
        ],
    }))
    new_deck, report = assign_photos(deck, [photo], llm=llm)
    assert report.assigned == {0: "p.jpg"}  # первое предложение победило
    assert new_deck.slides[1].visual is None


def test_assign_photos_rejects_two_photos_on_the_same_slide():
    deck = _deck(SlideSpec(index=0, kind="bullets", headline="Слайд"))
    photos = [ContentPhoto(name="a.jpg", path=Path("a.jpg")), ContentPhoto(name="b.jpg", path=Path("b.jpg"))]
    llm = _FakeLLM(json.dumps({
        "assignments": [
            {"photo": "a.jpg", "slide_index": 0},
            {"photo": "b.jpg", "slide_index": 0},
        ],
    }))
    new_deck, report = assign_photos(deck, photos, llm=llm)
    assert report.assigned == {0: "a.jpg"}
    assert new_deck.slides[0].visual.photo_name == "a.jpg"


def test_assign_photos_survives_malformed_model_response():
    deck = _deck(SlideSpec(index=0, kind="bullets", headline="Слайд"))
    photo = ContentPhoto(name="a.jpg", path=Path("a.jpg"))
    llm = _FakeLLM("это не json вовсе")
    new_deck, report = assign_photos(deck, [photo], llm=llm)
    assert new_deck.slides[0].visual is None
    assert report.placed_count == 0
    assert report.notes


def test_assign_photos_survives_llm_raising():
    deck = _deck(SlideSpec(index=0, kind="bullets", headline="Слайд"))
    photo = ContentPhoto(name="a.jpg", path=Path("a.jpg"))
    llm = _FakeLLM(RuntimeError("сеть недоступна"))
    new_deck, report = assign_photos(deck, [photo], llm=llm)
    assert new_deck.slides[0].visual is None
    assert report.placed_count == 0
    assert report.notes
