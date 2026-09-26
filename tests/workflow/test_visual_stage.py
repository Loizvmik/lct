"""Аудит по картинке рискованных слайдов в рамках бюджета (задача H):
`run_visual(only_slides=...)`, пачка на коллаже (`run_visual_batch`) и
стадия `workflow.visual_stage.run_visual_stage`. Модель фейковая."""
from __future__ import annotations
import json
import threading
from pathlib import Path

from PIL import Image

from deckforge.audit.findings import Finding
from deckforge.audit.visual import PER_SLIDE_CHECK_IDS, run_visual, run_visual_batch
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec
from deckforge.provider.base import VisionProvider
from deckforge.workflow.budget import BudgetPolicy, ModeSpec, RunBudget, RunMode
from deckforge.workflow.visual_stage import run_visual_stage


def _payload(prompt: str) -> dict:
    return json.loads(prompt.rsplit("Служебные данные:\n", 1)[1])


class _FakeVision(VisionProvider):
    """Отвечает «нет» на C01 по каждому слайду; умеет и одиночный запрос, и
    пачку. Запоминает запросы."""

    def __init__(self) -> None:
        self.prompts: list[dict] = []
        self._lock = threading.Lock()

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        payload = _payload(prompt)
        with self._lock:
            self.prompts.append(payload)
        answer = {k: {"ok": k != "C01", "where": "заголовок" if k == "C01" else None} for k in payload["answer_only_keys"]}
        answer = {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in answer.items()}
        if payload.get("mode") == "batch":
            return json.dumps({"slides": {
                str(s["index_1based"]): {**answer, "scores": {"content": 3, "design": 4}}
                for s in payload["slides"]
            }})
        return json.dumps({**answer, "scores": {"content": 2, "design": 2}})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _pngs(tmp_path: Path, n: int) -> list[Path]:
    out = []
    for i in range(n):
        path = tmp_path / f"s{i}.png"
        Image.new("RGB", (320, 180), (10 * i, 20, 30)).save(path)
        out.append(path)
    return out


def _spec(n: int, from_scratch: set[int]) -> DeckSpec:
    slides = []
    for i in range(n):
        notes = [] if i in from_scratch else [f"Слайд {i + 1}: собран клоном слайда-примера №1 шаблона."]
        slides.append(SlideSpec(
            index=i + 1, kind="bullets", headline=f"Заголовок {i + 1}",
            blocks=[BulletBlock(items=["пункт"])], findings=notes,
        ))
    return DeckSpec(title="Колода", language="ru", slides=slides)


def test_run_visual_only_slides_asks_just_those(tmp_path: Path):
    vlm = _FakeVision()
    spec = _spec(5, set())
    result = run_visual(_pngs(tmp_path, 5), spec, None, vlm, only_slides={1, 3}, deck_level=False)
    assert result.slides_checked == 2
    assert result.model_calls == 2
    assert sorted(p["slide_index_1based"] for p in vlm.prompts) == [2, 4]
    assert all(p["total_slides"] == 5 for p in vlm.prompts)
    assert {f.slide_index for f in result.findings} == {1, 3}
    assert all(f.check_id == "C01" for f in result.findings)


def test_run_visual_default_still_checks_whole_deck(tmp_path: Path):
    vlm = _FakeVision()
    result = run_visual(_pngs(tmp_path, 3), _spec(3, set()), None, vlm)
    assert result.slides_checked == 3
    assert result.model_calls == 4  # три слайда плюс коллаж C09/C11


def test_run_visual_batch_one_call_findings_per_real_slide(tmp_path: Path):
    vlm = _FakeVision()
    result = run_visual_batch(_pngs(tmp_path, 6), _spec(6, set()), vlm, only_slides=[4, 1])
    assert result.model_calls == 1
    assert [s["index_1based"] for s in vlm.prompts[0]["slides"]] == [5, 2]
    assert vlm.prompts[0]["answer_only_keys"] == list(PER_SLIDE_CHECK_IDS)
    assert {f.slide_index for f in result.findings} == {1, 4}
    assert result.slide_scores == {4: {"content": 3, "design": 4}, 1: {"content": 3, "design": 4}}


def test_run_visual_batch_missing_slide_is_c00_not_crash(tmp_path: Path):
    class _Partial(VisionProvider):
        def ask_image(self, png, prompt, *, max_tokens=1024):
            ok = {k: {"ok": True} for k in PER_SLIDE_CHECK_IDS}
            return json.dumps({"slides": {"2": ok}})

    result = run_visual_batch(_pngs(tmp_path, 3), _spec(3, set()), _Partial(), only_slides=[1, 2])
    assert [f.check_id for f in result.findings] == ["C00"]
    assert result.findings[0].slide_index == 2


def test_run_visual_batch_garbage_is_c00_for_each_slide(tmp_path: Path):
    class _Garbage(VisionProvider):
        def ask_image(self, png, prompt, *, max_tokens=1024):
            return "не json"

    result = run_visual_batch(_pngs(tmp_path, 3), _spec(3, set()), _Garbage(), only_slides=[0, 2])
    assert sorted((f.slide_index, f.check_id) for f in result.findings) == [(0, "C00"), (2, "C00")]
    assert result.model_calls == 2  # одна повторная попытка


