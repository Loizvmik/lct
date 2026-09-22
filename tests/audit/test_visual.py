"""Тесты `deckforge.audit.visual` (Task 12, Приложение 1 ТЗ, «Валидация
контента»).

Офлайн-тесты (без сети) проверяют инфраструктуру, которая обязана работать
одинаково при любом ответе модели: валидацию схемой, честную деградацию без
(мультимодальной) модели, устойчивость к невалидному ответу, число вызовов
модели на колоду (N слайдов + 1 коллаж, не 11×N — докстрока `visual.py`).

`@live`-тесты (`YANDEX_API_KEY` в окружении) идут в реальную модель по
реальным превью, отрендеренным LibreOffice (`soffice_available()`) — сеть и
недетерминированность ответа означают, что они НЕ гоняются в
`uv run pytest tests/ -v` без ключа и намеренно допускают некоторый допуск в
assert'ах (модель может ошибиться, см. докстроку `audit.visual`: «ТЗ
дословно: недетерминированная проверка... может дать разный ответ»)."""
from __future__ import annotations
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from deckforge.audit.visual import (
    CHECK_IDS, DECK_LEVEL_CHECK_IDS, PER_SLIDE_CHECK_IDS, VisualAuditResult,
    _build_collage, _findings_from_answers, _load_agent_prompt, _parse_answer, _supports_vision,
    run_visual,
)
from deckforge.compose.builder import Variant, build_deck
from deckforge.plan.outline import SourceDoc
from deckforge.plan.spec import BulletBlock, DeckSpec, SlideSpec
from deckforge.provider.base import VisionProvider
from deckforge.render.soffice import soffice_available, to_pngs
from deckforge.template.profile import TemplateProfile

live = pytest.mark.skipif(not os.getenv("YANDEX_API_KEY"), reason="нет YANDEX_API_KEY")
needs_render = pytest.mark.skipif(not soffice_available(), reason="LibreOffice не установлен")

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")
PROFILE = TemplateProfile.from_file(TEMPLATE)


# ---------------------------------------------------------------------------
# Фейковые провайдеры
# ---------------------------------------------------------------------------


class _TextOnlyProvider:
    """Не мультимодальна и не наследует `VisionProvider` — ни `ask_image`,
    ни `.card.vision`. То же самое, что реальный текстовый провайдер без
    vision-возможности."""


class _AllOkVisionProvider(VisionProvider):
    """Всегда отвечает "всё хорошо" на запрошенное подмножество вопросов —
    считает, сколько раз её вызвали (`calls`), нужен тестам числа вызовов и
    устойчивости к параллельным потокам."""

    def __init__(self):
        self.calls: list[str] = []

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        self.calls.append(prompt)
        keys = json.loads(prompt.rsplit("Служебные данные:\n", 1)[1])["answer_only_keys"]
        return json.dumps({k: {"ok": True} for k in keys})


class _BrokenJsonProvider(VisionProvider):
    """Мультимодальна (наследует `VisionProvider`), но отвечает не JSON —
    тест устойчивости пайплайна к невалидному ответу модели."""

    def ask_image(self, png: bytes, prompt: str, *, max_tokens: int = 1024) -> str:
        return "конечно! вот мой ответ без единой фигурной скобки"


class _CardVisionProvider:
    """Не наследует `VisionProvider`, но несёт `.card.vision` — тот же
    духк-тайпинг, каким `YandexProvider` сообщает о себе через
    `provider.registry.ModelCard` (`_supports_vision`, docstring
    `visual.py`)."""

    class _Card:
        def __init__(self, vision: bool):
            self.vision = vision

    def __init__(self, vision: bool):
        self.card = self._Card(vision)


def _tiny_png(color=(10, 20, 30)) -> Path:
    import tempfile
    path = Path(tempfile.mktemp(suffix=".png"))
    Image.new("RGB", (320, 180), color).save(path)
    return path


def _tiny_spec(n: int) -> DeckSpec:
    return DeckSpec(
        title="Тестовая колода",
        language="ru",
        slides=[
            SlideSpec(index=i, kind="bullets", headline=f"Слайд {i}", blocks=[BulletBlock(items=["пункт"])])
            for i in range(n)
        ],
    )


