"""Общий на процесс планировщик вызовов модели (задача W).

До задачи W одновременность держал семафор писателя (`plan.writer.
_MODEL_SLOTS`, задача P), а аудит по картинке, починка и схема слотов шли
мимо него. Три задания одного пакета стилей делят одну квоту Yandex, и
живой прогон задачи Q показал, чем это кончается: письмо текста
растягивалось до 127-246 с, а один зависший вызов аудита держал стадию
140 с. Здесь один лимит на все роли и очередь с приоритетом: слот
получает то задание, у которого меньше времени до резерва этого вызова
(конец бюджета минус то, что должно остаться на стадии после него).

Приоритет считается в момент выдачи слота, а не в момент постановки в
очередь: остаток времени у заданий убывает с одной скоростью, но задание,
вставшее в очередь раньше, не должно обгонять задание, у которого часы
кончаются раньше. Слоты не делятся между заданиями жёстко: одно задание
забирает весь лимит, три делят его через очередь.

`ScheduledProvider` оборачивает провайдера роли: берёт слот, режет
дедлайн вызова по остатку бюджета задания минус резерв на стадии после
него и пишет в бюджет, сколько вызов шёл и сколько ждал очередь. Модуль не
знает про `workflow.budget`: бюджет для него любой объект с `remaining()`
и `record_call(...)`, чтобы `plan/` мог пользоваться планировщиком, не
завися от слоя выше.

Задача V4: слот выдаётся сначала по классу стадии, потом по времени до
резерва. Одного времени мало: аудит по картинке задания, которому до
резерва осталось 20с, обгонял писателя соседа со 100с, и сосед собирал
слайды запасным путём ради шага, без которого файл всё равно есть.
Необязательная стадия не должна стоить обязательной стадии другого
задания; внутри класса прежний порядок."""
from __future__ import annotations
import enum
import itertools
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

# Одновременных запросов к модели на процесс. 6 взято из задачи P: при
# трёх стилях по 4 потока (12 одновременно) Yandex отвечал 429 на восьми
# слайдах из двенадцати, при общем пределе 6 отказов 429 не было.
DEFAULT_LIMIT = 6

# Меньше этого вызов не начинается: ответ писателя или аудита по картинке
# за такое время не приходит, а слот и квота будут потрачены.
MIN_CALL_SECONDS = 5.0


class StagePriority(enum.IntEnum):
    """Класс стадии в очереди: меньше значит раньше."""

    STRUCTURE = 0  # P0: структура и текст, без них нет презентации
    REPAIR = 1  # P1: починка и сокращение текста под раскладку
    RERANK = 2  # P2: переранжирование раскладок моделью
    VISUAL_AUDIT = 3  # P3: аудит по картинке


# Роль вызова -> класс. Разбор шаблона (имена палитры, вид и схема
# раскладок) обязателен для задания так же, как структура: задание стоит
# и ждёт его, поэтому P0. Бюджета у разбора нет, и внутри P0 он всё равно
# идёт за заданиями с часами.
_ROLE_PRIORITY: dict[str, StagePriority] = {
    "outline": StagePriority.STRUCTURE,
    "writer": StagePriority.STRUCTURE,
    "palette_namer": StagePriority.STRUCTURE,
    "pattern_kind": StagePriority.STRUCTURE,
    "pattern_schema": StagePriority.STRUCTURE,
    "repair": StagePriority.REPAIR,
    "shorten": StagePriority.REPAIR,
    "rerank": StagePriority.RERANK,
    "visual_audit": StagePriority.VISUAL_AUDIT,
}


def stage_priority(role: str) -> StagePriority:
    """Класс стадии по роли вызова. Незнакомая роль считается обязательной:
    ошибка в таблице должна стоить соседям места в очереди, а не лишить
    задание текста."""
    return _ROLE_PRIORITY.get(role, StagePriority.STRUCTURE)


