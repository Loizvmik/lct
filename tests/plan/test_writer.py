"""Тесты `plan.writer.write_slides`: текст пишется под контракт уже
выбранной раскладки (задача P). Модель везде фейковая, сети нет."""
from __future__ import annotations
from dataclasses import replace
import json
import threading
import time

import pytest

from deckforge.plan.contracts import plan_contracts
from deckforge.plan.outline import Outline, OutlineSlide
from deckforge.plan.spec import BulletBlock, Card, CardBlock, SlideSpec, validate_deck_spec
from deckforge.plan.writer import _drop_thin_duplicates, _flag_repeated_headlines, write_slides
from deckforge.provider.base import LLMProvider

import deckforge.plan.writer as writer_module


def _outline(n: int = 4) -> Outline:
    slides = [OutlineSlide(kind="title", intent="Тема")]
    kinds = ["problem", "how_it_works", "risks", "solution", "case"]
    for i in range(n - 2):
        slides.append(OutlineSlide(kind=kinds[i % len(kinds)], intent=f"Пункт {i}", needs=["а", "б", "в"]))
    slides.append(OutlineSlide(kind="closing", intent="Итог"))
    return Outline(slides=slides, title="Колода", language="ru")


def _contracts(profile, outline: Outline, style: str = "dense"):
    return plan_contracts(outline, profile, style)[1]


def _limit_words(spec: dict) -> int:
    return max(1, min(2, spec["max_words"]))


def _compliant_answer(contract: dict, headline: str = "Вывод") -> dict:
    """Ответ, который укладывается в контракт: ровно `count` единиц,
    каждая не длиннее предела."""
    blocks = []
    for block in contract["blocks"]:
        if not block["required"]:
            continue
        n, kind = block["count"], block["type"]
        if kind == "cards":
            body = " ".join(["шаг"] * _limit_words(block["body"]))
            blocks.append({"type": "cards", "items": [{"title": "Шаг", "body": body} for _ in range(n)]})
        elif kind == "bullets":
            blocks.append({"type": "bullets", "items": [" ".join(["пункт"] * _limit_words(block["item"]))] * n})
        elif kind == "kpi":
            blocks.append({"type": "kpi", "items": [{"value": "80%", "label": "доля"} for _ in range(n)]})
        elif kind == "quote":
            blocks.append({"type": "quote", "text": "цитата"})
        else:
            blocks.append({"type": "text", "text": " ".join(["текст"] * _limit_words(block["text"]))})
    return {"headline": headline, "blocks": blocks, "source_note": "Источник: тест"}


class _ContractLLM(LLMProvider):
    """Отвечает по контракту из запроса. `overrun`: номера слайдов, на
    которых первый ответ нарушает контракт (лишняя единица и длинный
    заголовок); `repair_ok`: исправляет ли модель на ремонте."""

    def __init__(self, *, overrun: set[int] = frozenset(), repair_ok: bool = True, delay=None, fail=frozenset()):
        self.overrun, self.repair_ok = set(overrun), repair_ok
        self.delay, self.fail = delay or (lambda i: 0.0), set(fail)
        self.requests: list[dict] = []
        self.schemas: list[dict] = []
        self.concurrent = self.max_concurrent = 0
        self._lock = threading.Lock()

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        payload = json.loads(messages[1]["content"])
        index = payload["position"]["index"]
        with self._lock:
            self.requests.append(payload)
            self.schemas.append(schema)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self.delay(index))
            if index in self.fail:
                raise RuntimeError(f"слайд {index}: модель недоступна (тест)")
            answer = _compliant_answer(payload["contract"], headline=f"Вывод {index}")
            repairing = "contract_problems" in payload
            if index in self.overrun and (not repairing or not self.repair_ok):
                answer["headline"] = " ".join(["очень"] * 30)
            answer["kind"] = "quote"  # модель не выбирает вид: код обязан его перезаписать
            return json.dumps(answer, ensure_ascii=False)
        finally:
            with self._lock:
                self.concurrent -= 1


def test_without_a_model_every_slide_is_a_valid_fallback_on_its_planned_layout(PROFILE):
    outline = _outline(4)
    contracts = _contracts(PROFILE, outline)
    deck = write_slides(outline, contracts, [], PROFILE, llm=None)

    assert len(deck.slides) == len(outline.slides)
    assert validate_deck_spec(deck) == []
    assert [s.pattern_id for s in deck.slides] == [c.pattern_id for c in contracts]
    assert all(any("запасным вариантом" in f for f in s.findings) for s in deck.slides)


