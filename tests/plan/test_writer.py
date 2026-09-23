"""Тесты `plan.writer.write_slides`/`pick_patterns` (Task 13, Step 3 брифа)."""
from __future__ import annotations
import json
import threading
import time

from deckforge.plan.outline import Outline, OutlineSlide, SourceDoc
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec, validate_deck_spec
from deckforge.compose.slide_tools import list_layouts
from deckforge.plan.writer import _flag_repeated_headlines, pick_patterns, write_slides
from deckforge.provider.base import LLMProvider


class _QueueLLM(LLMProvider):
    """Отдаёт заранее заготовленные ответы по очереди (FIFO) — нужен, чтобы
    проверить путь "первый ответ невалиден -> код просит исправить -> второй
    ответ валиден", а не только happy path в один вызов."""

    def __init__(self, responses: list[str | Exception]):
        self._responses = list(responses)

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        if not self._responses:
            raise RuntimeError("_QueueLLM: закончились заготовленные ответы")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _valid_slide_json(headline: str, *, with_number: bool = False) -> str:
    payload = {
        "kind": "bullets",
        "headline": headline,
        "blocks": [{"type": "bullets", "items": ["Первый пункт", "Второй пункт"]}],
    }
    if with_number:
        payload["blocks"][0]["items"][0] = "Сквозная медиана — 6,2 часа"
        payload["source_note"] = "Источник: пилот, июнь—август 2026"
    return json.dumps(payload, ensure_ascii=False)


def _outline(n: int = 3) -> Outline:
    slides = [OutlineSlide(kind="title", intent="Тема")]
    for i in range(n - 2):
        slides.append(OutlineSlide(kind="problem", intent=f"Пункт {i}", needs=["число"]))
    slides.append(OutlineSlide(kind="closing", intent="Итог"))
    return Outline(slides=slides, title="Колода", language="ru")


def test_write_slides_without_llm_produces_valid_fallback_deck(PROFILE):
    outline = _outline(4)
    deck = write_slides(outline, [], PROFILE, llm=None)
    assert isinstance(deck, DeckSpec)
    assert len(deck.slides) == len(outline.slides)
    assert validate_deck_spec(deck) == []
    assert all(s.findings for s in deck.slides)  # запасной вариант честно помечен


def test_write_slides_uses_model_response_when_valid(PROFILE):
    outline = _outline(3)
    llm = _QueueLLM([_valid_slide_json("Заголовок один"), _valid_slide_json("Заголовок два"), _valid_slide_json("Заголовок три")])
    sources = [SourceDoc(name="sources.md", text="Сквозная медиана — 6,2 часа")]
    # max_workers=1: тест проверяет ЛОГИКУ (repair/fallback/happy path), не
    # параллельность — `_QueueLLM` отдаёт ответы строго по очереди (FIFO),
    # и с несколькими воркерами порядок обращения к очереди зависит от
    # планировщика потоков, а не от индекса слайда (реальную параллельность
    # и то, что порядок ИТОГОВОЙ колоды от неё не зависит, проверяют
    # отдельные тесты ниже, "параллельность write_slides").
    deck = write_slides(outline, sources, PROFILE, llm=llm, max_workers=1)
    assert [s.headline for s in deck.slides] == ["Заголовок один", "Заголовок два", "Заголовок три"]
    assert validate_deck_spec(deck) == []
    assert deck.title == "Колода"
    assert deck.language == "ru"


def test_write_slides_repairs_once_then_falls_back_to_valid_answer(PROFILE):
    outline = _outline(3)
    # Первый ответ на второй слайд несёт цифру без source_note (невалиден) —
    # код обязан попросить исправление один раз и принять второй, валидный
    # ответ, а не сразу деградировать до запасного варианта.
    bad = json.dumps({"kind": "bullets", "headline": "Заголовок", "blocks": [
        {"type": "bullets", "items": ["Медиана — 6,2 часа"]},
    ]}, ensure_ascii=False)  # цифра есть, source_note нет — невалидно
    good = _valid_slide_json("Заголовок исправлен", with_number=True)
    llm = _QueueLLM([_valid_slide_json("Первый"), bad, good, _valid_slide_json("Третий")])
    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1)  # см. комментарий выше — FIFO нужен последовательно
    assert deck.slides[1].headline == "Заголовок исправлен"
    assert validate_deck_spec(deck) == []


