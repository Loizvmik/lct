"""Тесты `template.vision_kind.classify_patterns_by_vision` — уточнение
`Pattern.kind` мультимодальной моделью по картинке слайда-примера. Модель
предлагает, код проверяет; без ключа/при сбое рендера/сети/ответа работает
по-старому (геометрический `kind` не меняется).

Задача "разбор незнакомого шаблона в бюджет" добавила три поведения поверх
Task 18: модель спрашивается только про паттерны с низкой уверенностью
геометрии (`kind_confidence < _ASK_CONFIDENCE_THRESHOLD`), несколько таких
паттернов уходят ОДНИМ вызовом (пачка, сетка-коллаж), число параллельных
пачек читается из `config/app.yaml` при `max_workers=None`."""
from __future__ import annotations
import json
from dataclasses import replace
from pathlib import Path

from PIL import Image

from deckforge.ooxml.geometry import Box
from deckforge.provider.base import VisionProvider
from deckforge.render.soffice import RenderError
from deckforge.template.patterns import Capacity, Pattern, PatternSlot
from deckforge.template.vision_kind import (
    _ASK_CONFIDENCE_THRESHOLD,
    _BATCH_SIZE,
    _build_grid_collage,
    _max_tokens_for_batch,
    allowed_kind_ids,
    classify_patterns_by_vision,
    load_pattern_kinds,
    validate_slot_schema,
)


class _FakeVision(VisionProvider):
    """Отвечает заранее заготовленным JSON по очереди вызовов (по порядку
    submit в ThreadPoolExecutor) либо одной и той же функцией ответа на
    каждый вызов пачки."""

    def __init__(self, respond):
        self._respond = respond

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        return self._respond(png, prompt, max_tokens)

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:  # pragma: no cover
        raise NotImplementedError


def _fixed(response) -> _FakeVision:
    """Одинаковый ответ (строка) или исключение на любой вызов."""
    def _respond(png, prompt, max_tokens):
        if isinstance(response, Exception):
            raise response
        return response
    return _FakeVision(_respond)


def _by_hint(mapping: dict[str, str | Exception]) -> _FakeVision:
    """Отвечает объектом JSON, где каждый ключ пачки ("1", "2", ...)
    сопоставлен ответу из `mapping`, найденному по `geometric_hint` ТОГО ЖЕ
    паттерна (порядок `items` в payload прочитан из промпта — не завязан на
    порядок потоков)."""
    def _respond(png, prompt, max_tokens):
        payload = json.loads(prompt.split("Служебные данные:\n", 1)[1])
        out = {}
        for item in payload["items"]:
            hint = item["geometric_hint"]
            value = mapping.get(hint, hint)
            if isinstance(value, Exception):
                raise value
            out[str(item["index"])] = {"kind": value}
        return json.dumps(out)
    return _FakeVision(_respond)


def _slot() -> PatternSlot:
    return PatternSlot(
        role="headline", box=Box(left=0.1, top=0.1, width=0.5, height=0.1), size_pt=24.0,
        color_hex=None, align="l", max_chars=40, wraps=False,
    )


