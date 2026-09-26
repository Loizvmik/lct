"""Планировщик вызовов модели на процесс (задача W): общий лимит
одновременных вызовов, очередь по остатку бюджета задания, дедлайн вызова
по остатку минус резерв. Модель везде фейковая, с искусственной задержкой."""
from __future__ import annotations
import threading
import time

import pytest

from deckforge.provider.scheduler import MIN_CALL_SECONDS, ModelScheduler, OutOfTime, ScheduledProvider
from deckforge.workflow.budget import BudgetPolicy, RunBudget


class _SlowModel:
    """Отвечает через `delay` секунд и считает одновременные вызовы."""

    accepts_call_deadline = True

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.deadlines: list[float | None] = []
        self.order: list[str] = []
        self._lock = threading.Lock()

    def complete(self, messages, *, deadline_seconds=None, **_kwargs) -> str:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.deadlines.append(deadline_seconds)
            self.order.append(messages[0]["content"])
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return "{}"


class _FixedBudget:
    """Бюджет с заданным остатком: приоритет в очереди без реальных часов."""

    def __init__(self, remaining: float) -> None:
        self._remaining = remaining
        self.calls: list[tuple[str, float, float, bool]] = []
        self.skips: list[str] = []

    def remaining(self) -> float:
        return self._remaining

    def record_call(self, role, seconds, waited, *, ok):
        self.calls.append((role, seconds, waited, ok))

    def note_time_skip(self, what):
        self.skips.append(what)

    def median_call_seconds(self, role, default):
        return default


def _ask(provider, text: str) -> None:
    provider.complete([{"role": "user", "content": text}])


def test_concurrent_calls_never_exceed_the_limit():
    """Три задания по шесть вызовов, лимит 3: одновременно не больше трёх,
    и все вызовы доходят до модели."""
    scheduler = ModelScheduler(3)
    model = _SlowModel(delay=0.05)
    providers = [
        ScheduledProvider(model, role="writer", budget=_FixedBudget(200.0 + i), scheduler=scheduler)
        for i in range(3)
    ]
    threads = [threading.Thread(target=_ask, args=(p, "x")) for p in providers for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(model.order) == 18
    assert model.max_active <= 3
    assert scheduler.peak == 3, "один лимит на всех, а не квота на задание"


def test_one_job_alone_takes_all_slots():
    """Одно задание без соседей получает весь лимит: слоты не делятся
    жёстко на число заданий."""
    scheduler = ModelScheduler(4)
    model = _SlowModel(delay=0.1)
    provider = ScheduledProvider(model, role="writer", budget=_FixedBudget(250.0), scheduler=scheduler)
    threads = [threading.Thread(target=_ask, args=(provider, "x")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert model.max_active == 4


def test_job_with_less_time_goes_first():
    """Слот занят; первым в очередь встало задание со 200с, вторым с 50с.
    Освободившийся слот получает то, у которого времени меньше, хоть оно и
    встало позже."""
    scheduler = ModelScheduler(1)
    model = _SlowModel(delay=0.01)
    scheduler.acquire()  # слот занят «чужим» вызовом
    roomy = ScheduledProvider(model, role="writer", budget=_FixedBudget(200.0), scheduler=scheduler)
    tight = ScheduledProvider(model, role="writer", budget=_FixedBudget(50.0), scheduler=scheduler)

    first = threading.Thread(target=_ask, args=(roomy, "roomy"))
    first.start()
    while len(scheduler._waiters) < 1:
        time.sleep(0.005)
    second = threading.Thread(target=_ask, args=(tight, "tight"))
    second.start()
    while len(scheduler._waiters) < 2:
        time.sleep(0.005)
    scheduler.release()
    first.join()
    second.join()

    assert model.order == ["tight", "roomy"]


def test_call_without_budget_waits_behind_jobs_with_budget():
    """Вызов без бюджета (разбор шаблона) не обгоняет задания с часами."""
    scheduler = ModelScheduler(1)
    model = _SlowModel(delay=0.01)
    scheduler.acquire()
    free = ScheduledProvider(model, role="pattern_kind", scheduler=scheduler)
    job = ScheduledProvider(model, role="writer", budget=_FixedBudget(280.0), scheduler=scheduler)

    first = threading.Thread(target=_ask, args=(free, "free"))
    first.start()
    while len(scheduler._waiters) < 1:
        time.sleep(0.005)
    second = threading.Thread(target=_ask, args=(job, "job"))
    second.start()
    while len(scheduler._waiters) < 2:
        time.sleep(0.005)
    scheduler.release()
    first.join()
    second.join()

    assert model.order == ["job", "free"]


def test_call_deadline_is_cut_to_remaining_minus_reserve():
    model = _SlowModel(delay=0.0)
    budget = _FixedBudget(100.0)
    provider = ScheduledProvider(model, role="writer", budget=budget, reserve=60.0, scheduler=ModelScheduler(2))

    _ask(provider, "x")

    assert model.deadlines == [pytest.approx(40.0)]
    assert budget.calls and budget.calls[0][0] == "writer" and budget.calls[0][3] is True


def test_call_is_not_started_without_time_over_the_reserve():
    """Остаток сверх резерва меньше минимального вызова: модель не зовётся,
    пропуск записан в бюджет."""
    model = _SlowModel(delay=0.0)
    budget = _FixedBudget(60.0 + MIN_CALL_SECONDS - 1)
    provider = ScheduledProvider(model, role="writer", budget=budget, reserve=60.0, scheduler=ModelScheduler(2))

    with pytest.raises(OutOfTime):
        _ask(provider, "x")

    assert model.order == []
    assert budget.skips and budget.skips[0].startswith("writer:")
    assert budget.calls == [], "не начатый вызов не считается вызовом"


def test_queue_wait_is_bounded_by_the_time_left():
    """Слот так и не освободился, а время до резерва вышло: вызов
    бросается, а не ждёт очередь дольше, чем у задания есть времени."""
    scheduler = ModelScheduler(1)
    scheduler.acquire()
    model = _SlowModel(delay=0.0)
    budget = _FixedBudget(MIN_CALL_SECONDS + 0.2)
    provider = ScheduledProvider(model, role="visual_audit", budget=budget, scheduler=scheduler)

    started = time.monotonic()
    with pytest.raises(OutOfTime):
        _ask(provider, "x")

    assert time.monotonic() - started < 2.0
    assert model.order == []
    scheduler.release()


def test_queue_wait_and_calls_land_in_the_run_budget():
    """Настоящий `RunBudget`: число вызовов, ожидание очереди и роли
    попадают в снимок задания."""
    scheduler = ModelScheduler(1)
    model = _SlowModel(delay=0.05)
    budget = RunBudget.from_policy(BudgetPolicy(budget_seconds=300.0))
    provider = ScheduledProvider(model, role="writer", budget=budget, scheduler=scheduler)
    threads = [threading.Thread(target=_ask, args=(provider, "x")) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = budget.summary()
    assert summary["model_calls"] == 3
    assert summary["queue_wait_seconds"] >= 0.05, "два из трёх вызовов ждали слот"
    assert summary["calls_by_role"]["writer"]["calls"] == 3
    assert budget.median_call_seconds("writer", 99.0) == pytest.approx(0.05, abs=0.04)
    assert budget.median_call_seconds("visual_audit", 25.0) == 25.0, "до первого вызова роли оценка из конфига"
