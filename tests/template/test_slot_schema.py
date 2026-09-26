"""Схема слотов раскладки от модели (задача F): модель по превью слайда-
примера говорит, что за место перед ней и что туда писать, код проверяет
ответ, профиль его хранит, сборка и писатель им пользуются. Модель в тестах
не зовётся: вместо неё заглушка с заготовленным ответом."""
from __future__ import annotations
import json
from dataclasses import replace
from pathlib import Path

from deckforge.compose.blocks import assign_content
from deckforge.compose.builder import _pattern_from_model, _sample_text_slots
from deckforge.compose.slide_tools import list_layouts
from deckforge.ooxml.geometry import Box
from deckforge.plan.spec import Card, CardBlock, SlideSpec
from deckforge.provider.base import VisionProvider
from deckforge.template.grid import Grid
from deckforge.template.patterns import (
    CHARS_PER_WORD, Capacity, Pattern, PatternSlot, RepeatSpec, chars_per_item, slot_char_capacity,
)
from deckforge.template.profile import TemplateProfile, _apply_slot_schema, _pattern_model
from deckforge.template.vision_kind import describe_pattern_slots, slot_manifest, validate_slot_schema

TEMPLATES_DIR = Path("dataset/templates")


class _FakeVision(VisionProvider):
    def __init__(self, respond):
        self._respond = respond
        self.calls = 0

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        self.calls += 1
        return self._respond(prompt)


def _payload(prompt: str) -> dict:
    return json.loads(prompt.split("Служебные данные:\n", 1)[1])


def _slot(role: str, left: float, top: float, width: float, height: float, sample: str | None, **kw) -> PatternSlot:
    return PatternSlot(
        role=role, box=Box(left, top, width, height), size_pt=14.0, color_hex=None,
        align="l", max_chars=kw.pop("max_chars", 200), wraps=True, sample_text=sample, **kw,
    )


def _steps_pattern(**circle_kw) -> Pattern:
    """Три шага: кружок с номером над описанием (как `slide21` VK
    Education). Роль кружка — `card_title`: ровно та путаница, которую
    геометрия не разрешает."""
    slots = [_slot("headline", 0.05, 0.05, 0.9, 0.1, "Заголовок", max_chars=60)]
    for i in range(3):
        left = 0.05 + i * 0.3
        slots.append(_slot("card_title", left, 0.3, 0.05, 0.05, str(i + 1), max_chars=3, **circle_kw))
        slots.append(_slot("card_body", left, 0.4, 0.25, 0.3, "Описание шага", max_chars=180))
    return Pattern(
        pattern_id="steps", source_slide_index=[21], layout_id="L", kind="cards", slots=slots,
        repeat=RepeatSpec(axis="x", count=3, step=0.3, slot_roles=["card_title", "card_body"], group_size=2),
        decor=[],
        capacity=Capacity(max_items=3, max_chars_per_item=180, max_bullets=0, max_series=0, max_rows=0, max_cols=0),
        score=1.0, is_dark=False,
    )


def _grid() -> Grid:
    return Grid(
        margin_left=0.05, margin_right=0.05, margin_top=0.1, margin_bottom=0.1,
        columns=[], gutter=0.02, anchors={}, confidence={}, skipped_no_box=0, native_guides_used=False,
    )


# --- проверка ответа модели -------------------------------------------------

def test_manifest_numbers_slots_from_one_and_marks_repeat_units():
    manifest = slot_manifest(_steps_pattern())
    assert [item["index"] for item in manifest] == list(range(1, 8))
    assert manifest[0]["repeat_unit"] is None
    assert [item["repeat_unit"] for item in manifest[1:]] == [0, 0, 1, 1, 2, 2]
    assert manifest[1]["sample"] == "1"


def test_foreign_index_is_dropped_and_valid_fields_accepted():
    pattern = _steps_pattern()
    accepted, notes = validate_slot_schema(pattern, {
        "2": {"purpose": "номер шага", "content_hint": "не менять", "max_words": 1, "ordinal": True},
        "3": {"purpose": "описание шага", "content_hint": "одно предложение", "max_words": 20},
        "42": {"purpose": "выдумка"},
    })
    assert set(accepted) == {1, 2}
    assert accepted[1]["ordinal"] is True
    assert accepted[2] == {"purpose": "описание шага", "content_hint": "одно предложение", "max_words": 20}
    assert any("'42'" in n for n in notes)