def test_write_slides_falls_back_when_model_keeps_sending_invalid_json(PROFILE):
    outline = _outline(3)
    # Оба ответа (первичный и после просьбы исправить) — с неизвестным полем:
    # код обязан деградировать до запасного варианта, а не пропустить слайд
    # или упасть.
    bad = json.dumps({"kind": "bullets", "headline": "X", "unknown_field": 1}, ensure_ascii=False)
    llm = _QueueLLM([_valid_slide_json("Первый"), bad, bad, _valid_slide_json("Третий")])
    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1)  # FIFO — см. комментарий выше
    assert validate_deck_spec(deck) == []
    assert deck.slides[1].findings  # запасной вариант помечен честно


def test_write_slides_falls_back_on_network_error(PROFILE):
    outline = _outline(2)
    llm = _QueueLLM([RuntimeError("сеть недоступна"), RuntimeError("сеть недоступна")])
    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1)  # FIFO — см. комментарий выше
    assert validate_deck_spec(deck) == []
    assert all(s.findings for s in deck.slides)


# ---------------------------------------------------------------------------
# Task 19: агентный цикл — модель пишет, при необходимости зовёт инструменты
# (measure_fit/check_number), видит результат и переписывает, бюджет два
# сетевых круга на слайд.
# ---------------------------------------------------------------------------


def _one_slide_outline() -> Outline:
    return Outline(slides=[OutlineSlide(kind="problem", intent="Слайд", needs=[])], title="Т", language="ru")


class _ToolThenFinalLLM(LLMProvider):
    """Первый ответ — вызов `measure_fit` инструмента с заведомо огромным
    текстом; второй (после результата инструмента) — короткий финальный
    слайд. Считает вызовы, чтобы тест мог проверить ТОЧНЫЙ бюджет сетевых
    кругов (бриф Task 19: "Цикл ограничен двумя шагами")."""

    def __init__(self):
        self.calls = 0
        self.seen_tool_results: list[dict] = []

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps({
                "tool_calls": [
                    {"tool": "measure_fit", "args": {"role": "headline", "text": "Очень длинный текст. " * 60}},
                ],
            }, ensure_ascii=False)
        # Второй шаг: код обязан был прислать результат инструмента отдельным
        # user-сообщением (см. `_write_with_agent_loop`) — запоминаем его,
        # чтобы тест ниже мог проверить, что модель РЕАЛЬНО увидела ответ
        # инструмента, а не просто была вызвана дважды подряд вслепую.
        self.seen_tool_results.append(json.loads(messages[-1]["content"]))
        return _valid_slide_json("Короткий заголовок")


def test_write_slides_agent_loop_calls_measure_fit_then_rewrites_shorter(PROFILE):
    llm = _ToolThenFinalLLM()
    deck = write_slides(_one_slide_outline(), [], PROFILE, llm=llm, max_workers=1)

    assert llm.calls == 2, "должно было хватить одного вызова инструмента и одного финального шага"
    assert deck.slides[0].headline == "Короткий заголовок"
    assert validate_deck_spec(deck) == []
    tool_results = llm.seen_tool_results[0]["tool_results"]
    assert tool_results[0]["tool"] == "measure_fit"
    assert tool_results[0]["result"]["fits"] is False, "инструмент обязан был честно сказать, что текст не влез"


class _StubbornToolCallingLLM(LLMProvider):
    """Каждый ответ — вызов инструмента, даже на последнем разрешённом
    шаге: код обязан остановиться на бюджете, а не звать модель без конца
    или бросить исключение."""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        self.calls += 1
        return json.dumps({"tool_calls": [{"tool": "check_number", "args": {"query": "1"}}]}, ensure_ascii=False)


def test_write_slides_agent_loop_stops_at_the_step_budget_and_falls_back(PROFILE):
    llm = _StubbornToolCallingLLM()
    deck = write_slides(_one_slide_outline(), [], PROFILE, llm=llm, max_workers=1, agent_max_steps=2)

    assert llm.calls == 2, "бюджет — ровно два сетевых круга, не больше и не меньше"
    assert deck.slides[0].findings, "не уложилась — обязан остаться запасной вариант с честной пометкой"
    assert validate_deck_spec(deck) == []