# ---------------------------------------------------------------------------
# AGENT.md
# ---------------------------------------------------------------------------


def test_agent_prompt_has_frontmatter_with_version():
    meta, body = _load_agent_prompt()
    assert meta["name"] == "content-auditor"
    assert meta["version"]
    assert meta["model_role"] == "content_audit"
    assert body


def test_agent_prompt_contains_all_eleven_questions_verbatim():
    _meta, body = _load_agent_prompt()
    questions = [
        "Заголовок содержит вывод, а не просто называет тему?",
        "Содержимое слайда соответствует заголовку?",
        "Слайд пересказывается одним предложением?",
        "Все цифры и факты со слайда есть в исходных материалах?",
        "На слайде есть содержание, а не только заголовок?",
        "Картинки и иконки относятся к теме слайда?",
        "Нет служебного мусора: реплик спикера, кусков промпта?",
        "Текст без опечаток?",
        "Вся колода на одном языке?",
        "Все строки таблицы и элементы легенды работают на мысль слайда?",
        "Соседние слайды связаны между собой по логике?",
    ]
    for q in questions:
        assert q in body


def test_check_ids_cover_eleven_questions_split_slide_vs_deck():
    assert len(CHECK_IDS) == 11
    assert set(PER_SLIDE_CHECK_IDS) | set(DECK_LEVEL_CHECK_IDS) == set(CHECK_IDS)
    assert set(PER_SLIDE_CHECK_IDS) & set(DECK_LEVEL_CHECK_IDS) == set()
    assert DECK_LEVEL_CHECK_IDS == ("C09", "C11")


# ---------------------------------------------------------------------------
# _supports_vision
# ---------------------------------------------------------------------------


def test_supports_vision_none_is_false():
    assert _supports_vision(None) is False


def test_supports_vision_false_for_text_only_provider():
    assert _supports_vision(_TextOnlyProvider()) is False


def test_supports_vision_true_for_vision_provider_subclass():
    assert _supports_vision(_AllOkVisionProvider()) is True


def test_supports_vision_reads_card_attribute():
    assert _supports_vision(_CardVisionProvider(vision=True)) is True
    assert _supports_vision(_CardVisionProvider(vision=False)) is False


# ---------------------------------------------------------------------------
# _parse_answer — валидация схемой
# ---------------------------------------------------------------------------


def test_parse_answer_accepts_valid_json():
    raw = json.dumps({"C01": {"ok": True}, "C05": {"ok": False, "where": "нет буллетов"}})
    result = _parse_answer(raw, ("C01", "C05"))
    assert result == {"C01": (True, None), "C05": (False, "нет буллетов")}


def test_parse_answer_strips_markdown_json_fence():
    """Живой прогон обязательной проверки задачи: qwen3.6 оборачивает ответ
    в ```json ... ``` вопреки AGENT.md ("никакого текста вне JSON") — это
    оформление, не сломанный формат, снимается перед разбором JSON."""
    raw = "```json\n" + json.dumps({"C01": {"ok": True}}) + "\n```"
    assert _parse_answer(raw, ("C01",)) == {"C01": (True, None)}


def test_parse_answer_strips_bare_markdown_fence_without_json_tag():
    raw = "```\n" + json.dumps({"C01": {"ok": True}}) + "\n```"
    assert _parse_answer(raw, ("C01",)) == {"C01": (True, None)}


def test_parse_answer_rejects_non_json():
    with pytest.raises(ValueError, match="JSON"):
        _parse_answer("не json", ("C01",))


def test_parse_answer_rejects_missing_key():
    with pytest.raises(ValueError, match="C05"):
        _parse_answer(json.dumps({"C01": {"ok": True}}), ("C01", "C05"))


def test_parse_answer_rejects_non_bool_ok():
    with pytest.raises(ValueError):
        _parse_answer(json.dumps({"C01": {"ok": "да"}}), ("C01",))