def _pattern(
    pattern_id: str, *, kind: str = "bullets", source_slide_index: list[int] = (1,),
    kind_confidence: float | None = None,
) -> Pattern:
    """`kind_confidence` по умолчанию — низкая уверенность (сомнительный
    паттерн, попадает в пул "спрашиваем модель"), чтобы существующие тесты
    (написанные до находки №2 "спрашивать не про все раскладки") продолжали
    упражнять реальный сетевой путь, а не молча всегда пропускали его."""
    confidence = kind_confidence if kind_confidence is not None else (_ASK_CONFIDENCE_THRESHOLD - 0.1)
    return Pattern(
        pattern_id=pattern_id, source_slide_index=list(source_slide_index), layout_id="slideLayout1",
        kind=kind, slots=[_slot()], repeat=None, decor=[],
        capacity=Capacity(max_items=1, max_chars_per_item=40, max_bullets=0, max_series=0, max_rows=0, max_cols=0),
        score=0.9, is_dark=False, kind_confidence=confidence,
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
    result, notes = classify_patterns_by_vision([], Path("does-not-matter.pptx"), _fixed('{"1": {"kind": "quote"}}'))
    assert result == []
    assert notes == []


# --- находка №2: геометрически уверенные паттерны модель не видит ----------


def test_confident_patterns_are_never_asked_no_render_no_network(monkeypatch):
    """Паттерн с `kind_confidence >= порог` (структурно уверенная геометрия,
    например "cards") не должен рендериться и не должен звонить в сеть —
    ни то, ни другое не потребовалось."""
    import deckforge.template.vision_kind as vk

    def _boom_render(*args, **kwargs):
        raise AssertionError("to_pngs вызван для паттерна, который геометрия сняла уверенно")

    monkeypatch.setattr(vk, "to_pngs", _boom_render)

    def _boom_network(*args, **kwargs):
        raise AssertionError("ask_image вызван для паттерна, который геометрия сняла уверенно")

    patterns = [_pattern("p1", kind="cards", kind_confidence=1.0)]
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _FakeVision(_boom_network))
    assert result == patterns
    assert any("модель не спрашивалась ни разу" in n for n in notes)


def test_mixed_confidence_only_asks_about_the_uncertain_ones(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png_path1 = tmp_path / "slide-1.png"
    png_path1.write_bytes(_tiny_png())
    png_path2 = tmp_path / "slide-2.png"
    png_path2.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png_path1, png_path2])

    confident = _pattern("confident", kind="cards", kind_confidence=1.0, source_slide_index=[1])
    uncertain = _pattern("uncertain", kind="bullets", kind_confidence=0.3, source_slide_index=[2])
    result, notes = classify_patterns_by_vision(
        [confident, uncertain], Path("template.pptx"), _by_hint({"bullets": "quote"}),
    )
    by_id = {p.pattern_id: p for p in result}
    assert by_id["confident"].kind == "cards"  # не тронут — геометрия уверена
    assert by_id["uncertain"].kind == "quote"
    assert any("уверена" in n and "1" in n for n in notes)  # certain_count=1 упомянут в сводке


def _tiny_png() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


# --- честная деградация (рендер/сеть/формат) --------------------------------


def test_render_failure_keeps_geometric_kind(monkeypatch):
    import deckforge.template.vision_kind as vk

    def _boom(*args, **kwargs):
        raise RenderError("soffice не найден")

    monkeypatch.setattr(vk, "to_pngs", _boom)
    patterns = [_pattern("p1", kind="bullets")]
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _fixed('{"1": {"kind": "quote"}}'))
    assert result == patterns
    assert any("рендер" in n for n in notes)


def test_model_proposes_a_valid_new_kind_and_it_overrides_the_geometric_one(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png_path = tmp_path / "slide-1.png"
    png_path.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png_path])

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[1])]
    result, notes = classify_patterns_by_vision(
        patterns, Path("template.pptx"), _fixed('{"1": {"kind": "quote"}}'),
    )
    assert result[0].kind == "quote"
    assert result[0].pattern_id == "p1"
    assert any("уточнён вид у 1 из 1" in n for n in notes)


def test_model_proposes_a_kind_outside_the_closed_list_is_rejected(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png_path = tmp_path / "slide-1.png"
    png_path.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png_path])

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[1])]
    result, notes = classify_patterns_by_vision(
        patterns, Path("template.pptx"), _fixed('{"1": {"kind": "gallery_carousel"}}'),
    )
    assert result[0].kind == "bullets"
    assert any("которого нет в config/pattern-kinds.yaml" in n for n in notes)


