"""Командная строка DeckForge.

`deckforge parse <template.pptx> -o profile.json` — разбирает шаблон в
`TemplateProfile` и печатает человеку отчёт «откуда что взято» и
предупреждения о деградировавших источниках (сам профиль целиком уходит в
файл, не в терминал — он велик, а человеку нужен смысл, не байты JSON).

Именование ролей палитры моделью — необязательное: без `.env`/ключа команда
всё равно разбирает шаблон целиком, просто с ролями из детерминированного
запасного варианта (see naming.py) — сборка профиля не должна требовать
сети.
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import run_deterministic
from deckforge.audit.report import AuditReport
from deckforge.audit.visual import run_visual
from deckforge.compose.builder import build_deck, count_embedded_photos
from deckforge.plan.outline import build_outline, load_content_pack
from deckforge.plan.photos import assign_photos, load_content_pack_photos
from deckforge.plan.spec import deck_spec_from_debug_dict, deck_spec_to_dict
from deckforge.plan.variants import Variant, apply_variant
from deckforge.plan.writer import AGENT_MAX_STEPS_DEFAULT, DEFAULT_WRITER_MAX_WORKERS, rerank_patterns, write_slides
from deckforge.provider.base import LLMProvider
from deckforge.provider.registry import ModelNotAllowed
from deckforge.provider.yandex import YandexProvider
from deckforge.render.soffice import to_pngs
from deckforge.settings import Settings
from deckforge.template.profile import TemplateProfile
from deckforge.workflow.budget import RunBudget, RunMode, load_policy
from deckforge.workflow.visual_stage import run_visual_stage

APP_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "app.yaml"


def _build_role_provider(role: str, *, deadline_seconds: float | None = None) -> LLMProvider | None:
    """Провайдер для роли `role` (`outline`/`writer`/`pattern_picker`/
    `palette_namer`), либо `None`, если секретов нет или их не хватает для
    клиента — весь пайплайн (`parse`/`generate`) обязан продолжить работу
    запасным вариантом, не падать без сети/ключа."""
    try:
        settings = Settings.load(APP_YAML_PATH)
    except Exception:
        return None
    if not settings.yandex_api_key or not settings.yandex_folder_id:
        return None
    try:
        return YandexProvider(
            model=settings.llm.model_for(role),
            api_key=settings.yandex_api_key,
            folder_id=settings.yandex_folder_id,
            deadline_seconds=deadline_seconds if deadline_seconds is not None else settings.llm.deadline_seconds,
        )
    except (ValueError, ModelNotAllowed):
        return None


def _rerank(deck, profile, variants, budget: RunBudget | None = None) -> tuple[dict, list[str]]:
    """Задача D: модель выбирает раскладку из трёх, отобранных кодом, для
    airy и visual. Возвращает `(вариант -> {номер слайда -> pattern_id},
    заметки)`. Выключено в конфиге, нет ключа или конфиг не читается —
    пустой выбор, варианты собираются чисто детерминированно.

    Задача L: идёт ли rerank вообще, решает режим прогона (`budget.mode_
    spec().rerank`), зафиксированный на контрольной точке `after_write`
    (см. `_cmd_generate`) — не отдельный порог этой функции."""
    try:
        settings = Settings.load(APP_YAML_PATH)
    except Exception:
        return {}, []
    if not settings.plan.rerank_variants:
        return {}, []
    if budget is not None and not budget.mode_spec().rerank:
        mode_name = budget.mode.value if budget.mode is not None else RunMode.FULL.value
        return {}, [f"переранжирование пропущено режимом прогона {mode_name}"]
    llm = _build_role_provider("pattern_picker", deadline_seconds=settings.llm.pattern_picker_deadline_seconds)
    notes: list[str] = []
    chosen = rerank_patterns(
        deck, profile, variants, llm,
        max_workers=settings.llm.pattern_picker_max_workers,
        budget_seconds=settings.llm.pattern_picker_step_budget_seconds, notes=notes,
    )
    by_variant: dict = {}
    for (variant, index), pattern_id in chosen.items():
        by_variant.setdefault(variant, {})[index] = pattern_id
    return by_variant, notes


def _build_namer() -> LLMProvider | None:
    return _build_role_provider("palette_namer")


def _build_vlm() -> LLMProvider | None:
    """Мультимодальный провайдер для аудита по картинке (роль
    `content_audit`) — та же честная деградация до `None`, что и у ролей
    выше: `deckforge audit-visual` без ключа не падает, а сообщает через
    `VisualAuditResult.skipped_reason` (см. `audit.visual.run_visual`),
    почему проверка не выполнялась."""
    return _build_role_provider("content_audit")


def _build_pattern_kind_vlm() -> LLMProvider | None:
    """Мультимодальный провайдер для уточнения вида раскладки по картинке
    слайда-примера (Task 18, роль `pattern_kind`) — та же честная
    деградация, что и `_build_vlm`: `TemplateProfile.from_file(...,
    vision=...)` без ключа продолжает работать чистой геометрией
    (`template.patterns._classify_kind`), см. `template.vision_kind`."""
    return _build_role_provider("pattern_kind")


def _build_pattern_schema_vlm() -> LLMProvider | None:
    """Провайдер схемы слотов раскладки (задача F, роль `pattern_schema`):
    своя строка в `llm.roles`, чтобы роль можно было перевести на другую
    модель, не трогая вид раскладки. Без ключа — `None`, схема не снимается."""
    return _build_role_provider("pattern_schema")


def _writer_max_workers() -> int:
    """Число слайдов, чей текст пишется одновременно (`plan.writer.write_
    slides`) — из `config/app.yaml` (`llm.slide_writer_max_workers`, см. её
    комментарий там про происхождение числа); без читаемого конфига —
    запасной дефолт модуля (`DEFAULT_WRITER_MAX_WORKERS`), тот же принцип,
    что и у `_build_role_provider` (сеть/конфиг недоступны — пайплайн
    продолжает работать, не падает)."""
    try:
        return Settings.load(APP_YAML_PATH).llm.slide_writer_max_workers
    except Exception:
        return DEFAULT_WRITER_MAX_WORKERS


def _writer_agent_max_steps() -> int:
    """Task 19 — тот же приём, что и `_writer_max_workers` выше: бюджет
    сетевых кругов агентного цикла (`config/app.yaml`, `llm.slide_writer_
    agent_max_steps`), запасной дефолт модуля без читаемого конфига."""
    try:
        return Settings.load(APP_YAML_PATH).llm.slide_writer_agent_max_steps
    except Exception:
        return AGENT_MAX_STEPS_DEFAULT


def _writer_fill_repair() -> int | None:
    """Ремонт недобора (`plan.writer._repair_underfill`): `None` — выключен,
    иначе предел числа элементов слайда (0 — без предела). Без читаемого
    конфига включён для всех слайдов, как в `config/app.yaml`."""
    try:
        llm = Settings.load(APP_YAML_PATH).llm
    except Exception:
        return 0
    return llm.slide_writer_fill_repair_max_items if llm.slide_writer_fill_repair else None


def _cmd_parse(args: argparse.Namespace) -> int:
    namer = _build_namer()
    vision = _build_pattern_kind_vlm()
    started = time.monotonic()
    profile = TemplateProfile.from_file(
        args.template, namer=namer, vision=vision, schema=_build_pattern_schema_vlm(),
    )
    elapsed = time.monotonic() - started

    args.output.write_text(profile.to_json(), encoding="utf-8")

    print(f"{args.template.name}: разобрано за {elapsed:.3f}с, профиль записан в {args.output}")
    print(f"Лейаутов: {len(profile.layouts)}, паттернов: {len(profile.patterns)}")
    print(f"Роли палитры: {', '.join(f'{role}={hexv}' for role, hexv in sorted(profile.palette_roles.items())) or '(нет)'}")

    if profile.patterns:
        # Задача C ("превью PNG на каждый паттерн шаблона в кэше профиля") —
        # человек, глядя на отчёт, должен уметь открыть глазами то, что
        # разбор вытащил из шаблона, не только прочитать JSON целиком.
        print("\nПаттерны:")
        for pattern in profile.patterns:
            preview = profile.pattern_preview_path(pattern)
            preview_text = str(preview) if preview is not None else "(превью нет — soffice недоступен или ключ модели не передан)"
            print(f"  - {pattern.pattern_id} ({pattern.kind}): {preview_text}")

    print("\nОткуда что взято:")
    for line in profile.provenance:
        print(f"  - {line}")

    if profile.warnings:
        print("\nПредупреждения:")
        for line in profile.warnings:
            print(f"  ! {line}")

    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    """Весь путь от брифа до готовой презентации (Task 13, "эта задача
    замыкает пайплайн"): разбор шаблона -> структура -> текст слайдов ->
    три варианта вёрстки -> сборка -> детерминированный аудит каждого
    варианта. Без ключа модели (`.env`) продолжает работать запасными
    вариантами на каждом шаге (`build_outline`/`write_slides` без `llm`) —
    результат хуже по содержанию, но пайплайн не падает.

    Полный аудит по картинке всех слайдов (C01-C11) сюда не входит: замер
    `task-12-report.md` дал 302.8с генерации плюс 152.8с такого аудита,
    почти вдвое больше пяти минут ТЗ. Задача H вернула в генерацию его
    часть: вопросы C01-C08, C10 по нескольким самым рискованным слайдам
    одного варианта (`workflow.visual_stage`), и только если бюджет прогона
    (`workflow.budget.RunBudget`, `run:` в app.yaml) это позволяет. Не
    пошла стадия: печатается причина. Полный аудит остаётся отдельной
    командой `deckforge audit-visual` на готовом файле."""
    # Бюджет прогона создаётся первым делом: пять минут ТЗ считаются от
    # начала команды.
    budget = RunBudget.from_policy(load_policy(APP_YAML_PATH))
    namer = _build_namer()
    vision = _build_pattern_kind_vlm()
    outline_llm = _build_role_provider("outline")
    writer_llm = _build_role_provider("writer")

    started = time.monotonic()
    profile = TemplateProfile.from_file(
        args.template, namer=namer, vision=vision, schema=_build_pattern_schema_vlm(),
    )
    parsed_at = time.monotonic()
    budget.record("parse", parsed_at - started)
    print(f"{args.template.name}: разобран за {parsed_at - started:.1f}с, паттернов: {len(profile.patterns)}")

    brief, sources, meta = load_content_pack(args.content_pack)
    target_slides = args.target_slides or meta.get("target_slides")
    outline = build_outline(
        brief, sources, profile, outline_llm, target_slides,
        title=meta.get("title", args.content_pack.name), language=meta.get("language", "ru"),
    )
    outlined_at = time.monotonic()
    budget.record("outline", outlined_at - parsed_at)
    print(f"Структура: {len(outline.slides)} слайдов за {outlined_at - parsed_at:.1f}с")

    # Текст слайдов пишется ПАРАЛЛЕЛЬНО (max_workers — config/app.yaml,
    # `llm.slide_writer_max_workers`) — слайды друг от друга не зависят, а
    # последовательное написание было девяноста процентами времени всей
    # генерации (живой замер, docstring `write_slides`/`task-12-report.md`).
    writer_max_workers = args.writer_max_workers if args.writer_max_workers is not None else _writer_max_workers()
    deck = write_slides(
        outline, sources, profile, writer_llm,
        max_workers=writer_max_workers, agent_max_steps=_writer_agent_max_steps(),
        template_path=args.template, fill_repair_max_items=_writer_fill_repair(),
    )
    written_at = time.monotonic()
    budget.record("write", written_at - outlined_at)
    print(f"Текст слайдов написан за {written_at - outlined_at:.1f}с")
    if "fill_repairs" in deck.meta:
        print(
            f"  дописано до объёма: {deck.meta['fill_repairs_accepted']} из {deck.meta['fill_repairs']} слайдов, "
            f"вызовы ремонта заняли {deck.meta['fill_repair_seconds']}с суммарно"
        )
    if "normalized_slides" in deck.meta:
        print(f"  содержание приведено к раскладке: {deck.meta['normalized_slides']} слайдов")

    # Задача L: первая контрольная точка режима — решает, идёт ли rerank
    # раскладок ниже. Держится до следующей точки (`after_compose`).
    mode_after_write = budget.decide_mode("after_write")
    print(f"Режим прогона: {mode_after_write.value} (точка after_write, осталось {budget.remaining():.0f}с)")

    # Task 20: распределение фотографий контент-пакета по слайдам — ПОСЛЕ
    # текста (нужны уже написанные заголовки/содержание, см. `plan.photos.
    # assign_photos`) и ДО `apply_variant`/`build_deck` (`_compatible_kinds`
    # смотрит на `SlideSpec.visual.kind`, проставленный здесь, при выборе
    # финальной раскладки — см. докстроку `plan.variants._compatible_
    # kinds`). Один вызов модели на всю колоду, не на слайд (бриф задачи).
    photos = load_content_pack_photos(args.content_pack)
    photo_llm = _build_role_provider("photo_picker") if photos else None
    deck, photo_report = assign_photos(deck, photos, photo_llm)
    photos_at = time.monotonic()
    if photos:
        # Task 22, отчёт задачи, находка "пайплайн рапортует не то, что в
        # файле": ЭТО число — план планировщика (`assign_photos`, слайд
        # получил `Visual.photo_name`), не факт вставки. У раскладки,
        # которую слайду в итоге даст `apply_variant`, может не найтись
        # слота под картинку — тогда `compose.builder._place_picture_
        # visual` честно не вставляет фотографию (см. её докстроку), и
        # факт расходится с планом. Сколько РЕАЛЬНО легло на слайды —
        # печатается отдельно, ПОСЛЕ сборки каждого варианта ниже (там же,
        # где известен готовый `.pptx`), не здесь.
        print(
            f"Фотографии контент-пакета: {len(photos)} пришло, "
            f"{photo_report.placed_count} распределено планировщиком (план, не факт вставки) "
            f"за {photos_at - written_at:.1f}с"
        )
        for note in photo_report.notes:
            print(f"  ! {note}")
        if photo_report.placed_count == 0:
            print("  ! Ни одна фотография не попала ни на один слайд — см. находки выше.")
    user_photos = {p.name: p.path for p in photos}

    config = AuditConfig.load()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    debug_path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__deck-t13.json"
    debug_path.write_text(json.dumps(deck_spec_to_dict(deck), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Содержание (для отладки) записано в {debug_path}")
    variants = [Variant[v] for v in args.variants] if args.variants else list(Variant)

    rerank_started = time.monotonic()
    preferred_by_variant, rerank_notes = _rerank(deck, profile, variants, budget)
    budget.record("rerank", time.monotonic() - rerank_started)
    if not preferred_by_variant:
        for note in rerank_notes:
            print(f"  ! {note}")
    if preferred_by_variant:
        picked = sum(len(v) for v in preferred_by_variant.values())
        print(f"Раскладки airy/visual уточнены моделью: {picked} слайдов за {time.monotonic() - rerank_started:.1f}с")
        for note in rerank_notes:
            print(f"  ! {note}")

    # Вариант, по которому идёт аудит по картинке: dense, если собирается,
    # иначе первый из заказанных (содержание у вариантов одно).
    visual_variant = Variant.dense if Variant.dense in variants else variants[0]
    visual_target = None
    for variant in variants:
        step_started = time.monotonic()
        variant_deck = apply_variant(deck, profile, variant, preferred=preferred_by_variant.get(variant))
        built_path = build_deck(variant_deck, profile, args.template, variant, user_photos=user_photos)
        built_at = time.monotonic()
        path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__{variant.value}-t13.pptx"
        shutil.copy2(built_path, path)
        findings = run_deterministic(path, profile, config)
        audited_at = time.monotonic()
        budget.record("compose", built_at - step_started)
        budget.record("audit", audited_at - built_at)
        if variant == visual_variant:
            visual_target = (path, variant_deck, findings)

        by_severity: dict[str, int] = {}
        for f in findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_check: dict[str, int] = {}
        for f in findings:
            by_check[f.check_id] = by_check.get(f.check_id, 0) + 1

        print(
            f"\n[{variant.value}] {path.name}: {len(variant_deck.slides)} слайдов, "
            f"сборка {built_at - step_started:.1f}с, аудит {audited_at - built_at:.1f}с"
        )
        print(f"  находки по серьёзности: {by_severity or '(нет)'}")
        print(f"  находки по видам: {dict(sorted(by_check.items()))}")
        slide_findings = [s for v in variant_deck.slides for s in v.findings]
        if slide_findings:
            print(f"  находки сборки (усечения/переполнения): {len(slide_findings)}")
        for f in slide_findings:
            if "раскладка под содержание не найдена" in f:
                print(f"  ! {f}")

        # Task 22: сколько фотографий контент-пакета РЕАЛЬНО легло на
        # слайды ЭТОГО варианта — считано по байтам уже сохранённого
        # `.pptx` (`count_embedded_photos`), не взято из плана
        # `assign_photos` (см. комментарий у печати плана выше). Число не
        # обязано совпадать с планом: у dense/airy/visual разные раскладки
        # на один и тот же слайд, и у части из них может не быть слота под
        # фото, даже если у другого варианта того же слайда — есть.
        if photos:
            embedded = count_embedded_photos(path, user_photos)
            not_embedded_findings = [
                f for f in slide_findings if "фотограф" in f.lower() and "не вставлен" in f.lower()
            ]
            print(
                f"  фотографий физически на слайдах: {embedded} из {photo_report.placed_count} "
                "распределённых планировщиком"
            )
            if embedded < photo_report.placed_count:
                if not_embedded_findings:
                    print("  ! не вставлены (нет слота в выбранной раскладке):")
                    for f in not_embedded_findings:
                        print(f"    - {f}")
                else:
                    print(
                        "  ! расхождение план/факт есть, но причина не найдена среди находок "
                        "сборки — требует разбора."
                    )

    # Вторая контрольная точка — после сборки всех вариантов, перед аудитом
    # по картинке: столько же rerank уже позади, время потрачено, остаток
    # может отличаться от точки after_write (см. докстроку `RunBudget.
    # decide_mode`).
    mode_after_compose = budget.decide_mode("after_compose")
    print(f"Режим прогона: {mode_after_compose.value} (точка after_compose, осталось {budget.remaining():.0f}с)")

    if visual_target is not None:
        _visual_stage(budget, visual_target, visual_variant, sources)

    summary = budget.summary()
    print(f"\nВсего: {time.monotonic() - started:.1f}с (бюджет {budget.deadline_seconds:.0f}с)")
    print(f"  режим: {summary['mode']} (точка {summary['mode_checkpoint']})")
    print(f"  по стадиям: {summary['stage_seconds']}")
    print(
        "\nПолный аудит по картинке всех слайдов (C01-C11) запускается отдельно на "
        "готовом .pptx:\n"
        f"  deckforge audit-visual {args.template} <один из .pptx выше> {debug_path} "
        f"--content-pack {args.content_pack}"
    )
    return 0


def _visual_stage(budget: RunBudget, target, variant, sources) -> None:
    """Аудит по картинке рискованных слайдов одного варианта в рамках
    бюджета прогона (задача H, `workflow.visual_stage`). Превью рендерятся,
    только если стадия реально пойдёт."""
    path, variant_deck, findings = target
    outcome = run_visual_stage(
        budget, variant_deck, findings, _build_vlm(),
        lambda: to_pngs(path, path.parent / f"{path.stem}__risk-preview"), sources=sources,
    )
    if outcome.result is None:
        print(f"\nАудит по картинке рискованных слайдов не выполнялся: {outcome.skipped_reason}")
        return
    slides = ", ".join(
        f"{pos + 1} (техн. {tech:.1f}, смысл. {sem:.1f})" for pos, tech, sem in outcome.picked
    )
    print(
        f"\n[{variant.value}] аудит по картинке рискованных слайдов: {slides}; "
        f"{outcome.result.model_calls} вызовов модели за {outcome.seconds:.1f}с"
    )
    report = AuditReport.merge(findings, outcome.result)
    print(f"  находок модели: {report.visual_count}, всего в отчёте: {len(report.findings)}")
    for f in outcome.findings:
        where = f"слайд {f.slide_index + 1}" if f.slide_index is not None else "колода"
        print(f"  [{f.severity}] {f.check_id} ({where}): {f.message}")


def _cmd_audit_visual(args: argparse.Namespace) -> int:
    """Модельный аудит по картинке (C01-C11, `audit.visual.run_visual`) —
    ОТДЕЛЬНО от `generate` (см. её docstring: ТЗ даёт пять минут на
    генерацию, не на генерацию вместе с модельной проверкой смысла).
    Работает на уже готовом `.pptx` и сохранённом `DeckSpec` (json,
    `deckforge generate` пишет его рядом с каждой колодой,
    `deck_spec_to_dict`) — `run_visual` не зависит от сборки (`compose.
    builder`), поэтому колода не пересобирается и модель для написания
    текста заново не вызывается. Печатает сводный отчёт (`AuditReport.
    merge`) — оба аудита, детерминированный и визуальный, вместе."""
    profile = TemplateProfile.from_file(args.template)
    spec = deck_spec_from_debug_dict(json.loads(args.deck_json.read_text(encoding="utf-8")))

    sources = []
    if args.content_pack is not None:
        _brief, sources, _meta = load_content_pack(args.content_pack)

    config = AuditConfig.load()
    started = time.monotonic()
    det_findings = run_deterministic(args.pptx, profile, config)
    det_at = time.monotonic()
    print(f"Детерминированный аудит: {len(det_findings)} находок за {det_at - started:.1f}с")

    # Рендер PNG-превью нужен только модельному аудиту — без провайдера
    # (`_build_vlm() is None`) `run_visual` всё равно вернёт честный
    # `skipped_reason` и не посмотрит ни на один PNG, так что рендерить их
    # заранее было бы потраченным впустую временем (soffice — не бесплатно,
    # `render.soffice._SOFFICE_TIMEOUT_SECONDS`).
    vlm = _build_vlm()
    pngs: list[Path] = []
    if vlm is not None:
        preview_dir = args.pptx.parent / f"{args.pptx.stem}__audit-preview"
        pngs = to_pngs(args.pptx, preview_dir)
        if len(pngs) != len(spec.slides):
            print(
                f"! Внимание: PNG-превью ({len(pngs)}) и слайдов в {args.deck_json.name} "
                f"({len(spec.slides)}) не совпадают — {args.deck_json.name} собран не из "
                f"{args.pptx.name}?"
            )

    max_workers = args.max_workers if args.max_workers is not None else 4
    vis_result = run_visual(pngs, spec, profile, vlm, sources=sources, max_workers=max_workers)
    if vis_result.skipped_reason:
        print(f"Модельный аудит по картинке: не выполнялся — {vis_result.skipped_reason}")
    else:
        print(
            f"Модельный аудит по картинке: {vis_result.slides_checked} слайдов, "
            f"{vis_result.model_calls} вызовов модели за {vis_result.elapsed_seconds:.1f}с"
        )

    report = AuditReport.merge(det_findings, vis_result)

    # Задача G (PPTEval): средние по content/design печатаются, только если
    # хоть один слайд реально получил оценку — иначе "среднее 0.0" выглядело
    # бы как честный вердикт там, где модель просто не прислала `scores`
    # (см. докстроку `_axis_average` в `audit.visual`).
    if report.content_avg is not None or report.design_avg is not None or report.deck_score:
        content = f"{report.content_avg:.1f}" if report.content_avg is not None else "—"
        design = f"{report.design_avg:.1f}" if report.design_avg is not None else "—"
        coherence = report.deck_score.get("coherence") if report.deck_score else None
        print(
            f"\nОценки PPTEval (1-5): содержание {content}, дизайн {design}, "
            f"связность колоды {coherence if coherence is not None else '—'}"
        )
        deck_why = report.deck_score.get("why") if report.deck_score else None
        if deck_why:
            print(f"  связность: {deck_why}")
        for idx in sorted(report.slide_scores):
            score = report.slide_scores[idx]
            why = score.get("why")
            print(
                f"  слайд {idx}: содержание {score.get('content', '—')}, "
                f"дизайн {score.get('design', '—')}" + (f" — {why}" if why else "")
            )

    print(
        f"\nСводный отчёт: {len(report.findings)} находок "
        f"(детерминированных: {report.deterministic_count}, визуальных: {report.visual_count})"
    )
    print(f"  по серьёзности: {report.by_severity() or '(нет)'}")
    print(f"  по видам: {dict(sorted(report.by_check().items()))}")
    for f in report.findings:
        where = f"слайд {f.slide_index}" if f.slide_index is not None else "колода"
        print(f"  [{f.severity}] {f.check_id} ({where}): {f.message}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deckforge", description="DeckForge — разбор .pptx-шаблонов в дизайн-систему")
    sub = parser.add_subparsers(dest="command", required=True)

    parse_cmd = sub.add_parser("parse", help="Разобрать .pptx-шаблон в TemplateProfile")
    parse_cmd.add_argument("template", type=Path, help="Путь к .pptx-шаблону")
    parse_cmd.add_argument("-o", "--output", type=Path, required=True, help="Куда записать профиль (JSON)")
    parse_cmd.set_defaults(func=_cmd_parse)

    generate_cmd = sub.add_parser("generate", help="Собрать презентацию (все три варианта) по шаблону и контент-пакету")
    generate_cmd.add_argument("template", type=Path, help="Путь к .pptx-шаблону")
    generate_cmd.add_argument("content_pack", type=Path, help="Каталог контент-пакета (brief.md + sources.md)")
    generate_cmd.add_argument("-o", "--output-dir", type=Path, required=True, help="Куда положить собранные .pptx")
    generate_cmd.add_argument("--target-slides", type=int, default=None, help="Целевое число слайдов (иначе — из brief.md)")
    generate_cmd.add_argument(
        "--variant", dest="variants", action="append", choices=[v.value for v in Variant],
        help="Собрать только этот вариант (можно повторять); по умолчанию — все три",
    )
    generate_cmd.add_argument(
        "--writer-max-workers", type=int, default=None,
        help=(
            "Число слайдов, чей текст пишется одновременно (по умолчанию — "
            "config/app.yaml, llm.slide_writer_max_workers); значение 1 — для "
            "замера 'до' против параллельной записи"
        ),
    )
    generate_cmd.set_defaults(func=_cmd_generate)

    audit_cmd = sub.add_parser(
        "audit-visual",
        help="Прогнать модельный аудит по картинке (C01-C11) на готовом .pptx вместе с детерминированным аудитом",
    )
    audit_cmd.add_argument("template", type=Path, help="Путь к .pptx-шаблону (нужен для детерминированного аудита)")
    audit_cmd.add_argument("pptx", type=Path, help="Готовый собранный .pptx (например, из deckforge generate)")
    audit_cmd.add_argument(
        "deck_json", type=Path,
        help="DeckSpec в JSON, сохранённый deckforge generate (<шаблон>__<пакет>__deck-t13.json)",
    )
    audit_cmd.add_argument(
        "--content-pack", type=Path, default=None,
        help="Каталог контент-пакета — для сверки цифр с исходниками (C04); необязателен",
    )
    audit_cmd.add_argument(
        "--max-workers", type=int, default=None,
        help="Число параллельных вызовов модели на слайд (по умолчанию — 4, как у run_visual)",
    )
    audit_cmd.set_defaults(func=_cmd_audit_visual)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