def test_parse_answer_rejects_top_level_non_object():
    with pytest.raises(ValueError, match="объект"):
        _parse_answer(json.dumps([1, 2, 3]), ("C01",))


# ---------------------------------------------------------------------------
# _findings_from_answers
# ---------------------------------------------------------------------------


def test_findings_from_answers_skips_ok_true():
    findings = _findings_from_answers(0, {"C01": (True, None)})
    assert findings == []


def test_findings_from_answers_emits_for_ok_false():
    findings = _findings_from_answers(2, {"C05": (False, "только заголовок")})
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "C05"
    assert f.slide_index == 2
    assert "только заголовок" in f.message
    assert f.fixable is False


# ---------------------------------------------------------------------------
# _build_collage
# ---------------------------------------------------------------------------


def test_build_collage_returns_valid_png_taller_than_one_slide():
    paths = [_tiny_png((i * 30, 0, 0)) for i in range(3)]
    try:
        png_bytes = _build_collage(paths)
        import io
        im = Image.open(io.BytesIO(png_bytes))
        assert im.format == "PNG"
        single = Image.open(paths[0])
        assert im.height > single.height  # три слайда подряд, не один
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# run_visual — офлайн: честная деградация / устойчивость к мусору / число вызовов
# ---------------------------------------------------------------------------


def test_visual_audit_degrades_loudly_without_a_vision_model():
    """Если выбранная модель не мультимодальна, аудит обязан сказать об
    этом, а не молча вернуть пустой список и создать видимость проверки."""
    spec = _tiny_spec(1)
    pngs = [_tiny_png()]
    try:
        report = run_visual(pngs, spec, PROFILE, _TextOnlyProvider())
        assert report.skipped_reason and "мультимодал" in report.skipped_reason
        assert list(report) == []
    finally:
        pngs[0].unlink(missing_ok=True)


def test_visual_audit_degrades_loudly_without_any_provider():
    spec = _tiny_spec(1)
    pngs = [_tiny_png()]
    try:
        report = run_visual(pngs, spec, PROFILE, None)
        assert report.skipped_reason
        assert list(report) == []
    finally:
        pngs[0].unlink(missing_ok=True)


def test_malformed_model_answer_does_not_crash_the_pipeline():
    spec = _tiny_spec(2)
    pngs = [_tiny_png(), _tiny_png()]
    try:
        result = run_visual(pngs, spec, PROFILE, _BrokenJsonProvider(), max_workers=2)
        assert result.skipped_reason is None  # аудит РЕАЛЬНО выполнялся, просто ответы не разобрались
        findings = list(result)
        assert findings, "невалидный ответ модели обязан дать хоть одну находку C00, не тишину"
        assert all(f.check_id == "C00" for f in findings)
    finally:
        for p in pngs:
            p.unlink(missing_ok=True)


def test_run_visual_makes_n_plus_one_model_calls():
    """N слайдов -> N вызовов по одному на слайд + 1 вызов на коллаж всей
    колоды (C09/C11) — не 11×N (докстрока `visual.py`)."""
    spec = _tiny_spec(4)
    pngs = [_tiny_png((i * 10, 0, 0)) for i in range(4)]
    provider = _AllOkVisionProvider()
    try:
        result = run_visual(pngs, spec, PROFILE, provider, max_workers=4)
        assert result.skipped_reason is None
        assert list(result) == []  # всё "ok": true -> ни одной находки
        assert result.model_calls == 5
        assert len(provider.calls) == 5
        assert result.slides_checked == 4
    finally:
        for p in pngs:
            p.unlink(missing_ok=True)


def test_run_visual_deck_level_call_asks_only_deck_level_keys():
    spec = _tiny_spec(2)
    pngs = [_tiny_png(), _tiny_png((0, 0, 99))]
    provider = _AllOkVisionProvider()
    try:
        run_visual(pngs, spec, PROFILE, provider, max_workers=2)
        keys_asked = [json.loads(p.rsplit("Служебные данные:\n", 1)[1])["answer_only_keys"] for p in provider.calls]
        per_slide_calls = [k for k in keys_asked if set(k) == set(PER_SLIDE_CHECK_IDS)]
        deck_calls = [k for k in keys_asked if set(k) == set(DECK_LEVEL_CHECK_IDS)]
        assert len(per_slide_calls) == 2
        assert len(deck_calls) == 1
    finally:
        for p in pngs:
            p.unlink(missing_ok=True)