def test_a_compliant_answer_costs_one_call_and_keeps_the_planned_kind_and_layout(PROFILE):
    outline = _outline(4)
    contracts = _contracts(PROFILE, outline)
    llm = _ContractLLM()

    deck = write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=1, style="dense")

    assert len(llm.requests) == len(contracts)
    for slide, contract in zip(deck.slides, contracts):
        assert slide.pattern_id == contract.pattern_id
        assert slide.kind == contract.kind
        assert not any("не уложился" in f for f in slide.findings)
    assert deck.meta["contract_places_ok"] == deck.meta["contract_places"]
    assert deck.meta["contract_repairs"] == "0"


def test_the_writer_sees_the_contract_not_a_layout_catalogue(PROFILE):
    outline = _outline(3)
    contracts = _contracts(PROFILE, outline)
    llm = _ContractLLM()

    write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=1)

    payload = next(r for r in llm.requests if r["position"]["index"] == 1)
    assert "available_kinds" not in payload and "capacity" not in payload
    contract = payload["contract"]
    assert {"headline", "blocks", "intent", "evidence"} <= set(contract)
    assert contract["headline"]["max_words"] >= contract["headline"]["target_words"]
    main = contract["blocks"][0]
    assert main["count"] >= 1 and "type" in main
    tools = json.dumps(llm.schemas[0], ensure_ascii=False)
    assert "list_layouts" not in tools and "try_slide" not in tools
    assert "measure_fit" in tools and "check_number" in tools


def test_a_contract_violation_gets_one_repair_call_that_is_accepted(PROFILE):
    outline = _outline(3)
    contracts = _contracts(PROFILE, outline)
    llm = _ContractLLM(overrun={1})

    deck = write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=1)

    calls = [r for r in llm.requests if r["position"]["index"] == 1]
    assert len(calls) == 2
    assert any("заголовок" in p for p in calls[1]["contract_problems"])
    assert calls[1]["previous_answer"]["headline"].startswith("очень")
    assert deck.slides[1].headline == "Вывод 1"
    assert deck.meta["contract_repairs"] == "1" and deck.meta["contract_repairs_accepted"] == "1"


def test_a_failed_repair_keeps_the_answer_and_names_the_violation(PROFILE):
    outline = _outline(3)
    contracts = _contracts(PROFILE, outline)
    llm = _ContractLLM(overrun={1}, repair_ok=False)

    deck = write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=1)

    assert len([r for r in llm.requests if r["position"]["index"] == 1]) == 2
    assert any("не уложился в контракт" in f for f in deck.slides[1].findings)
    assert int(deck.meta["contract_places_ok"]) < int(deck.meta["contract_places"])


def test_measure_fit_measures_in_the_planned_layout(PROFILE, monkeypatch):
    outline = _outline(3)
    contracts = _contracts(PROFILE, outline)
    seen: list[tuple] = []

    def _spy(text, role, profile, kind, *, layout_id=None):
        seen.append((kind, layout_id))
        return {"fits": True, "overflow_in": 0, "lines": 1, "fill": 0.8}

    monkeypatch.setattr(writer_module, "measure_fit", _spy)

    class _ToolThenAnswer(LLMProvider):
        def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
            payload = json.loads(messages[1]["content"])
            if len(messages) == 2:
                return json.dumps({"tool_calls": [{"tool": "measure_fit", "args": {"role": "headline", "text": "Вывод"}}]})
            return json.dumps(_compliant_answer(payload["contract"]), ensure_ascii=False)

    write_slides(outline, contracts, [], PROFILE, llm=_ToolThenAnswer(), max_workers=1)

    assert (contracts[1].kind, contracts[1].pattern_id) in seen


def test_airy_dividers_are_written_without_a_model(PROFILE):
    outline = _outline(5)
    contracts = _contracts(PROFILE, outline, "airy")
    dividers = [c for c in contracts if c.is_divider]
    if not dividers:
        pytest.skip("в шаблоне нет раскладки под разделитель")
    llm = _ContractLLM()

    deck = write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=1, style="airy")

    assert len(llm.requests) == len(contracts) - len(dividers)
    for slide, contract in zip(deck.slides, contracts):
        if contract.is_divider:
            assert slide.headline == contract.divider_label and not slide.blocks


def test_a_photo_planned_for_the_slide_reaches_its_visual(PROFILE):
    from deckforge.pattern.intent import intents_from_outline

    outline = _outline(3)
    intents = intents_from_outline(outline, {1: ("team.jpg", "Команда пилота")})
    contracts = plan_contracts(intents, PROFILE, "visual")[1]

    deck = write_slides(outline, contracts, [], PROFILE, llm=_ContractLLM(), max_workers=1)

    assert deck.slides[1].visual is not None and deck.slides[1].visual.photo_name == "team.jpg"