def test_write_slides_agent_loop_costs_one_call_when_the_model_is_confident_upfront(PROFILE):
    """Слайд, написанный уверенно с первого раза (без вызова инструмента),
    по-прежнему стоит ОДИН сетевой вызов — цикл не должен удорожать уже
    хороший случай (бриф Task 19, "агент с двумя шагами может удвоить
    [время]" — но только там, где инструмент реально понадобился)."""
    llm = _QueueLLM([_valid_slide_json("Заголовок")])
    deck = write_slides(_one_slide_outline(), [], PROFILE, llm=llm, max_workers=1)
    assert deck.slides[0].headline == "Заголовок"
    assert not llm._responses, "остался неиспользованный заготовленный ответ — было больше одного вызова"


def test_write_slides_agent_loop_respects_a_configured_step_budget_of_one(PROFILE):
    """`agent_max_steps=1` — тот же путь, что и до Task 19 (цикл выключен
    конфигом): даже если модель отвечает вызовом инструмента, первый шаг уже
    последний — код обязан потребовать финальный ответ сразу, не звать
    модель второй раз."""
    llm = _StubbornToolCallingLLM()
    deck = write_slides(_one_slide_outline(), [], PROFILE, llm=llm, max_workers=1, agent_max_steps=1)
    assert llm.calls == 1
    assert deck.slides[0].findings


# ---------------------------------------------------------------------------
# Параллельность write_slides (это задача: слайды пишутся по очереди — 23с
# на слайд, слайды друг от друга не зависят, писать нужно параллельно).
# ---------------------------------------------------------------------------


class _SlowIndexAwareLLM(LLMProvider):
    """Читает `position.index` из ЗАПРОСА (не угадывает по порядку вызова —
    у параллельных потоков порядок обращения непредсказуем) и отвечает
    валидным слайдом, чей заголовок несёт этот индекс. `delay_by_index`
    позволяет заставить более поздние слайды отвечать РАНЬШЕ более ранних —
    единственный честный способ проверить, что итоговый порядок колоды не
    зависит от того, кто ответил первым (не просто "порядок не менялся",
    что было бы правдой и без всякой сортировки по индексу)."""

    def __init__(self, delay_by_index=None, fail_indices: set[int] = frozenset()):
        self._delay_by_index = delay_by_index or (lambda i: 0.0)
        self._fail_indices = fail_indices
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        self._lock = threading.Lock()

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        payload = json.loads(messages[1]["content"])
        index = payload["position"]["index"]

        with self._lock:
            self.concurrent_calls += 1
            self.max_concurrent_calls = max(self.max_concurrent_calls, self.concurrent_calls)
        try:
            time.sleep(self._delay_by_index(index))
            if index in self._fail_indices:
                raise RuntimeError(f"слайд {index}: модель недоступна (тест)")
            # with_number=True: заголовок несёт цифру индекса ("Заголовок 0"
            # и т.п.) — без source_note такой слайд невалиден
            # (`slide_spec_problems`, "цифра без источника"), с ним — валиден.
            return _valid_slide_json(f"Заголовок {index}", with_number=True)
        finally:
            with self._lock:
                self.concurrent_calls -= 1


def test_write_slides_calls_the_model_concurrently_not_one_at_a_time(PROFILE):
    """Ловит именно параллельность, не просто факт вызова: пять слайдов,
    каждый вызов модели держит поток 0.2с. Последовательно это заняло бы
    ~1.0с; при реальном распараллеливании (max_workers=5) — ~0.2с. Порог
    0.6с — с большим запасом от 1.0с (последовательно) и от 0.2с
    (идеально параллельно), не хрупкий к дрожанию таймингов CI."""
    outline = _outline(5)
    llm = _SlowIndexAwareLLM(delay_by_index=lambda i: 0.2)

    started = time.monotonic()
    write_slides(outline, [], PROFILE, llm=llm, max_workers=5)
    elapsed = time.monotonic() - started

    assert elapsed < 0.6, f"write_slides заняло {elapsed:.2f}с — похоже, слайды пишутся по очереди, не параллельно"
    assert llm.max_concurrent_calls > 1, "ни разу не было больше одного активного вызова модели одновременно"


def test_write_slides_keeps_outline_order_even_when_later_slides_answer_first(PROFILE):
    """Порядок слайдов в готовой колоде не должен зависеть от того, кто
    ответил первым — слайд с БОЛЬШИМ индексом намеренно отвечает БЫСТРЕЕ
    (задержка обратно пропорциональна индексу), и всё равно должен оказаться
    на своём месте в конце `deck.slides`, а не в начале."""
    outline = _outline(5)
    llm = _SlowIndexAwareLLM(delay_by_index=lambda i: (4 - i) * 0.05)

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=5)

    assert [s.headline for s in deck.slides] == [f"Заголовок {i}" for i in range(5)]
    assert [s.index for s in deck.slides] == [0, 1, 2, 3, 4]