def test_batch_network_failure_keeps_geometric_kind_for_the_whole_batch(tmp_path, monkeypatch):
    """Пачка — один вызов на несколько паттернов; сбой сети откатывает ВСЮ
    пачку к геометрическим видам, не только один паттерн (докстрока модуля
    про "блейст-радиус"). `_BATCH_SIZE` поднят monkeypatch'ем до 2 — иначе
    (продакшен-дефолт 1) два паттерна ушли бы двумя независимыми вызовами,
    и тест не проверял бы собственно блейст-радиус пачки."""
    import deckforge.template.vision_kind as vk

    monkeypatch.setattr(vk, "_BATCH_SIZE", 2)

    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(_tiny_png())
    png2 = tmp_path / "slide-2.png"
    png2.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1, png2])

    patterns = [
        _pattern("p1", kind="bullets", source_slide_index=[1]),
        _pattern("p2", kind="section", source_slide_index=[2]),
    ]
    result, notes = classify_patterns_by_vision(
        patterns, Path("template.pptx"), _fixed(RuntimeError("сеть недоступна")),
    )
    by_id = {p.pattern_id: p for p in result}
    assert by_id["p1"].kind == "bullets"
    assert by_id["p2"].kind == "section"
    assert any("вид моделью не получен" in n for n in notes)


def test_batch_recovers_on_a_second_attempt_after_the_first_fails(tmp_path, monkeypatch):
    """Живая находка обязательной проверки задачи (ЛЦТ2026): без ретрая
    пачка, чей первый заход ушёл в finish_reason=length/пустой ответ, теряла
    ВСЕ свои паттерны безвозвратно — второй ПОЛНЫЙ заход (не переиспользо-
    вание пустого ответа) часто отвечает содержательно там, где первый не
    ответил (тот же принцип, что `audit/visual.py::_MAX_MODEL_ATTEMPTS`)."""
    import deckforge.template.vision_kind as vk

    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1])

    attempts = {"n": 0}

    def _respond(png, prompt, max_tokens):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return ""  # первый заход — пустой ответ, не разбирается как JSON
        return '{"1": {"kind": "quote"}}'

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[1])]
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _FakeVision(_respond))

    assert attempts["n"] == 2
    assert result[0].kind == "quote"
    assert not any("вид моделью не получен" in n for n in notes)


def test_batch_gives_up_after_max_attempts_all_fail(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk
    from deckforge.template.vision_kind import _MAX_MODEL_ATTEMPTS

    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1])

    attempts = {"n": 0}

    def _respond(png, prompt, max_tokens):
        attempts["n"] += 1
        return ""  # каждый заход — пустой ответ

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[1])]
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _FakeVision(_respond))

    assert attempts["n"] == _MAX_MODEL_ATTEMPTS
    assert result[0].kind == "bullets"
    assert any("вид моделью не получен" in n for n in notes)


def test_pattern_whose_source_slide_has_no_rendered_page_keeps_geometric_kind(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1])  # только 1 страница

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[7])]  # источник за пределами рендера
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _fixed('{"1": {"kind": "quote"}}'))
    assert result[0].kind == "bullets"
    assert any("не нашёлся среди отрисованных" in n for n in notes)


def test_model_returns_a_non_object_json_is_rejected_for_the_whole_batch(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1])

    patterns = [_pattern("p1", kind="bullets", source_slide_index=[1])]
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _fixed("[1, 2, 3]"))
    assert result[0].kind == "bullets"
    assert any("ожидался объект JSON" in n for n in notes)


# --- находка №3: несколько паттернов уходят одним вызовом (пачка) ----------


def test_several_uncertain_patterns_are_batched_into_fewer_model_calls(tmp_path, monkeypatch):
    """`_BATCH_SIZE` в продакшене — 1 (см. её комментарий, "разбор в бюджет:
    вторая попытка" — пачки ≥2 наступают на потолок эскалации бюджета
    провайдера ненадёжно). Сам МЕХАНИЗМ группировки (`_ask_batch` уходит
    ОДНИМ вызовом на несколько паттернов, если `_BATCH_SIZE` больше 1) от
    этого не удалён и остаётся рабочим и протестированным — тест гонит его
    с явно повышенным `_BATCH_SIZE` (monkeypatch), не завязываясь на то,
    какое значение стоит в проде сейчас."""
    import deckforge.template.vision_kind as vk

    batch_size = 3
    monkeypatch.setattr(vk, "_BATCH_SIZE", batch_size)

    n = batch_size + 1  # заведомо больше одной пачки, но меньше двух полных
    pngs = []
    for i in range(1, n + 1):
        p = tmp_path / f"slide-{i}.png"
        p.write_bytes(_tiny_png())
        pngs.append(p)
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: pngs)

    calls = []

    def _respond(png, prompt, max_tokens):
        payload = json.loads(prompt.split("Служебные данные:\n", 1)[1])
        calls.append(len(payload["items"]))
        return json.dumps({str(item["index"]): {"kind": "quote"} for item in payload["items"]})

    patterns = [_pattern(f"p{i}", kind="bullets", source_slide_index=[i]) for i in range(1, n + 1)]
    result, notes = classify_patterns_by_vision(patterns, Path("template.pptx"), _FakeVision(_respond), max_workers=1)

    assert all(p.kind == "quote" for p in result)
    # n паттернов пачками до batch_size — минимум два вызова (одна пачка не
    # вместила бы все n > batch_size), и НИ ОДИН вызов не пачка размера 1
    # на весь список (иначе батчинг не работает вовсе).
    assert len(calls) >= 2
    assert sum(calls) == n
    assert max(calls) <= batch_size