def test_slides_are_written_concurrently(PROFILE):
    outline = _outline(5)
    contracts = _contracts(PROFILE, outline)
    llm = _ContractLLM(delay=lambda i: 0.2)

    started = time.monotonic()
    write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=5)

    assert time.monotonic() - started < 0.6
    assert llm.max_concurrent > 1


def test_order_does_not_depend_on_who_answers_first(PROFILE):
    outline = _outline(5)
    contracts = _contracts(PROFILE, outline)
    llm = _ContractLLM(delay=lambda i: (4 - i) * 0.05)

    deck = write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=5)

    assert [s.headline for s in deck.slides] == [f"Вывод {i}" for i in range(5)]
    assert [s.index for s in deck.slides] == [0, 1, 2, 3, 4]


def test_one_failing_slide_does_not_bring_down_the_rest(PROFILE):
    outline = _outline(5)
    contracts = _contracts(PROFILE, outline)
    llm = _ContractLLM(delay=lambda i: 0.02, fail={2})

    deck = write_slides(outline, contracts, [], PROFILE, llm=llm, max_workers=5)

    assert len(deck.slides) == 5
    assert any("запасным вариантом" in f for f in deck.slides[2].findings)
    assert deck.slides[3].headline == "Вывод 3"
    assert validate_deck_spec(deck) == []


class _QueueLLM(LLMProvider):
    def __init__(self, responses: list):
        self._responses = list(responses)

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_fallback_finding_names_the_network_failure(PROFILE):
    outline = _outline(2)
    llm = _QueueLLM([RuntimeError("The read operation timed out")] * 4)

    deck = write_slides(outline, _contracts(PROFILE, outline), [], PROFILE, llm=llm, max_workers=1)

    finding = " ".join(deck.slides[0].findings)
    assert "The read operation timed out" in finding and "RuntimeError" in finding


def test_fallback_finding_names_the_schema_failure(PROFILE):
    outline = _outline(2)
    bad = json.dumps({"headline": "X", "unknown_field": 1}, ensure_ascii=False)
    llm = _QueueLLM([bad] * 4)

    deck = write_slides(outline, _contracts(PROFILE, outline), [], PROFILE, llm=llm, max_workers=1)

    finding = " ".join(deck.slides[0].findings)
    assert "unknown_field" in finding or "схем" in finding


def test_fallback_reason_does_not_leak_the_provider_key(PROFILE, monkeypatch):
    monkeypatch.setenv("YANDEX_API_KEY", "AQVN-секрет-не-для-экрана")
    outline = _outline(2)
    llm = _QueueLLM([RuntimeError("401 Unauthorized: Api-Key AQVN-секрет-не-для-экрана")] * 4)

    deck = write_slides(outline, _contracts(PROFILE, outline), [], PROFILE, llm=llm, max_workers=1)

    finding = " ".join(deck.slides[0].findings)
    assert "AQVN-секрет-не-для-экрана" not in finding and "***" in finding


def test_a_fallback_slide_says_what_data_it_needs(PROFILE):
    """Слайд с одним заголовком — брак по ТЗ; запасной показывает пункты
    плана в главном месте раскладки, а о запасном варианте говорят находка
    и заметка докладчика."""
    outline = _outline(3)
    llm = _QueueLLM([RuntimeError("сеть недоступна")] * 9)

    deck = write_slides(outline, _contracts(PROFILE, outline), [], PROFILE, llm=llm, max_workers=1)

    middle = deck.slides[1]
    assert middle.blocks
    assert any("Нужны данные" in f for f in middle.findings)
    assert middle.speaker_notes


def test_a_cover_slide_does_not_ask_for_data(PROFILE):
    outline = Outline(
        slides=[OutlineSlide(kind="title", intent="Тема доклада", needs=["Название инициативы"])],
        title="Колода", language="ru",
    )
    llm = _QueueLLM([RuntimeError("сеть недоступна")] * 4)

    deck = write_slides(outline, _contracts(PROFILE, outline), [], PROFILE, llm=llm, max_workers=1)

    assert not deck.slides[0].blocks


# ---------------------------------------------------------------------------
# _flag_repeated_headlines и _drop_thin_duplicates (без изменений задачи P)
# ---------------------------------------------------------------------------