def test_write_slides_one_slide_failing_does_not_bring_down_the_rest_under_concurrency(PROFILE):
    """Отказ одного слайда не должен ронять всю колоду, даже когда остальные
    слайды пишутся параллельно с ним — сохраняется честная деградация
    (заголовок + пометка в findings) на месте, а не пропуск слайда или
    падение всей колоды."""
    outline = _outline(5)
    llm = _SlowIndexAwareLLM(delay_by_index=lambda i: 0.02, fail_indices={2})

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=5)

    assert len(deck.slides) == 5
    assert [s.index for s in deck.slides] == [0, 1, 2, 3, 4]
    for i, slide in enumerate(deck.slides):
        if i == 2:
            assert slide.findings, "неудачный слайд обязан остаться с честной пометкой, не пропасть"
        else:
            assert slide.headline == f"Заголовок {i}"
    assert validate_deck_spec(deck) == []


# ---------------------------------------------------------------------------
# _flag_repeated_headlines
# ---------------------------------------------------------------------------


def test_flag_repeated_headlines_catches_the_same_number_stated_differently():
    """Task 13, дефект отчёта задачи №3 (важное, ручная проверка ЛЦТ2026):
    в реально собранной колоде слайды 2 и 4 несли один и тот же факт
    другими словами — "98,5% ожидания устранимы: пилот подтвердил
    эффективность, готов план раскатки" и "98,5% времени заявка находится
    в ожидании, а не обрабатывается" — разный падеж ("ожидания"/
    "ожидании"), разный порядок слов, и буквального пересечения ЗНАЧИМЫХ
    слов нет вовсе (доля 0.0 — "ожидания" != "ожидании" посимвольно) — старый
    бэкстоп (только пересечение целых слов) эту пару пропускал. Индексы
    здесь НЕ соседние (0 и 3, между ними два других слайда) — бэкстоп
    обязан сравнивать ВСЮ колоду, не только соседей (бриф задачи).
    Общая цифра "98,5%" — сильный сигнал того же факта (бриф: "числа в
    заголовке — хороший признак, одна и та же цифра в двух заголовках
    почти всегда означает повтор")."""
    slides = [
        SlideSpec(
            index=0, kind="bullets",
            headline="98,5% ожидания устранимы: пилот подтвердил эффективность, готов план раскатки",
        ),
        SlideSpec(index=1, kind="bullets", headline="Команда и бюджет"),
        SlideSpec(index=2, kind="bullets", headline="Обучение сотрудников"),
        SlideSpec(
            index=3, kind="bullets",
            headline="98,5% времени заявка находится в ожидании, а не обрабатывается.",
        ),
    ]
    _flag_repeated_headlines(slides)
    assert any("слайд 3" in f for f in slides[0].findings), slides[0].findings


def test_flag_repeated_headlines_does_not_fire_on_unrelated_headlines_with_incidental_numbers():
    """Общая цифра — сильный, но не единственный сигнал: две головы,
    делящие короткое случайное число (например, год "2026" в обеих) без
    единого общего значимого слова, не обязаны считаться повтором — иначе
    находка станет бесполезным шумом на любой реальной колоде (обе несут
    "2026" в контексте сроков/дат)."""
    slides = [
        SlideSpec(index=0, kind="bullets", headline="Команда — 2 человека на доработку правил закупок"),
        SlideSpec(index=1, kind="bullets", headline="Раскатка завершится в 2026 году"),
    ]
    _flag_repeated_headlines(slides)
    assert slides[0].findings == []


# ---------------------------------------------------------------------------
# pick_patterns
# ---------------------------------------------------------------------------
# Task 18, находка №2 брифа: slide-writer должен видеть вместимость ВСЕХ
# видов раскладки шаблона, не только подсказанного `layout_kind` — иначе
# ему физически неоткуда узнать, что шаблон умеет цитату/крупный фактоид с
# подписью/фото с текстом, и он раз за разом пишет прозу под ту же
# подсказку.
# ---------------------------------------------------------------------------


class _RecordingLLM(LLMProvider):
    """Запоминает `messages` каждого вызова, ключом — `position.index` из
    payload (слайды пишутся параллельно, ThreadPoolExecutor — порядок
    вызовов не совпадает с порядком слайдов, см. `write_slides`)."""

    def __init__(self, response: str):
        self._response = response
        self.messages_by_index: dict[int, list] = {}

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        payload = json.loads(messages[1]["content"])
        self.messages_by_index[payload["position"]["index"]] = messages
        return self._response