def test_max_tokens_for_batch_grows_with_batch_size(tmp_path, monkeypatch):
    """Бюджет вызова — на ВСЮ пачку, не фиксированное число вне зависимости
    от того, сколько паттернов в неё легло (докстрока модуля про живой
    замер: 3072 на паттерн, умноженное на размер пачки). `_BATCH_SIZE`
    поднят monkeypatch'ем — иначе (продакшен-дефолт 1, см. её комментарий)
    два паттерна ушли бы ДВУМЯ вызовами по одному, а не одной пачкой из
    двух, и тест проверял бы не то, что заявлен."""
    import deckforge.template.vision_kind as vk

    monkeypatch.setattr(vk, "_BATCH_SIZE", 2)

    pngs = []
    for i in (1, 2):
        p = tmp_path / f"slide-{i}.png"
        p.write_bytes(_tiny_png())
        pngs.append(p)
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: pngs)

    seen_max_tokens = []

    def _respond(png, prompt, max_tokens):
        seen_max_tokens.append(max_tokens)
        payload = json.loads(prompt.split("Служебные данные:\n", 1)[1])
        return json.dumps({str(item["index"]): {"kind": "quote"} for item in payload["items"]})

    patterns = [_pattern("p1", source_slide_index=[1]), _pattern("p2", source_slide_index=[2])]
    classify_patterns_by_vision(patterns, Path("template.pptx"), _FakeVision(_respond))

    assert seen_max_tokens == [_max_tokens_for_batch(2)]
    assert _max_tokens_for_batch(2) > _max_tokens_for_batch(1)


# --- находка №4: число потоков — из config/app.yaml, не хардкод -----------


def test_max_workers_none_reads_config_when_available(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    monkeypatch.setattr(vk, "_default_max_workers", lambda: 7)
    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1])

    captured = {}
    real_executor = vk.ThreadPoolExecutor

    def _spy(max_workers):
        captured["max_workers"] = max_workers
        return real_executor(max_workers=max_workers)

    monkeypatch.setattr(vk, "ThreadPoolExecutor", _spy)

    patterns = [_pattern("p1", source_slide_index=[1])]
    classify_patterns_by_vision(patterns, Path("template.pptx"), _fixed('{"1": {"kind": "quote"}}'))
    assert captured["max_workers"] == 7


def test_explicit_max_workers_is_not_overridden_by_config(tmp_path, monkeypatch):
    import deckforge.template.vision_kind as vk

    monkeypatch.setattr(vk, "_default_max_workers", lambda: 99)
    png1 = tmp_path / "slide-1.png"
    png1.write_bytes(_tiny_png())
    monkeypatch.setattr(vk, "to_pngs", lambda pptx, out_dir, **kw: [png1])

    captured = {}
    real_executor = vk.ThreadPoolExecutor

    def _spy(max_workers):
        captured["max_workers"] = max_workers
        return real_executor(max_workers=max_workers)

    monkeypatch.setattr(vk, "ThreadPoolExecutor", _spy)

    patterns = [_pattern("p1", source_slide_index=[1])]
    classify_patterns_by_vision(
        patterns, Path("template.pptx"), _fixed('{"1": {"kind": "quote"}}'), max_workers=2,
    )
    assert captured["max_workers"] == 2