def test_ordinal_on_a_long_sample_is_rejected():
    pattern = _steps_pattern()
    accepted, notes = validate_slot_schema(pattern, {
        "3": {"purpose": "описание", "ordinal": True},  # «Описание шага» — не номер
    })
    assert "ordinal" not in accepted[2]
    assert accepted[2]["purpose"] == "описание"
    assert any("ordinal не принят" in n for n in notes)


def test_bad_max_words_and_empty_fixed_are_ignored():
    pattern = _steps_pattern()
    accepted, _ = validate_slot_schema(pattern, {
        "1": {"purpose": "заголовок слайда", "max_words": 500, "fixed": True},
        "3": {"max_words": True},  # bool — не число слов
    })
    assert accepted[0] == {"purpose": "заголовок слайда", "fixed": True}
    assert 2 not in accepted


def test_without_model_nothing_is_described_and_nothing_breaks():
    assert describe_pattern_slots([_steps_pattern()], {"steps": b"png"}, None) == ({}, [])


def test_model_failure_leaves_pattern_without_schema():
    def _boom(prompt):
        raise RuntimeError("сеть")

    schema, notes = describe_pattern_slots([_steps_pattern()], {"steps": b"png"}, _FakeVision(_boom), max_workers=1)
    assert schema == {}
    assert notes[0].startswith("Схема слотов: модель ответила по 0 из 1")
    assert any("не получена" in n for n in notes[1:])


def test_model_answer_is_validated_per_pattern():
    def _respond(prompt):
        payload = _payload(prompt)
        assert payload["kind"] == "cards"
        return "```json\n" + json.dumps({"2": {"purpose": "номер шага", "ordinal": True}}) + "\n```"

    fake = _FakeVision(_respond)
    schema, notes = describe_pattern_slots([_steps_pattern()], {"steps": b"png"}, fake, max_workers=1)
    assert fake.calls == 1
    assert schema == {"steps": {1: {"purpose": "номер шага", "ordinal": True}}}
    assert "из них порядковых номеров: 1" in notes[0]


# --- вместимость и профиль ---------------------------------------------------

def test_max_words_caps_char_capacity():
    wide = _slot("card_body", 0.1, 0.1, 0.5, 0.5, "x", max_chars=300, max_words=5)
    assert slot_char_capacity(wide) == 5 * CHARS_PER_WORD
    ordinal = _slot("card_body", 0.1, 0.1, 0.5, 0.5, "1", max_chars=500, ordinal=True)
    assert chars_per_item([wide, ordinal]) == 5 * CHARS_PER_WORD


def test_schema_round_trips_through_profile_json_and_back_to_dataclass():
    model = _pattern_model(_steps_pattern())
    [described] = _apply_slot_schema([model], {"steps": {
        1: {"purpose": "номер шага", "ordinal": True},
        2: {"purpose": "описание шага", "content_hint": "одно предложение", "max_words": 10},
    }})
    assert described.capacity.max_chars_per_item == 180  # у двух других описаний своя вместимость
    again = type(described).model_validate_json(described.model_dump_json())
    assert again == described
    pattern = _pattern_from_model(again)
    assert pattern.slots[1].ordinal and pattern.slots[1].keeps_sample_text
    assert pattern.slots[2].max_words == 10 and pattern.slots[2].content_hint == "одно предложение"


def _fake_to_pngs(monkeypatch):
    def _fake(template_path, out_dir, dpi=110, pages=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for page in pages or []:
            dest = out_dir / f"slide-{page}.png"
            dest.write_bytes(f"PNG-{page}".encode())
            result.append(dest)
        return result

    monkeypatch.setattr("deckforge.template.profile.to_pngs", _fake)


def _describe_first_slot(prompt):
    return json.dumps({"1": {"purpose": "заголовок слайда", "content_hint": "вывод", "max_words": 8}})


def test_full_parse_with_schema_provider_describes_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "deckforge.template.profile.classify_patterns_by_vision", lambda p, path, llm, **kw: (list(p), []),
    )
    _fake_to_pngs(monkeypatch)
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"

    profile = TemplateProfile.from_file(path, cache_dir=tmp_path, schema=_FakeVision(_describe_first_slot))

    assert profile.pattern_schema_source == "model"
    assert all(p.slots[0].purpose == "заголовок слайда" for p in profile.patterns if p.slots)
    assert any(line.startswith("Схема слотов") for line in profile.provenance)