def test_write_slides_shows_every_slide_the_capacity_of_all_kinds_the_template_has(PROFILE):
    outline = _outline(3)
    llm = _RecordingLLM(_valid_slide_json("Заголовок"))
    write_slides(outline, [], PROFILE, llm=llm, max_workers=1)
    assert len(llm.messages_by_index) == 3
    actual_kinds = {p.kind for p in PROFILE.patterns}
    for messages in llm.messages_by_index.values():
        payload = json.loads(messages[1]["content"])
        available = payload["available_kinds"]
        assert available, "available_kinds пуст, хотя PROFILE несёт паттерны"
        assert {a["kind"] for a in available} == actual_kinds
        # Подсказанный `layout_kind` тоже обязан присутствовать среди
        # `available_kinds` (он и есть один из видов шаблона) — иначе модель
        # видела бы противоречивые числа для одного и того же вида.
        hinted = next(a for a in available if a["kind"] == payload["layout_kind"])
        assert hinted["max_chars_per_item"] == payload["capacity"]["max_chars_per_item"]
        assert hinted["max_headline_chars"] == payload["max_headline_chars"]


def _cards_deck() -> DeckSpec:
    return DeckSpec(title="T", language="ru", slides=[
        SlideSpec(index=0, kind="cards", headline="Заголовок", blocks=[BulletBlock(items=["а", "б"])]),
    ])


def test_pick_patterns_without_llm_assigns_by_capacity(PROFILE):
    deck = pick_patterns(_cards_deck(), PROFILE, llm=None)
    assert deck.slides[0].pattern_id is not None
    assert deck.slides[0].pattern_id in {p.pattern_id for p in PROFILE.patterns if p.kind == "cards"}


def test_pick_patterns_uses_model_choice_when_valid(PROFILE):
    candidate_id = next(p.pattern_id for p in PROFILE.patterns if p.kind == "cards")
    llm = _QueueLLM([json.dumps({"pattern_id": candidate_id})])
    deck = pick_patterns(_cards_deck(), PROFILE, llm=llm)
    assert deck.slides[0].pattern_id == candidate_id


def test_pick_patterns_rejects_hallucinated_pattern_id(PROFILE):
    llm = _QueueLLM([json.dumps({"pattern_id": "not-a-real-pattern-id"})])
    deck = pick_patterns(_cards_deck(), PROFILE, llm=llm)
    assert deck.slides[0].pattern_id in {p.pattern_id for p in PROFILE.patterns if p.kind == "cards"}


def test_pick_patterns_keeps_slide_untouched_when_kind_has_no_candidates(PROFILE):
    deck = DeckSpec(title="T", language="ru", slides=[
        SlideSpec(index=0, kind="cards", headline="H", pattern_id=None),
    ])
    # Профиль без единого паттерна вовсе (пустой) — код не должен падать,
    # просто оставляет слайд как есть (pattern_id=None), решать нечем.
    class _EmptyPatterns:
        patterns: list = []

    result = pick_patterns(deck, _EmptyPatterns(), llm=None)
    assert result.slides[0].pattern_id is None


# ---------------------------------------------------------------------------
# Причина отказа модели: живой прогон 23 сентября 2026 — 4 слайда из 12 ушли
# в запасной вариант, и понять почему было нечем
# ---------------------------------------------------------------------------


def test_fallback_finding_names_the_network_failure(PROFILE):
    """Запасной слайд обязан сказать, ЧТО именно случилось, а не только «не
    вернула валидный ответ»: сеть отвалилась, кончился бюджет токенов и
    ответ не лёг в схему — три разные болезни с разным лечением."""
    outline = _outline(2)
    llm = _QueueLLM([RuntimeError("The read operation timed out")] * 4)

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1)

    finding = " ".join(deck.slides[0].findings)
    assert "The read operation timed out" in finding
    assert "RuntimeError" in finding


def test_fallback_finding_names_the_schema_failure(PROFILE):
    """Ответ пришёл, но не лёг в схему — причина обязана отличаться от
    сетевой, иначе по отчёту не понять, чинить сеть или промпт."""
    outline = _outline(2)
    bad = json.dumps({"kind": "bullets", "headline": "X", "unknown_field": 1}, ensure_ascii=False)
    llm = _QueueLLM([bad] * 4)

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1)

    finding = " ".join(deck.slides[0].findings)
    assert "unknown_field" in finding or "схем" in finding
    assert "The read operation timed out" not in finding