class OutOfTime(RuntimeError):
    """Вызов не начат: у задания не осталось времени сверх резерва на
    обязательные стадии. Писатель и аудит ловят его как обычный отказ
    модели и идут запасным путём."""


class CallBudget(Protocol):
    def remaining(self) -> float: ...

    def record_call(self, role: str, seconds: float, waited: float, *, ok: bool) -> None: ...

    def note_time_skip(self, what: str) -> None: ...

    def median_call_seconds(self, role: str | None, default: float) -> float: ...


@dataclass
class _Waiter:
    seq: int
    remaining: Callable[[], float]
    priority: int = StagePriority.STRUCTURE


class ModelScheduler:
    """Лимит одновременных вызовов с очередью по остатку бюджета."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self._cond = threading.Condition()
        self._busy = 0
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()
        self.peak = 0

    def _head(self) -> _Waiter | None:
        # Без бюджета (разбор шаблона, командная строка без бюджета)
        # вызов стоит за всеми заданиями с часами: их пять минут важнее.
        def key(w: _Waiter) -> tuple[int, float, int]:
            try:
                left = w.remaining()
            except Exception:  # noqa: BLE001: сломанный бюджет не должен вешать очередь
                left = math.inf
            return (int(w.priority), left, w.seq)

        return min(self._waiters, key=key) if self._waiters else None

    def acquire(
        self, remaining: Callable[[], float] | None = None, *, timeout: float | None = None,
        priority: int = StagePriority.STRUCTURE,
    ) -> float:
        """Занять слот; возвращает секунды ожидания. `timeout` истёк, а
        слот так и не дали: `OutOfTime`. `priority` из `StagePriority`:
        класс важнее остатка времени."""
        waiter = _Waiter(next(self._seq), remaining or (lambda: math.inf), int(priority))
        started = time.monotonic()
        give_up = None if timeout is None else started + max(timeout, 0.0)
        with self._cond:
            self._waiters.append(waiter)
            try:
                while not (self._busy < self.limit and self._head() is waiter):
                    wait = None if give_up is None else give_up - time.monotonic()
                    if wait is not None and wait <= 0:
                        raise OutOfTime(f"слот модели не освободился за {timeout:.0f}с")
                    self._cond.wait(wait)
            finally:
                self._waiters.remove(waiter)
                # Следующий в очереди мог стать головой, пока этот ждал.
                self._cond.notify_all()
            self._busy += 1
            self.peak = max(self.peak, self._busy)
        return time.monotonic() - started

    def release(self) -> None:
        with self._cond:
            self._busy = max(0, self._busy - 1)
            self._cond.notify_all()

    @property
    def busy(self) -> int:
        return self._busy


_DEFAULT: ModelScheduler | None = None
_DEFAULT_LOCK = threading.Lock()


def default_scheduler() -> ModelScheduler:
    """Планировщик процесса. Лимит из `config/app.yaml` (`llm.model_
    concurrency`), без читаемого конфига дефолт модуля."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            limit = DEFAULT_LIMIT
            try:
                from pathlib import Path

                from deckforge.settings import Settings

                app_yaml = Path(__file__).resolve().parents[3] / "config" / "app.yaml"
                limit = Settings.load(app_yaml).llm.model_concurrency
            except Exception:  # noqa: BLE001: без конфига работает дефолт
                pass
            _DEFAULT = ModelScheduler(limit)
        return _DEFAULT