def test_cache_hit_without_schema_is_described_later(tmp_path, monkeypatch):
    path = TEMPLATES_DIR / "VK Tech шаблон.pptx"
    first = TemplateProfile.from_file(path, cache_dir=tmp_path)
    assert first.pattern_schema_source == "none"
    assert all(s.purpose is None for p in first.patterns for s in p.slots)

    _fake_to_pngs(monkeypatch)
    second = TemplateProfile.from_file(path, cache_dir=tmp_path, schema=_FakeVision(_describe_first_slot))
    assert second.pattern_schema_source == "model"
    assert [p.pattern_id for p in second.patterns] == [p.pattern_id for p in first.patterns]
    assert any(p.slots and p.slots[0].purpose == "заголовок слайда" for p in second.patterns)

    reread = TemplateProfile.model_validate_json(
        (tmp_path / f"{first.fingerprint}.json").read_text(encoding="utf-8")
    )
    assert reread == second


# --- применение в сборке и у писателя ---------------------------------------

def test_ordinal_circle_gets_no_card_title():
    """Кружок с номером, помеченный моделью, не получает заголовок карточки:
    заголовок уходит жирным абзацем в тело, номер остаётся из примера."""
    pattern = _steps_pattern(ordinal=True)
    spec = SlideSpec(index=1, kind="cards", headline="Как мы работаем", blocks=[CardBlock(items=[
        Card(title="71 заявка", body="Собрали заявки от школ."),
        Card(title="12 недель", body="Провели обучение."),
    ])])

    contents = assign_content(spec, pattern, _grid())

    assert not any(c.slot.ordinal for c in contents)
    bodies = [c for c in contents if c.role_hint == "card_body"]
    assert [p.text for p in bodies[0].paragraphs] == ["71 заявка", "Собрали заявки от школ."]
    assert bodies[0].paragraphs[0].bold


def test_without_schema_circle_still_receives_card_title():
    """Без модели поведение прежнее: схема ничего не отнимает у разбора без ключа."""
    pattern = _steps_pattern()
    spec = SlideSpec(index=1, kind="cards", headline="Х", blocks=[CardBlock(items=[Card(title="1", body="Тело")])])
    contents = assign_content(spec, pattern, _grid())
    assert any(c.role_hint == "card_title" for c in contents)


def test_clone_keeps_ordinal_of_filled_units_and_fixed_text_outside_repeat():
    pattern = _steps_pattern(ordinal=True)
    closing = _slot("headline", 0.1, 0.8, 0.8, 0.1, "Спасибо за внимание", fixed=True)
    pattern = replace(pattern, slots=[*pattern.slots, closing])

    kept = _sample_text_slots(pattern, filled={0, 1})

    assert [s.sample_text for s in kept] == ["1", "2", "Спасибо за внимание"]


def test_list_layouts_gives_writer_slot_descriptions(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "deckforge.template.profile.classify_patterns_by_vision", lambda p, path, llm, **kw: (list(p), []),
    )
    _fake_to_pngs(monkeypatch)
    profile = TemplateProfile.from_file(
        TEMPLATES_DIR / "VK Tech шаблон.pptx", cache_dir=tmp_path, schema=_FakeVision(_describe_first_slot),
    )

    rows = list_layouts(profile)

    described = [
        place for row in rows for place in row["places"]
        if place["purpose"] == "заголовок слайда" and place["content_hint"] == "вывод"
    ]
    assert described
    place = described[0]
    assert place["max_words"] == 8 and place["target_words"] == 6
    assert place["max_chars"] <= 8 * 7
    assert place["target_chars"] == round(place["max_chars"] * 0.8)