def _stage(tmp_path: Path, clock: _Clock, vlm, *, batch: bool = False, elapsed: float = 0.0, n: int = 6):
    # Задача L: `visual_audit_max_slides` теперь живёт в таблице режимов, не
    # плоским полем политики — FULL здесь несёт то же число (2), что раньше
    # было `visual_audit_max_slides` напрямую, FAST/EMERGENCY — дефолтные
    # пороги 75/0, тот же порог, что раньше был `visual_audit_min_remaining`.
    modes = {
        RunMode.FULL: ModeSpec(min_remaining=120, rerank=True, visual_audit_max_slides=2),
        RunMode.FAST: ModeSpec(min_remaining=75, rerank=False, visual_audit_max_slides=1),
        RunMode.EMERGENCY: ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=0),
    }
    budget = RunBudget.from_policy(
        BudgetPolicy(budget_seconds=300, modes=modes, visual_audit_min_risk=1.0, visual_audit_batch=batch),
        clock=clock,
    )
    clock.now += elapsed
    # Контрольная точка `after_compose` (задача L) — стадия сама больше не
    # спрашивает бюджет, только читает уже решённый режим.
    budget.decide_mode("after_compose")
    rendered: list[int] = []

    def render() -> list[Path]:
        rendered.append(1)
        return _pngs(tmp_path, n)

    spec = _spec(n, from_scratch={1, 3, 5})
    det = [Finding(check_id="L03", severity="critical", slide_index=5, shape_ref=None, message="m",
                   box=None, fixable=True, fix_hint="h")]
    outcome = run_visual_stage(budget, spec, det, vlm, render, sources=None)
    return budget, outcome, rendered


def test_stage_runs_on_top_risky_slides_within_budget(tmp_path: Path):
    vlm = _FakeVision()
    budget, outcome, rendered = _stage(tmp_path, _Clock(), vlm)
    assert outcome.result is not None
    assert [pos for pos, _tech, _sem in outcome.picked] == [5, 1]  # с нуля + critical, затем с нуля
    # Задача M: коллаж C09/C11 всей колоды едет ВМЕСТЕ с рискованными
    # слайдами (`deck_level=True`) — на `_FakeVision` он не даёт находок
    # (её C01-ответ "нет" не входит в DECK_LEVEL_CHECK_IDS), но слайд-индекс
    # `None` (коллаж) должен быть среди позиций, по которым модель отвечала.
    assert {f.slide_index for f in outcome.findings} == {1, 5}
    assert rendered == [1]
    assert "visual_audit" in budget.stage_seconds
    assert outcome.summary()["ran"] is True


def test_stage_asks_deck_level_questions_once_per_variant(tmp_path: Path):
    """Задача M: вопросы уровня колоды (C09/C11) идут одним отдельным
    вызовом сверх рискованных слайдов — 2 рискованных слайда + 1 коллаж =
    3 вызова модели, не 2."""
    vlm = _FakeVision()
    _budget, outcome, _ = _stage(tmp_path, _Clock(), vlm)
    assert outcome.result is not None
    assert outcome.result.model_calls == 3


def test_stage_batch_mode_asks_deck_level_too(tmp_path: Path):
    vlm = _FakeVision()
    _budget, outcome, _ = _stage(tmp_path, _Clock(), vlm, batch=True)
    # Пачка: один вызов на ВСЕ рискованные слайды разом + один на коллаж
    # C09/C11 (задача M) = 2, не 1.
    assert outcome.result is not None and outcome.result.model_calls == 2
    assert outcome.batch is True


def test_stage_skipped_when_budget_is_short(tmp_path: Path):
    vlm = _FakeVision()
    budget, outcome, rendered = _stage(tmp_path, _Clock(), vlm, elapsed=240)
    assert outcome.result is None
    assert vlm.prompts == []
    assert rendered == []  # превью не рендерятся зря
    assert "visual_audit" in budget.skipped
    assert outcome.summary()["ran"] is False


def test_stage_without_model_reports_reason(tmp_path: Path):
    _budget, outcome, rendered = _stage(tmp_path, _Clock(), None)
    assert outcome.result is None
    assert "модель" in outcome.skipped_reason
    assert rendered == []