def test_flag_repeated_headlines_catches_the_same_number_stated_differently():
    """Общая заметная цифра — сильный признак того же факта, даже когда
    значимые слова не совпадают падежом (ЛЦТ2026, слайды 0 и 3)."""
    slides = [
        SlideSpec(index=0, kind="bullets",
                  headline="98,5% ожидания устранимы: пилот подтвердил эффективность, готов план раскатки"),
        SlideSpec(index=1, kind="bullets", headline="Команда и бюджет"),
        SlideSpec(index=2, kind="bullets", headline="Обучение сотрудников"),
        SlideSpec(index=3, kind="bullets", headline="98,5% времени заявка находится в ожидании, а не обрабатывается."),
    ]
    _flag_repeated_headlines(slides)
    assert any("слайд 3" in f for f in slides[0].findings), slides[0].findings


def test_flag_repeated_headlines_does_not_fire_on_unrelated_headlines_with_incidental_numbers():
    slides = [
        SlideSpec(index=0, kind="bullets", headline="Команда — 2 человека на доработку правил закупок"),
        SlideSpec(index=1, kind="bullets", headline="Раскатка завершится в 2026 году"),
    ]
    _flag_repeated_headlines(slides)
    assert slides[0].findings == []


def _deck_with_repeated_fact() -> list[SlideSpec]:
    return [
        SlideSpec(index=0, kind="section", headline="Оптимизация согласования"),
        SlideSpec(index=1, kind="bullets", headline="98,5% времени заявка ждёт",
                  blocks=[BulletBlock(items=["31,5 ч на цикл", "28 мин работы", "ошибки маршрутизации"])]),
        SlideSpec(index=2, kind="bullets", headline="98,5% времени — ожидание",
                  blocks=[BulletBlock(items=["31,5 ч на цикл, 28 мин работы"])]),
        SlideSpec(index=3, kind="cards", headline="Риски раскатки",
                  blocks=[CardBlock(items=[Card(title="Данные", body="задержка"), Card(title="Регламенты", body="4 из 11")])]),
        SlideSpec(index=4, kind="bullets", headline="План раскатки",
                  blocks=[BulletBlock(items=["сентябрь", "декабрь"])]),
        SlideSpec(index=5, kind="section", headline="Согласовать бюджет"),
    ]


def test_thin_duplicate_of_a_fact_is_dropped_and_indexes_are_renumbered():
    slides = _drop_thin_duplicates(_deck_with_repeated_fact())

    assert [s.headline for s in slides][:3] == [
        "Оптимизация согласования", "98,5% времени заявка ждёт", "Риски раскатки",
    ]
    assert [s.index for s in slides] == list(range(len(slides)))


def test_two_full_slides_on_the_same_fact_are_both_kept():
    slides = _deck_with_repeated_fact()
    slides[2] = replace(slides[2], blocks=[BulletBlock(items=["а", "б", "в"])])

    assert len(_drop_thin_duplicates(slides)) == len(slides)


def test_cover_and_closing_slides_are_never_dropped():
    slides = _deck_with_repeated_fact()
    slides[0] = replace(slides[0], headline="98,5% времени заявка ждёт")
    slides[-1] = replace(slides[-1], headline="98,5% времени заявка ждёт")

    kept = _drop_thin_duplicates(slides)

    assert kept[0].kind == "section" and kept[-1].kind == "section"


def test_short_decks_are_left_alone():
    slides = _deck_with_repeated_fact()[:4]
    assert _drop_thin_duplicates(slides) is slides


def test_a_card_title_repeated_in_markdown_at_the_start_of_the_body_is_cut():
    from deckforge.plan.writer import _clean_card

    card = _clean_card(Card(title="Текущий процесс", body="**Текущий процесс** 98,5% времени заявки ждут."))
    assert card == Card(title="Текущий процесс", body="98,5% времени заявки ждут.")


def test_a_tool_call_on_the_final_step_gets_one_more_turn_not_a_fallback(PROFILE):
    """Живой прогон 27 сентября 2026: модель на последнем шаге снова позвала
    инструмент, и слайд ушёл в запасной вариант."""
    outline = _outline(3)
    contracts = _contracts(PROFILE, outline)

    class _Stubborn(LLMProvider):
        def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
            payload = json.loads(messages[1]["content"])
            if len(messages) <= 4:
                return json.dumps({"tool_calls": [{"tool": "check_number", "args": {"query": "1"}}]})
            return json.dumps(_compliant_answer(payload["contract"]), ensure_ascii=False)

    deck = write_slides(outline, contracts, [], PROFILE, llm=_Stubborn(), max_workers=1)

    assert not any("запасным вариантом" in f for s in deck.slides for f in s.findings)