# --- сетка-коллаж ------------------------------------------------------------


def test_build_grid_collage_returns_a_decodable_png_sized_for_the_batch():
    pngs = [_tiny_png(), _tiny_png(), _tiny_png()]
    collage = _build_grid_collage(pngs)
    with Image.open(__import__("io").BytesIO(collage)) as im:
        im.load()
        assert im.format == "PNG"
        # 3 ячейки, сетка (не вертикальная простыня из одной колонки) — по
        # ширине заметно больше одной ячейки.
        assert im.width > 480


def test_allowed_kind_ids_matches_load_pattern_kinds():
    assert allowed_kind_ids() == {k["id"] for k in load_pattern_kinds()}


# --- схема слотов: уверенность и фильтр постоянного текста (задача J) ---------

def _schema_pattern(*samples: str) -> Pattern:
    """Разделитель как `slide10`/`slide11` VK Education: заголовок и подпись
    с текстом примера. `samples` — тексты примера по местам."""
    slots = [
        PatternSlot(
            role="headline" if i == 0 else "caption", box=Box(0.05, 0.1 + 0.2 * i, 0.4, 0.1), size_pt=14.0,
            color_hex=None, align="l", max_chars=120, wraps=True, sample_text=sample,
        )
        for i, sample in enumerate(samples)
    ]
    return Pattern(
        pattern_id="divider", source_slide_index=[10], layout_id="L", kind="section", slots=slots,
        repeat=None, decor=[],
        capacity=Capacity(max_items=1, max_chars_per_item=120, max_bullets=0, max_series=0, max_rows=0, max_cols=0),
        score=1.0, is_dark=False,
    )


def test_fixed_designer_hint_is_rejected_even_when_the_model_is_sure():
    """Скриншот 8.2: подпись «Точки используются для навигации» модель
    пометила постоянным текстом, и она осталась на разделителе."""
    pattern = _schema_pattern(
        "Спасибо \nза внимание!", "Точки используются для навигации. \nЧисло разделов = число точек.",
        "Вставьте фото", "Пример слайда-разделителя",
    )
    answer = {str(i): {"purpose": "текст", "fixed": True, "confidence": 0.95} for i in range(1, 5)}

    accepted, notes = validate_slot_schema(pattern, answer)

    assert accepted[0].get("fixed") is True
    assert not any(accepted[i].get("fixed") for i in (1, 2, 3))
    assert sum("fixed не принят" in n for n in notes) == 3


def test_low_confidence_drops_flags_first_and_description_below_half():
    pattern = _schema_pattern("Спасибо за внимание", "1", "2", "3")
    accepted, notes = validate_slot_schema(pattern, {
        "1": {"purpose": "финальная фраза", "max_words": 3, "fixed": True, "confidence": 0.7},
        "2": {"purpose": "номер", "ordinal": True, "confidence": 0.8},
        "3": {"purpose": "номер", "content_hint": "цифра", "max_words": 1, "ordinal": True, "confidence": 0.3},
        "4": {"purpose": "номер", "ordinal": True},  # без уверенности: схема места не принята
    })

    assert accepted[0] == {"purpose": "финальная фраза", "max_words": 3, "schema_confidence": 0.7}
    assert accepted[1] == {"purpose": "номер", "ordinal": True, "schema_confidence": 0.8}
    assert accepted[2] == {"schema_confidence": 0.3}
    assert 3 not in accepted
    assert any("fixed не принят" in n for n in notes)
    assert any("нет уверенности" in n for n in notes)


def test_keeps_sample_text_rechecks_fixed_from_an_old_cache():
    """Профиль, записанный до фильтра, несёт `fixed=True` на подсказке
    дизайнера: сборка всё равно не оставляет её на слайде."""
    pattern = _schema_pattern("Спасибо за внимание!", "Точки используются для навигации.")
    thanks, hint = (replace(s, fixed=True) for s in pattern.slots)

    assert thanks.keeps_sample_text
    assert not hint.keeps_sample_text