def test_run_visual_findings_sorted_with_deck_level_first():
    """`slide_index=None` (проверки на колоду целиком) сортируется как -1 —
    тот же порядок, что и I01 (`slide_index=None`) в `audit.deterministic.
    _stable_sort`."""

    class _Provider(VisionProvider):
        def ask_image(self, png, prompt, *, max_tokens=1024):
            keys = json.loads(prompt.rsplit("Служебные данные:\n", 1)[1])["answer_only_keys"]
            return json.dumps({k: {"ok": False, "where": "тест"} for k in keys})

    spec = _tiny_spec(2)
    pngs = [_tiny_png(), _tiny_png((0, 5, 5))]
    try:
        result = run_visual(pngs, spec, PROFILE, _Provider(), max_workers=2)
        findings = list(result)
        assert findings[0].slide_index is None
        assert findings[0].check_id in DECK_LEVEL_CHECK_IDS
        assert all(f.slide_index in (0, None) for f in findings[:len(DECK_LEVEL_CHECK_IDS)])
    finally:
        for p in pngs:
            p.unlink(missing_ok=True)


def test_visual_audit_result_is_iterable_and_sized():
    result = VisualAuditResult(findings=[])
    assert len(result) == 0
    assert list(result) == []


# ---------------------------------------------------------------------------
# @live — реальная модель, реальный рендер
# ---------------------------------------------------------------------------


def _build_and_render(spec: DeckSpec, tmp_path: Path, name: str) -> list[Path]:
    built = build_deck(spec, PROFILE, TEMPLATE, Variant.dense)
    return to_pngs(built, tmp_path / name)


@pytest.fixture
def vlm():
    from deckforge.provider.yandex import YandexProvider
    # Дедлайн выше дефолта (60с, `DEFAULT_DEADLINE_SECONDS` в
    # `provider/yandex.py`): визуальный аудит стартует с бюджетом
    # `max_tokens`, заметно превышающим потолок остальных ролей конвейера
    # (`_PER_SLIDE_MAX_TOKENS`/`_DECK_LEVEL_MAX_TOKENS`, `audit/visual.py` —
    # живой замер там же), и честный (не зависший) ответ на таком бюджете
    # может занять больше 60с (обязательная проверка задачи: до ~46с на
    # слайде, замер не гарантирует, что это потолок).
    return YandexProvider(
        model="qwen3.6-35b-a3b",
        api_key=os.environ.get("YANDEX_API_KEY"),
        folder_id=os.environ.get("YANDEX_FOLDER_ID"),
        deadline_seconds=180.0,
    )


@live
@needs_render
def test_missing_body_is_caught(vlm, tmp_path):
    """C05: на слайде есть содержание, а не только заголовок — раздел-
    разделитель (`kind="section"`) намеренно несёт только заголовок,
    честный ответ модели на этот вопрос — "нет"."""
    spec = DeckSpec(
        title="Только заголовок",
        language="ru",
        slides=[SlideSpec(index=0, kind="section", headline="Итоги квартала")],
    )
    pngs = _build_and_render(spec, tmp_path, "title-only")
    result = run_visual(pngs, spec, PROFILE, vlm, max_workers=1)
    assert result.skipped_reason is None
    assert "C05" in {f.check_id for f in result}