def test_fallback_reason_does_not_leak_the_provider_key(PROFILE, monkeypatch):
    """Причина едет в отчёт прогона и в веб-интерфейс. Ключ уходит в
    заголовок запроса, а не в текст исключения, но цена ошибки
    несимметрична — вырезаем."""
    monkeypatch.setenv("YANDEX_API_KEY", "AQVN-секрет-не-для-экрана")
    outline = _outline(2)
    llm = _QueueLLM([RuntimeError("401 Unauthorized: Api-Key AQVN-секрет-не-для-экрана")] * 4)

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1)

    finding = " ".join(deck.slides[0].findings)
    assert "AQVN-секрет-не-для-экрана" not in finding
    assert "***" in finding


# ---------------------------------------------------------------------------
# Task 23: агент видит последствия своего решения — каталог раскладок и
# черновая сборка слайда как инструменты цикла
# ---------------------------------------------------------------------------


def _tool_call(tool: str, args: dict) -> str:
    return json.dumps({"tool_calls": [{"tool": tool, "args": args}]}, ensure_ascii=False)


def test_agent_can_ask_what_layouts_the_template_has(PROFILE, TEMPLATE_PATH):
    outline = _outline(2)
    llm = _QueueLLM([
        _tool_call("list_layouts", {}),
        _valid_slide_json("После каталога"),
        _valid_slide_json("Второй"),
    ])

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1, template_path=TEMPLATE_PATH)

    assert deck.slides[0].headline == "После каталога"
    assert not deck.slides[0].findings, "слайд написан моделью, запасной вариант не нужен"


def test_agent_sees_the_verdict_of_a_draft_slide(PROFILE, TEMPLATE_PATH):
    """Главное обещание задачи: модель узнаёт, что вышло, ДО того как слайд
    попал в колоду."""
    outline = _outline(2)
    draft = json.loads(_valid_slide_json("Черновик"))
    layout_id = list_layouts(PROFILE)[0]["layout_id"]
    llm = _QueueLLM([
        _tool_call("try_slide", {"layout_id": layout_id, "slide": draft}),
        _valid_slide_json("Переписано после проверки"),
        _valid_slide_json("Второй"),
    ])

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1, template_path=TEMPLATE_PATH)

    assert deck.slides[0].headline == "Переписано после проверки"


def test_the_layout_the_agent_chose_reaches_the_slide(PROFILE, TEMPLATE_PATH):
    """Выбор раскладки агентом бесполезен, если теряется по дороге к
    сборке."""
    outline = _outline(2)
    layout_id = list_layouts(PROFILE)[0]["layout_id"]
    chosen = json.loads(_valid_slide_json("С выбранной раскладкой"))
    chosen["layout_id"] = layout_id
    llm = _QueueLLM([json.dumps(chosen, ensure_ascii=False), _valid_slide_json("Второй")])

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1, template_path=TEMPLATE_PATH)

    assert deck.slides[0].pattern_id == layout_id


def test_new_tools_are_unavailable_without_a_template_but_do_not_break_the_loop(PROFILE):
    """Старые вызовы `write_slides` пути шаблона не передают. Цикл обязан
    продолжиться на прежних двух инструментах, а не упасть."""
    outline = _outline(2)
    llm = _QueueLLM([
        _tool_call("try_slide", {"layout_id": "что-угодно", "slide": {}}),
        _valid_slide_json("Всё равно написано"),
        _valid_slide_json("Второй"),
    ])

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1)

    assert deck.slides[0].headline == "Всё равно написано"


def test_a_malformed_try_slide_call_is_answered_not_raised(PROFILE, TEMPLATE_PATH):
    """Модель вправе ошибиться в аргументах — ошибка возвращается ей
    текстом, тем же принципом, что и у остальных инструментов."""
    outline = _outline(2)
    llm = _QueueLLM([
        _tool_call("try_slide", {"slide": {"kind": "bullets"}}),  # забыт layout_id
        _valid_slide_json("После ошибки"),
        _valid_slide_json("Второй"),
    ])

    deck = write_slides(outline, [], PROFILE, llm=llm, max_workers=1, template_path=TEMPLATE_PATH)

    assert deck.slides[0].headline == "После ошибки"


