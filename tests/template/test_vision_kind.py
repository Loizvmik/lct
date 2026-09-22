"""Тесты `template.vision_kind.classify_patterns_by_vision` (Task 18):
уточнение `Pattern.kind` мультимодальной моделью по картинке слайда-примера
— модель предлагает, код проверяет; без ключа/при сбое рендера/сети/ответа
работает по-старому (геометрический `kind` не меняется)."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from deckforge.ooxml.geometry import Box
from deckforge.provider.base import VisionProvider
from deckforge.render.soffice import RenderError
from deckforge.template.patterns import Capacity, Pattern, PatternSlot
from deckforge.template.vision_kind import allowed_kind_ids, classify_patterns_by_vision, load_pattern_kinds


class _FakeVision(VisionProvider):
    """Отвечает заранее заготовленным JSON по очереди вызовов (по порядку
    submit в ThreadPoolExecutor) либо одним и тем же ответом на все вызовы
    — тот же приём, что `tests/plan/test_writer.py::_QueueLLM`, но проще:
    вызовы этого модуля независимы (каждый — свой паттерн), порядок ответов
    друг с другом не связан по смыслу."""

    def __init__(self, response: str | Exception | dict[str, str]):
        self._response = response

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        if isinstance(self._response, Exception):
            raise self._response
        if isinstance(self._response, dict):
            # Ключ по `geometric_hint`, встроенному в prompt, — позволяет
            # тестам с несколькими паттернами разных geometric_hint отвечать
            # по-разному на каждый вызов, не завязываясь на порядок потоков.
            payload = json.loads(prompt.split("Служебные данные:\n", 1)[1])
            hint = payload["geometric_hint"]
            return json.dumps({"kind": self._response.get(hint, hint)})
        return self._response

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:  # pragma: no cover
        raise NotImplementedError


def _slot() -> PatternSlot:
    return PatternSlot(
        role="headline", box=Box(left=0.1, top=0.1, width=0.5, height=0.1), size_pt=24.0,
        color_hex=None, align="l", max_chars=40, wraps=False,
    )


def _pattern(pattern_id: str, *, kind: str = "bullets", source_slide_index: list[int] = (1,)) -> Pattern:
    return Pattern(
        pattern_id=pattern_id, source_slide_index=list(source_slide_index), layout_id="slideLayout1",
        kind=kind, slots=[_slot()], repeat=None, decor=[],
        capacity=Capacity(max_items=1, max_chars_per_item=40, max_bullets=0, max_series=0, max_rows=0, max_cols=0),
        score=0.9, is_dark=False,
    )


def test_config_lists_at_least_ten_kinds_including_the_three_new_ones():
    kinds = load_pattern_kinds()
    ids = {k["id"] for k in kinds}
    assert len(ids) >= 10
    assert {"quote", "photo_text", "kpi_caption"} <= ids
    # Совпадает с геометрическим закрытым набором `patterns.KINDS` — новый
    # словарь РАСШИРЯЕТ его, не подменяет.
    from deckforge.template.patterns import KINDS as GEOMETRIC_KINDS
    assert set(GEOMETRIC_KINDS) <= ids


def test_without_llm_leaves_patterns_untouched():
    patterns = [_pattern("p1"), _pattern("p2")]
    result, notes = classify_patterns_by_vision(patterns, Path("dataset/templates/VK Tech шаблон.pptx"), None)
    assert result == patterns
    assert notes == []


def test_empty_pattern_list_is_a_no_op():
    result, notes = classify_patterns_by_vision([], Path("does-not-matter.pptx"), _FakeVision('{"kind": "quote"}'))
    assert result == []
    assert notes == []


def test_render_failure_keeps_geometric_kind(monkeypatch):
    import deckforge.template.vision_kind as vk

    def _boom(*args, **kwargs):
        raise RenderError("soffice не найден")

    monkeypatch.setattr(vk, "to_pngs", _boom)
    patterns = [_pattern("p1", kind="bullets")]
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _FakeVision('{"kind": "quote"}'))
    assert result == patterns
    assert any("рендер" in n for n in notes)


def test_model_proposes_a_valid_new_kind_and_it_overrides_the_geometric_one(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png_path = tmp_path / "slide-1.png"
    png_path.write_bytes(b"\x89PNG-fake")
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png_path])

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[1])]
    result, notes = classify_patterns_by_vision(
        patterns, Path("template.pptx"), _FakeVision('{"kind": "quote"}'),
    )
    assert result[0].kind == "quote"
    assert result[0].pattern_id == "p1"
    assert any("уточнён моделью у 1 из 1" in n for n in notes)


def test_model_proposes_a_kind_outside_the_closed_list_is_rejected(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png_path = tmp_path / "slide-1.png"
    png_path.write_bytes(b"\x89PNG-fake")
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png_path])

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[1])]
    result, notes = classify_patterns_by_vision(
        patterns, Path("template.pptx"), _FakeVision('{"kind": "gallery_carousel"}'),
    )
    assert result[0].kind == "bullets"
    assert any("которого нет в config/pattern-kinds.yaml" in n for n in notes)


def test_model_network_failure_keeps_geometric_kind_for_that_pattern_only(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(b"\x89PNG-fake-1")
    png2 = tmp_path / "slide-2.png"
    png2.write_bytes(b"\x89PNG-fake-2")
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1, png2])

    patterns = [
        _pattern("p1", kind="bullets", source_slide_index=[1]),
        _pattern("p2", kind="section", source_slide_index=[2]),
    ]
    result, notes = classify_patterns_by_vision(
        patterns, Path("template.pptx"), _FakeVision({"bullets": "quote", "section": RuntimeError("сеть недоступна")}),
    )
    by_id = {p.pattern_id: p for p in result}
    assert by_id["p1"].kind == "quote"
    assert by_id["p2"].kind == "section"  # сбой сети — не тронут
    assert any("вид моделью не получен" in n for n in notes)


def test_pattern_whose_source_slide_has_no_rendered_page_keeps_geometric_kind(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(b"\x89PNG-fake")
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1])  # только 1 страница

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[7])]  # источник за пределами рендера
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _FakeVision('{"kind": "quote"}'))
    assert result[0].kind == "bullets"
    assert any("не нашёлся среди отрисованных" in n for n in notes)


def test_allowed_kind_ids_matches_load_pattern_kinds():
    assert allowed_kind_ids() == {k["id"] for k in load_pattern_kinds()}