@live
@needs_render
def test_invented_number_is_caught(vlm, tmp_path):
    """C04: все цифры со слайда есть в исходных материалах — слайд несёт
    полностью выдуманную статистику, которой нет ни в edu-platform
    sources.md, ни где-либо ещё."""
    sources_text = (Path("fixtures/content-packs/edu-platform") / "sources.md").read_text(encoding="utf-8")
    spec = DeckSpec(
        title="Итоги учебной платформы",
        language="ru",
        slides=[
            SlideSpec(
                index=0, kind="bullets", headline="Аномальный рост вовлечённости",
                blocks=[BulletBlock(items=[
                    "Выдуманная метрика: 314 592 активных пользователя за один день",
                    "Рост показателя на 812% за неделю — рекорд платформы",
                ])],
            ),
        ],
    )
    pngs = _build_and_render(spec, tmp_path, "invented-number")
    result = run_visual(
        pngs, spec, PROFILE, vlm, sources=[SourceDoc(name="sources.md", text=sources_text)], max_workers=1,
    )
    assert result.skipped_reason is None
    assert "C04" in {f.check_id for f in result}


@live
@needs_render
def test_clean_slide_passes_most_checks(vlm, tmp_path):
    sources_text = "Доведение курса до конца выросло с 41% до 57% в четвёртом квартале."
    spec = DeckSpec(
        title="Доведение курса выросло",
        language="ru",
        slides=[
            SlideSpec(
                index=0, kind="bullets", headline="Доведение курса выросло с 41% до 57%",
                blocks=[BulletBlock(items=[
                    "Доведение до конца выросло с 41% в первом квартале до 57% в четвёртом",
                    "Рост обеспечили три изменения, внедрённые в октябре",
                ])],
                source_note="Источник: sources.md",
            ),
        ],
    )
    pngs = _build_and_render(spec, tmp_path, "clean")
    result = run_visual(
        pngs, spec, PROFILE, vlm, sources=[SourceDoc(name="sources.md", text=sources_text)], max_workers=1,
    )
    assert result.skipped_reason is None
    assert len(result) <= 2


@live
@needs_render
def test_deterministic_finding_slide_also_flagged_by_visual_audit(vlm, tmp_path):
    """Слайд, на котором детерминированный аудит уже нашёл проблему (пустой
    почти без содержания слайд, вручную опустошённый после сборки) —
    визуальный аудит, глядя на ту же картинку, обязан заметить то же самое
    (C05)."""
    from pptx import Presentation

    spec = DeckSpec(
        title="Будет опустошён",
        language="ru",
        slides=[
            SlideSpec(
                index=0, kind="bullets", headline="Итоги пилота",
                blocks=[BulletBlock(items=["Пилот прошёл успешно, показатели выросли"])],
            ),
        ],
    )
    built = build_deck(spec, PROFILE, TEMPLATE, Variant.dense)

    from deckforge.audit.config import AuditConfig
    from deckforge.audit.deterministic import run_deterministic

    prs = Presentation(str(built))
    slide = prs.slides[0]
    # Опустошаем все текстовые рамки, кроме заголовка — тот же дефект,
    # что ловит D02/I03 детерминированного аудита ("пустой/почти пустой
    # слайд"), и одновременно C05 визуального.
    from deckforge.ooxml.ns import qn
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        if shape.text_frame.text.strip() == "Итоги пилота":
            continue
        for p in list(shape.text_frame._txBody.findall(qn("a:p"))):
            for r in list(p.findall(qn("a:r"))):
                p.remove(r)
    emptied = tmp_path / "emptied.pptx"
    prs.save(str(emptied))

    config = AuditConfig.load()
    det_findings = run_deterministic(emptied, PROFILE, config)
    det_ids = {f.check_id for f in det_findings}

    pngs = to_pngs(emptied, tmp_path / "emptied-png")
    vis_result = run_visual(pngs, spec, PROFILE, vlm, max_workers=1)
    assert vis_result.skipped_reason is None
    vis_ids = {f.check_id for f in vis_result}

    # Не требуем БУКВАЛЬНО одного и того же check_id (детерминированный и
    # визуальный аудиты используют разные идентификаторы) — требуем, что
    # ОБА заметили дефект на этом слайде: детерминированный обязан (это его
    # прямая работа), визуальный — по крайней мере C05 ("есть содержание,
    # а не только заголовок").
    assert det_ids, "детерминированный аудит обязан заметить опустошённый слайд"
    assert "C05" in vis_ids, f"визуальный аудит не заметил опустошённый слайд, ответил: {vis_ids}"
