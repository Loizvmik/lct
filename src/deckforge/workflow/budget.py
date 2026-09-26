"""Бюджет времени одного прогона генерации (ТЗ: пять минут на презентацию).

До задачи L деградация решалась «на лету»: каждый вызов (`_rerank`,
`run_visual_stage`) сам спрашивал бюджет `can_run(stage)`/`check(stage)` в
момент своего запуска, с порогом этой ОДНОЙ стадии. Два вызова в одном
прогоне могли увидеть разный остаток времени и решить по-разному, хотя
человеку, читающему отчёт, нужен один ответ на вопрос «в каком режиме
прошла эта генерация» — не россыпь независимых «да/нет» по стадиям.

Задача L вводит режимы FULL/FAST/EMERGENCY (`RunMode`): каждый режим — это
целиком таблица «что можно» (rerank раскладок, до скольки слайдов идёт
аудит по картинке), а не отдельный порог на стадию. Режим выбирается на
КОНТРОЛЬНЫХ ТОЧКАХ пайплайна (`decide_mode(checkpoint)`, вызывающий код
передаёт `"after_write"`/`"after_compose"`) по остатку времени НА ЭТОЙ
точке и держится до следующего вызова `decide_mode` — стадии между точками
читают уже принятое решение (`mode_spec()`), не пересчитывают его сами.
Это и даёт воспроизводимость: одинаковые входы при нормальной задержке
проходят обе точки с большим остатком времени и получают FULL оба раза
(тест `test_same_inputs_give_full_mode_at_both_checkpoints`), а не
скатываются в разные режимы от случайного дрожания времени между двумя
близкими вызовами.

Обязательные стадии (разбор, структура, текст, сборка, детерминированный
аудит, экспорт) идут всегда: без них нет файла, а файл важнее уложиться в
срок — режимы на них не влияют.

Задача Q: одна задача генерации = одна презентация = один стиль = один
`RunBudget` на 300 с. Три стиля идут тремя независимыми заданиями, у
каждого свои часы, свои контрольные точки и свой режим, поэтому медленный
стиль не переводит соседей в режим попроще. Бюджеты «на вариант внутри
прогона» (`for_variant`) больше не нужны и убраны. Контрольных точек три:
`after_outline`, `after_write`, `after_compose`. Стадию, которую задание
не считало само, а получило готовой (разбор шаблона, общая структура
пакета из трёх стилей), отмечает `mark_reused`: её секунды остаются в
бюджете задания (задание их честно ждало), а снимок помечает её в
`shared_stages` как «переиспользовано»."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class RunMode(str, Enum):
    """Три режима прогона (задача L, `config/app.yaml` — `run.modes`).

    FULL — хватает времени на всё необязательное: rerank раскладок моделью
    для airy/visual и аудит по картинке до `visual_audit_max_slides`
    рискованных слайдов.
    FAST — времени в обрез: rerank раскладок не идёт (см. докстроку
    `ModeSpec.rerank` про то, почему это не «частичный rerank», а именно
    пропуск), аудит по картинке — по меньшему числу слайдов.
    EMERGENCY — ни rerank, ни аудит по картинке: только обязательные
    стадии, лишь бы получить файл."""

    FULL = "full"
    FAST = "fast"
    EMERGENCY = "emergency"


@dataclass(frozen=True)
class ModeSpec:
    """Одна строка таблицы `run.modes`: с какого остатка времени режим
    вообще годится (`min_remaining`) и что он разрешает необязательным
    стадиям."""

    min_remaining: float
    # Идёт ли rerank раскладок моделью для airy/visual (`plan.writer.
    # rerank_patterns`) в этом режиме. Бриф задачи L для FAST просит более
    # тонкое правило («только слайды с двумя и более кандидатами одного
    # ранга»), но `plan.writer.rank_patterns` УЖЕ отдаёт на rerank только
    # кандидатов, равных лидеру по вместимости (см. её докстроку) — вне
    # `plan/` (эту задачу прямо просили его не трогать) нет способа отличить
    # «равны по вместимости» от «совпадают ещё и по вкусовому ранжиру»,
    # не заглядывая в приватный ключ сортировки. Бриф сам даёт это как
    # равноценную альтернативу («или пропуск») — FAST этим и пользуется:
    # честный пропуск дешевле почти обязательно бесполезного различения.
    rerank: bool
    visual_audit_max_slides: int


# Порядок проверки на контрольной точке: от щедрого к отчаянному — первый
# режим, чей `min_remaining` не больше остатка времени, и есть ответ.
_MODE_ORDER: tuple[RunMode, ...] = (RunMode.FULL, RunMode.FAST, RunMode.EMERGENCY)

# Пометка стадии, полученной готовой от другого задания (`mark_reused`).
REUSED_NOTE = "переиспользовано"

# Дефолты дублируют `config/app.yaml` (`run.modes`), чтобы бюджет работал и
# там, где конфиг не читается (тесты на чужом дереве, воркер без
# смонтированного `config/`), тем же приёмом, что и остальные настройки
# `Settings`. Числа — те же 120/75, что раньше были порогами `rerank_min_
# remaining`/`visual_audit_min_remaining` (см. их обоснование в старом
# конфиге): FULL нужен остаток, которого хватало на весь rerank-шаг
# (~40с) плюс запас на сборку/аудит/экспорт после; FAST — остаток одного
# вызова аудита по картинке с запасом. Максимумы аудита (4/2/0) — из брифа
# задачи L дословно.
DEFAULT_MODES: dict[RunMode, ModeSpec] = {
    RunMode.FULL: ModeSpec(min_remaining=120.0, rerank=True, visual_audit_max_slides=4),
    RunMode.FAST: ModeSpec(min_remaining=75.0, rerank=False, visual_audit_max_slides=2),
    RunMode.EMERGENCY: ModeSpec(min_remaining=0.0, rerank=False, visual_audit_max_slides=0),
}


@dataclass(frozen=True)
class BudgetPolicy:
    """Числа из раздела `run:` конфига."""

    budget_seconds: float = 300.0
    modes: dict[RunMode, ModeSpec] = field(default_factory=lambda: dict(DEFAULT_MODES))
    # Отбор рискованных слайдов под аудит по картинке (`audit.risk.pick_
    # risky_slides`) — не зависит от режима, только от того, сколько слайдов
    # режим вообще разрешает смотреть (`ModeSpec.visual_audit_max_slides`).
    visual_audit_min_risk: float = 1.0
    visual_audit_batch: bool = False


@dataclass
class RunBudget:
    """Часы одного прогона. Создаётся в самом начале генерации, до первого
    вызова модели: всё, что было до создания, в бюджет не попадает.

    `clock` подменяется в тестах, чтобы проверять пороги без `sleep`."""

    deadline_seconds: float
    policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    clock: Callable[[], float] = time.monotonic
    stage_seconds: dict[str, float] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    # Режим, зафиксированный последним вызовом `decide_mode` — `None` до
    # первой контрольной точки (см. `mode_spec` про то, что это значит для
    # чтения режима до того, как хоть одна точка прошла).
    mode: RunMode | None = None
    mode_checkpoint: str | None = None
    # Все решения по контрольным точкам подряд — для отчёта (HTML/cli):
    # человеку важно видеть не только итоговый режим, но и где он менялся.
    mode_history: list[dict] = field(default_factory=list)
    # Стадии, полученные готовыми от другого задания (см. `mark_reused`).
    reused: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._started = self.clock()
        self._stopped: float | None = None

    @classmethod
    def from_policy(cls, policy: BudgetPolicy, *, clock: Callable[[], float] = time.monotonic) -> RunBudget:
        return cls(deadline_seconds=policy.budget_seconds, policy=policy, clock=clock)

    def mark_reused(self, stage: str) -> None:
        """Стадию не считало это задание: её результат (разбор шаблона,
        общая структура пакета стилей) пришёл готовым. Время ожидания всё
        равно пишется через `record`, пометка нужна отчёту."""
        self.reused[stage] = REUSED_NOTE

    def stop(self) -> None:
        """Остановить часы: задание доделано, и его время в снимке не
        должно расти, пока соседние задания ещё работают."""
        if self._stopped is None:
            self._stopped = self.clock()

    def elapsed(self) -> float:
        end = self._stopped if self._stopped is not None else self.clock()
        return end - self._started

    def remaining(self) -> float:
        """Сколько секунд осталось; отрицательное число значит, что бюджет
        уже превышен (обязательные стадии всё равно доделываются)."""
        return self.deadline_seconds - self.elapsed()


    def allowance(self, wanted: float, *, reserve: float) -> float:
        """Сколько секунд дать необязательному шагу (починка текста под
        контракт, задача U): не больше `wanted` и не больше остатка минус
        `reserve` на обязательные стадии после него (сборка, аудит,
        экспорт). Ноль значит «шаг не запускать». Метод пропал при
        слиянии задач Q и U (27 сентября 2026)."""
        return max(0.0, min(wanted, self.remaining() - reserve))
    def decide_mode(self, checkpoint: str) -> RunMode:
        """Контрольная точка пайплайна: `"after_outline"` перед раскладками
        и текстом, `"after_write"` перед сборкой, `"after_compose"` перед
        аудитом по картинке.
        Решение считается ЗДЕСЬ, по остатку времени НА МОМЕНТ ВЫЗОВА, и
        фиксируется в `self.mode` до следующего вызова — код между точками
        обязан читать `mode_spec()`, не спрашивать бюджет заново."""
        remaining = self.remaining()
        chosen = _MODE_ORDER[-1]  # EMERGENCY, если ни одна строка таблицы не подошла
        for mode in _MODE_ORDER:
            spec = self.policy.modes.get(mode)
            if spec is not None and remaining >= spec.min_remaining:
                chosen = mode
                break
        self.mode = chosen
        self.mode_checkpoint = checkpoint
        self.mode_history.append({
            "checkpoint": checkpoint, "mode": chosen.value, "remaining_seconds": round(max(remaining, 0.0), 1),
        })
        return chosen

    def mode_spec(self) -> ModeSpec:
        """Действующий режим между контрольными точками. До первой точки
        (например, стадия проверяется юнит-тестом без вызова `decide_mode`,
        или вызывающий код по какой-то причине пропустил точку) — самый
        полный режим: деградация начинается только когда её РЕАЛЬНО решили
        на контрольной точке, не по умолчанию."""
        mode = self.mode if self.mode is not None else RunMode.FULL
        return self.policy.modes.get(mode, DEFAULT_MODES[mode])

    def record(self, stage: str, seconds: float) -> None:
        """Время стадии копится, а не перезаписывается: стадия может идти
        кусками (аудит до и после автопочинки)."""
        self.stage_seconds[stage] = self.stage_seconds.get(stage, 0.0) + max(seconds, 0.0)

    def summary(self) -> dict:
        """Снимок для отчёта и интерфейса: одно задание, один бюджет.
        `shared_stages` есть, только если задание получило какую-то стадию
        готовой (`mark_reused`), со значением «переиспользовано»."""
        out = {
            "budget_seconds": self.deadline_seconds,
            "elapsed_seconds": round(self.elapsed(), 1),
            "stage_seconds": {k: round(v, 1) for k, v in self.stage_seconds.items()},
            "skipped": dict(self.skipped),
            "mode": self.mode.value if self.mode is not None else None,
            "mode_checkpoint": self.mode_checkpoint,
            "mode_history": [dict(entry) for entry in self.mode_history],
        }
        if self.reused:
            out["shared_stages"] = dict(self.reused)
        return out


def load_policy(app_yaml_path) -> BudgetPolicy:
    """Политика из `config/app.yaml`; нечитаемый конфиг даёт дефолты, а не
    падение: бюджет не должен ронять генерацию."""
    try:
        from deckforge.settings import Settings

        run = Settings.load(app_yaml_path).run
    except Exception:  # noqa: BLE001: без конфига бюджет работает на дефолтах
        return BudgetPolicy()
    modes = {
        RunMode(name): ModeSpec(
            min_remaining=spec.min_remaining, rerank=spec.rerank,
            visual_audit_max_slides=spec.visual_audit_max_slides,
        )
        for name, spec in run.modes.items()
        if name in RunMode._value2member_map_
    }
    return BudgetPolicy(
        budget_seconds=run.budget_seconds,
        modes=modes or dict(DEFAULT_MODES),
        visual_audit_min_risk=run.visual_audit_min_risk,
        visual_audit_batch=run.visual_audit_batch,
    )
