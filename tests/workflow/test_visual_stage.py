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
from deckforge.workflow.budget import BudgetPolicy, RunBudget
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
    budget = RunBudget.from_policy(
        BudgetPolicy(budget_seconds=300, visual_audit_min_remaining=75, visual_audit_max_slides=2,
                     visual_audit_min_risk=1.0, visual_audit_batch=batch),
        clock=clock,
    )
    clock.now += elapsed
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
    assert [pos for pos, _ in outcome.picked] == [5, 1]  # с нуля + critical, затем с нуля
    assert {f.slide_index for f in outcome.findings} == {1, 5}
    assert rendered == [1]
    assert "visual_audit" in budget.stage_seconds
    assert outcome.summary()["ran"] is True


def test_stage_batch_mode_uses_single_call(tmp_path: Path):
    vlm = _FakeVision()
    _budget, outcome, _ = _stage(tmp_path, _Clock(), vlm, batch=True)
    assert outcome.result is not None and outcome.result.model_calls == 1
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