class ScheduledProvider:
    """Провайдер роли, чьи вызовы идут через планировщик и в бюджете
    задания.

    `reserve`: сколько секунд задания должно остаться после этого вызова
    на обязательные стадии (у писателя сборка, аудит и экспорт; у аудита
    по картинке рендер и экспорт отчёта). Дедлайн вызова не больше
    `remaining() - reserve` и не больше `max_call_seconds`; если это
    меньше `MIN_CALL_SECONDS`, вызов не начинается (`OutOfTime`).

    `priority`: класс стадии в очереди; по умолчанию выводится из роли
    (`stage_priority`)."""

    def __init__(
        self, inner: Any, *, role: str, budget: CallBudget | None = None, reserve: float = 0.0,
        scheduler: ModelScheduler | None = None, max_call_seconds: float | None = None,
        priority: StagePriority | None = None,
    ) -> None:
        self.inner = inner
        self.role = role
        self.priority = priority if priority is not None else stage_priority(role)
        self.budget = budget
        self.reserve = reserve
        self.scheduler = scheduler if scheduler is not None else default_scheduler()
        self.max_call_seconds = max_call_seconds

    def __getattr__(self, name: str) -> Any:
        # `card`, `model_uri` и прочее, что код читает у провайдера.
        return getattr(self.inner, name)

    def _allowed(self) -> float:
        if self.budget is None:
            return math.inf
        return self.budget.remaining() - self.reserve

    def _skip(self, what: str, detail: str) -> OutOfTime:
        # В бюджет идёт короткая причина без чисел: одинаковые пропуски
        # складываются в счётчик, а не в десяток строк отчёта.
        if self.budget is not None:
            self.budget.note_time_skip(f"{self.role}: {what}")
        return OutOfTime(f"{what} ({detail})")

    def _call(self, method: str, args: tuple, kwargs: dict) -> Any:
        allowed = self._allowed()
        if allowed < MIN_CALL_SECONDS:
            raise self._skip(
                "вызов не начат, время до резерва вышло",
                f"резерв {self.reserve:.0f}с, сверх него осталось {max(allowed, 0.0):.0f}с",
            )
        # Приоритет по времени, оставшемуся ДО РЕЗЕРВА этого вызова, а не до
        # конца бюджета. Живой прогон задачи W: задания пакета стартуют
        # одновременно, и по голому остатку первое созданное задание
        # всегда шло первым, так что его необязательный аудит по картинке
        # (резерв 45с) отнимал слоты у письма соседа (резерв 60с), и тот
        # собрал 7 слайдов запасным вариантом.
        remaining = None if self.budget is None else (lambda: self.budget.remaining() - self.reserve)
        try:
            waited = self.scheduler.acquire(
                remaining, timeout=None if math.isinf(allowed) else allowed - MIN_CALL_SECONDS,
                priority=self.priority,
            )
        except OutOfTime as exc:
            raise self._skip("очередь к модели не дошла до вызова", str(exc)) from exc
        started = time.monotonic()
        ok = called = False
        try:
            allowed = self._allowed()
            if allowed < MIN_CALL_SECONDS:
                raise self._skip(
                    "время вышло, пока вызов ждал очередь",
                    f"ждал {waited:.0f}с, резерв {self.reserve:.0f}с",
                )
            deadline = allowed
            if self.max_call_seconds is not None:
                deadline = min(deadline, self.max_call_seconds)
            if not math.isinf(deadline) and getattr(self.inner, "accepts_call_deadline", False):
                kwargs = {**kwargs, "deadline_seconds": deadline}
            called = True
            result = getattr(self.inner, method)(*args, **kwargs)
            ok = True
            return result
        finally:
            self.scheduler.release()
            if called and self.budget is not None:
                self.budget.record_call(self.role, time.monotonic() - started, waited, ok=ok)

    def complete(self, messages, **kwargs) -> str:
        return self._call("complete", (messages,), kwargs)

    def ask_image(self, png: bytes, prompt: str, **kwargs) -> str:
        return self._call("ask_image", (png, prompt), kwargs)


def scheduled(llm: Any, *, role: str = "model") -> Any:
    """Провайдер через планировщик процесса: уже обёрнутый возвращается
    как есть, `None` остаётся `None`. Так писатель, вызванный из командной
    строки без бюджета, всё равно не превышает общий лимит."""
    if llm is None or isinstance(llm, ScheduledProvider):
        return llm
    return ScheduledProvider(llm, role=role)