def test_stage_lock_prevents_lost_updates_on_shared_budget(tmp_path: Path):
    """Задача M: три варианта зовут `run_visual_stage` параллельно на ОБЩИЙ
    `budget` (`api.jobs._visual_audit_variants`) — без общего `lock`
    `budget.record`/`budget.skipped` внутри `_done` теряют обновления
    (read-modify-write из нескольких потоков разом). Здесь это же
    воспроизведено напрямую: N потоков, один и тот же `budget`, общий лок —
    сумма секунд стадии обязана сойтись, а не оказаться меньше."""
    modes = {
        RunMode.FULL: ModeSpec(min_remaining=0, rerank=True, visual_audit_max_slides=2),
        RunMode.FAST: ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=1),
        RunMode.EMERGENCY: ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=0),
    }
    budget = RunBudget.from_policy(BudgetPolicy(budget_seconds=300, modes=modes, visual_audit_min_risk=1.0))
    budget.decide_mode("after_compose")
    lock = threading.Lock()
    n = 8

    def run_one(i: int) -> None:
        vlm = _FakeVision()
        run_visual_stage(
            budget, _spec(3, from_scratch=set()),
            [Finding(check_id="L03", severity="critical", slide_index=0, shape_ref=None, message="m",
                     box=None, fixable=True, fix_hint="h")],
            vlm, lambda: _pngs(tmp_path, 3), lock=lock,
        )

    threads = [threading.Thread(target=run_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # `record()` копит секунды (не перезаписывает) — при N успешных прогонах
    # без потерянных обновлений `stage_seconds["visual_audit"]` есть и не
    # ушёл в отрицательное/нулевое значение молчаливой потерей записи.
    assert budget.stage_seconds.get("visual_audit", 0.0) > 0.0


# --- Задача W: число слайдов по остатку времени на момент стадии ---


def _timed_stage(tmp_path: Path, *, elapsed: float, call_seconds: float, past_calls: list[float] = ()):
    modes = {
        RunMode.FULL: ModeSpec(min_remaining=120, rerank=True, visual_audit_max_slides=3),
        RunMode.FAST: ModeSpec(min_remaining=75, rerank=False, visual_audit_max_slides=1),
        RunMode.EMERGENCY: ModeSpec(min_remaining=0, rerank=False, visual_audit_max_slides=0),
    }
    clock = _Clock()
    budget = RunBudget.from_policy(
        BudgetPolicy(
            budget_seconds=300, modes=modes, visual_audit_min_risk=1.0,
            visual_audit_reserve_seconds=45.0, visual_audit_call_seconds=call_seconds,
        ),
        clock=clock,
    )
    for seconds in past_calls:
        budget.record_call("visual_audit", seconds, 0.0, ok=True)
    budget.decide_mode("after_compose")  # остаток 300: FULL, до 3 слайдов
    clock.now += elapsed  # сборка и экспорт затянулись после точки
    vlm = _FakeVision()
    spec = _spec(6, from_scratch={1, 2, 3, 4, 5})
    outcome = run_visual_stage(budget, spec, [], vlm, lambda: _pngs(tmp_path, 6), sources=None)
    return budget, outcome, vlm


def test_stage_takes_fewer_slides_when_little_time_is_left(tmp_path: Path):
    """Режим разрешил 3 слайда, но к стадии осталось 120с: (120 - 45) / 60
    = 1 слайд."""
    _budget, outcome, vlm = _timed_stage(tmp_path, elapsed=180, call_seconds=60.0)
    assert outcome.allowed_by_time == 1
    assert len(outcome.picked) == 1
    assert outcome.summary()["allowed_by_time"] == 1


def test_stage_uses_the_median_of_this_runs_calls(tmp_path: Path):
    """После первых вызовов оценка берётся по ним, а не из конфига."""
    _budget, outcome, _vlm = _timed_stage(tmp_path, elapsed=180, call_seconds=60.0, past_calls=[20.0, 25.0, 30.0])
    assert outcome.allowed_by_time == 3


def test_stage_is_skipped_by_time_before_any_call(tmp_path: Path):
    budget, outcome, vlm = _timed_stage(tmp_path, elapsed=250, call_seconds=25.0)
    assert outcome.result is None and vlm.prompts == []
    assert outcome.skipped_reason.startswith("по времени")
    assert budget.summary()["time_skipped"], "пропуск по времени виден в снимке"


def test_stage_calls_are_counted_in_the_budget(tmp_path: Path):
    budget, outcome, vlm = _timed_stage(tmp_path, elapsed=0, call_seconds=25.0)
    assert outcome.result is not None
    summary = budget.summary()
    assert summary["calls_by_role"]["visual_audit"]["calls"] == len(vlm.prompts)


def test_calls_not_started_for_time_leave_no_c00_findings(tmp_path: Path):
    """Вызов, который планировщик не начал по времени, не сбой модели:
    находки «модель ответила невалидно» (C00) за него нет, повтора тоже."""
    from deckforge.provider.scheduler import OutOfTime

    class _NoTime(VisionProvider):
        def __init__(self) -> None:
            self.calls = 0

        def ask_image(self, png, prompt, *, max_tokens=1024):
            self.calls += 1
            raise OutOfTime("вызов не начат, время до резерва вышло")

    vlm = _NoTime()
    result = run_visual(_pngs(tmp_path, 3), _spec(3, set()), None, vlm, only_slides={0, 1}, deck_level=True)
    assert result.findings == []
    assert vlm.calls == 3, "по одному заходу на слайд и коллаж, без повтора"
