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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import run_deterministic
from deckforge.audit.fidelity import template_fidelity
from deckforge.audit.report import AuditReport
from deckforge.audit.visual import run_visual
from deckforge.compose.builder import build_deck, count_embedded_photos
from deckforge.pattern.intent import intents_from_outline
from deckforge.plan.contracts import plan_contracts
from deckforge.plan.outline import build_outline, load_content_pack, outline_to_dict
from deckforge.plan.photos import assign_photos_to_outline, load_content_pack_photos
from deckforge.plan.spec import deck_spec_from_debug_dict, deck_spec_to_dict
from deckforge.plan.variants import Variant
from deckforge.plan.writer import AGENT_MAX_STEPS_DEFAULT, DEFAULT_WRITER_MAX_WORKERS, write_slides
from deckforge.provider.base import LLMProvider
from deckforge.provider.registry import ModelNotAllowed
from deckforge.provider.yandex import YandexProvider
from deckforge.render.soffice import to_pngs
from deckforge.settings import Settings
from deckforge.template.profile import TemplateProfile
from deckforge.workflow.budget import RunBudget, load_policy
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
    """Весь путь от брифа до готовой презентации: разбор шаблона ->
    структура -> фото по пунктам структуры -> для каждого стиля свои
    раскладки (`pattern.plan_patterns`), контракты и текст под них ->
    сборка -> детерминированный аудит. Без ключа модели (`.env`) продолжает
    работать запасными вариантами на каждом шаге, результат хуже по
    содержанию, но пайплайн не падает.

    Задача P поменяла порядок решений: раньше текст писался один раз на три
    стиля, а стиль потом подбирал раскладку под готовый текст. Теперь общие
    только разбор и структура; раскладки и текст у каждого стиля свои,
    текст пишется под свою композицию.

    Полный аудит по картинке всех слайдов (C01-C11) сюда не входит, только
    вопросы по нескольким рискованным слайдам одного варианта, если бюджет
    позволяет (`workflow.visual_stage`). Полный аудит остаётся командой
    `deckforge audit-visual` на готовом файле."""
    # Бюджет прогона создаётся первым делом: пять минут ТЗ считаются от
    # начала команды.
    budget = RunBudget.from_policy(load_policy(APP_YAML_PATH))
    namer = _build_namer()
    vision = _build_pattern_kind_vlm()
    outline_llm = _build_role_provider("outline")

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

    # Фотографии контент-пакета распределяются по пунктам структуры ДО
    # планирования раскладок: слайду с фото планировщик обязан дать место
    # под картинку. Один вызов модели на колоду, общий для всех стилей.
    photos = load_content_pack_photos(args.content_pack)
    photo_llm = _build_role_provider("photo_picker") if photos else None
    photo_by_slide, photo_report = assign_photos_to_outline(outline, photos, photo_llm)
    photos_at = time.monotonic()
    if photos:
        # Это план планировщика, не факт вставки: сколько реально легло на
        # слайды, печатается после сборки каждого варианта.
        print(
            f"Фотографии контент-пакета: {len(photos)} пришло, "
            f"{photo_report.placed_count} распределено планировщиком (план, не факт вставки) "
            f"за {photos_at - outlined_at:.1f}с"
        )
        for note in photo_report.notes:
            print(f"  ! {note}")
        if photo_report.placed_count == 0:
            print("  ! Ни одна фотография не попала ни на один слайд — см. находки выше.")
    user_photos = {p.name: p.path for p in photos}
    budget.record("photos", photos_at - outlined_at)
    intents = intents_from_outline(outline, photo_by_slide)

    config = AuditConfig.load()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outline_path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__outline.json"
    outline_path.write_text(json.dumps(outline_to_dict(outline), ensure_ascii=False, indent=2), encoding="utf-8")
    variants = [Variant[v] for v in args.variants] if args.variants else list(Variant)
    writer_max_workers = args.writer_max_workers if args.writer_max_workers is not None else _writer_max_workers()

    # Общие стадии позади. Дальше стили идут параллельно, каждый целиком
    # (раскладки, текст, сборка, аудит) в своём бюджете: лимит ТЗ считается
    # на одну презентацию. Бюджеты заводятся все сразу, чтобы дедлайн был
    # один. Печать каждого стиля копится и выводится целиком.
    budgets = {variant: budget.for_variant(variant.value) for variant in variants}
    # Вариант, по которому идёт аудит по картинке: dense, если собирается,
    # иначе первый из заказанных.
    visual_variant = Variant.dense if Variant.dense in variants else variants[0]
    ctx = dict(
        outline=outline, intents=intents, profile=profile, args=args, config=config, user_photos=user_photos,
        photos=photos, photo_report=photo_report, sources=sources, visual_variant=visual_variant,
        writer_max_workers=writer_max_workers,
    )
    with ThreadPoolExecutor(max_workers=len(variants)) as pool:
        futures = [pool.submit(_generate_variant, variant, budgets[variant], ctx) for variant in variants]
        outputs = [future.result() for future in futures]
    for lines in outputs:
        print("\n".join(lines))

    # Содержание варианта, по которому идёт аудит по картинке, ещё и под
    # общим именем: `deckforge audit-visual` берёт .pptx и его план.
    debug_path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__deck-t13.json"
    variant_debug = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__{visual_variant.value}-deck.json"
    if variant_debug.exists():
        shutil.copy2(variant_debug, debug_path)
        print(f"\nСодержание (для отладки) записано в {debug_path}")

    summary = budget.summary()
    print(f"\nВсего: {time.monotonic() - started:.1f}с (бюджет {budget.deadline_seconds:.0f}с на презентацию)")
    print(f"  общие стадии: {summary['stage_seconds']}")
    for name, part in summary.get("variants", {}).items():
        print(
            f"  [{name}] {part['elapsed_seconds']:.1f}с после общих стадий, режим {part['mode']}, "
            f"по стадиям: {part['stage_seconds']}"
        )
    print(
        "\nПолный аудит по картинке всех слайдов (C01-C11) запускается отдельно на "
        "готовом .pptx:\n"
        f"  deckforge audit-visual {args.template} <один из .pptx выше> {debug_path} "
        f"--content-pack {args.content_pack}"
    )
    return 0


def _visual_stage(budget: RunBudget, target, variant, sources, out: list[str]) -> None:
    """Аудит по картинке рискованных слайдов одного варианта в рамках
    его бюджета (задача H, `workflow.visual_stage`). Превью рендерятся,
    только если стадия реально пойдёт. Печать копится в `out`."""
    path, variant_deck, findings = target
    outcome = run_visual_stage(
        budget, variant_deck, findings, _build_vlm(),
        lambda: to_pngs(path, path.parent / f"{path.stem}__risk-preview"), sources=sources,
        pptx_path=path,
    )
    if outcome.result is None:
        out.append(f"\nАудит по картинке рискованных слайдов не выполнялся: {outcome.skipped_reason}")
        return
    slides = ", ".join(
        f"{pos + 1} (техн. {tech:.1f}, смысл. {sem:.1f})" for pos, tech, sem in outcome.picked
    )
    out.append(
        f"\n[{variant.value}] аудит по картинке рискованных слайдов: {slides}; "
        f"{outcome.result.model_calls} вызовов модели за {outcome.seconds:.1f}с"
    )
    report = AuditReport.merge(findings, outcome.result)
    out.append(f"  находок модели: {report.visual_count}, всего в отчёте: {len(report.findings)}")
    for f in outcome.findings:
        where = f"слайд {f.slide_index + 1}" if f.slide_index is not None else "колода"
        out.append(f"  [{f.severity}] {f.check_id} ({where}): {f.message}")


def _generate_variant(variant: Variant, budget: RunBudget, ctx: dict) -> list[str]:
    """Всё после структуры для одного стиля, в его бюджете: раскладки на
    всю колоду, контракты, текст под них, сборка, аудит, аудит по картинке.
    Возвращает строки отчёта, печатает их вызывающий код целиком."""
    profile, args = ctx["profile"], ctx["args"]
    photos, photo_report = ctx["photos"], ctx["photo_report"]
    out: list[str] = []

    step_started = time.monotonic()
    assignments, contracts = plan_contracts(ctx["intents"], profile, variant)
    planned_at = time.monotonic()
    budget.record("plan", planned_at - step_started)
    unique = len({a.pattern_id for a in assignments if a.pattern_id})
    out.append(
        f"\n[{variant.value}] раскладки назначены за {planned_at - step_started:.2f}с: "
        f"{unique} разных на {len(assignments)} слайдов"
    )

    deck = write_slides(
        ctx["outline"], contracts, ctx["sources"], profile, _build_role_provider("writer"),
        max_workers=ctx["writer_max_workers"], agent_max_steps=_writer_agent_max_steps(), style=variant,
    )
    written_at = time.monotonic()
    budget.record("write", written_at - planned_at)
    meta = deck.meta
    out.append(
        f"  текст под контракт за {written_at - planned_at:.1f}с: мест в пределах контракта "
        f"{meta.get('contract_places_ok', '0')} из {meta.get('contract_places', '0')}, "
        f"ремонтов {meta.get('contract_repairs_accepted', '0')} принято из {meta.get('contract_repairs', '0')}"
    )
    if "normalized_slides" in meta:
        out.append(f"  содержание приведено к раскладке: {meta['normalized_slides']} слайдов")

    mode = budget.decide_mode("after_write")
    out.append(f"  режим: {mode.value} (точка after_write, осталось {budget.remaining():.0f}с)")

    step_started = time.monotonic()
    built_path = build_deck(deck, profile, args.template, variant, user_photos=ctx["user_photos"])
    built_at = time.monotonic()
    path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__{variant.value}-t13.pptx"
    shutil.copy2(built_path, path)
    variant_debug = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__{variant.value}-deck.json"
    variant_debug.write_text(json.dumps(deck_spec_to_dict(deck), ensure_ascii=False, indent=2), encoding="utf-8")
    findings = run_deterministic(path, profile, ctx["config"])
    audited_at = time.monotonic()
    budget.record("compose", built_at - step_started)
    budget.record("audit", audited_at - built_at)

    by_severity: dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    by_check: dict[str, int] = {}
    for f in findings:
        by_check[f.check_id] = by_check.get(f.check_id, 0) + 1

    out.append(
        f"[{variant.value}] {path.name}: {len(deck.slides)} слайдов, "
        f"сборка {built_at - step_started:.1f}с, аудит {audited_at - built_at:.1f}с"
    )
    out.append(f"  раскладки: {', '.join(s.pattern_id or '-' for s in deck.slides)}")
    # Сводка верности шаблону на вариант; печать, не решение пайплайна, и
    # она не должна ронять генерацию.
    try:
        out.append(f"  {template_fidelity(path, deck, profile).summary}")
    except Exception as exc:  # noqa: BLE001 — сводка необязательна
        out.append(f"  ! Верность шаблону не посчитана: {exc}")
    out.append(f"  находки по серьёзности: {by_severity or '(нет)'}")
    out.append(f"  находки по видам: {dict(sorted(by_check.items()))}")
    slide_findings = [s for v in deck.slides for s in v.findings]
    if slide_findings:
        out.append(f"  находки сборки (усечения/переполнения): {len(slide_findings)}")
    for f in slide_findings:
        if "раскладка под содержание не найдена" in f:
            out.append(f"  ! {f}")

    # Сколько фотографий контент-пакета РЕАЛЬНО легло на слайды этого
    # варианта: по байтам сохранённого .pptx, не по плану распределения.
    if photos:
        embedded = count_embedded_photos(path, ctx["user_photos"])
        not_embedded_findings = [
            f for f in slide_findings if "фотограф" in f.lower() and "не вставлен" in f.lower()
        ]
        out.append(
            f"  фотографий физически на слайдах: {embedded} из {photo_report.placed_count} "
            "распределённых планировщиком"
        )
        if embedded < photo_report.placed_count:
            if not_embedded_findings:
                out.append("  ! не вставлены (нет слота в выбранной раскладке):")
                for f in not_embedded_findings:
                    out.append(f"    - {f}")
            else:
                out.append(
                    "  ! расхождение план/факт есть, но причина не найдена среди находок "
                    "сборки — требует разбора."
                )

    # Вторая контрольная точка варианта — после сборки, перед аудитом по
    # картинке (см. докстроку `RunBudget.decide_mode`).
    mode_after_compose = budget.decide_mode("after_compose")
    out.append(
        f"  режим: {mode_after_compose.value} (точка after_compose, осталось {budget.remaining():.0f}с)"
    )
    if variant == ctx["visual_variant"]:
        _visual_stage(budget, (path, deck, findings), variant, ctx["sources"], out)
    budget.stop()
    return out


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
    vis_result = run_visual(
        pngs, spec, profile, vlm, sources=sources, max_workers=max_workers, pptx_path=args.pptx,
    )
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


def _cmd_fidelity(args: argparse.Namespace) -> int:
    """Задача T: метрики верности шаблону (`audit.fidelity.template_
    fidelity`) на уже готовом `.pptx` и сохранённом `DeckSpec` (json,
    тот же файл, что и `audit-visual` читает — `deckforge generate` пишет
    его рядом с каждой колодой). Ничего не пересобирает и модель не
    зовёт — тот же принцип, что и у `audit-visual`."""
    profile = TemplateProfile.from_file(args.template)
    spec = deck_spec_from_debug_dict(json.loads(args.deck_json.read_text(encoding="utf-8")))

    report = template_fidelity(args.pptx, spec, profile)

    print(f"{args.pptx.name}: {report.summary}")
    print(f"  доля слайдов клоном: {report.native_clone_rate:.0%}")
    print(f"  сохранность фигур примера: {_fmt_metric(report.native_shape_preservation)}")
    print(f"  типографика/палитра без находок: {_fmt_metric(report.typography_palette_compliance)}")
    print(
        "  среднее отклонение геометрии: "
        + (f"{report.mean_geometry_deviation:.1%}" if report.mean_geometry_deviation is not None else "н/д")
    )
    print(f"  разнообразие раскладок: {_fmt_metric(report.pattern_diversity)}")
    print(
        "  энтропия распределения раскладок: "
        + (f"{report.pattern_entropy:.2f}" if report.pattern_entropy is not None else "н/д")
    )
    print(f"  использование родных ассетов: {_fmt_metric(report.native_asset_usage)}")
    print(
        "  дельта плотности к паттерну: "
        + (f"{report.density_delta:.1%}" if report.density_delta is not None else "н/д")
    )
    if report.notes:
        print("\nПримечания:")
        for note in report.notes:
            print(f"  ! {note}")

    return 0


def _fmt_metric(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "н/д"


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

    fidelity_cmd = sub.add_parser(
        "fidelity",
        help="Метрики верности шаблону (Template Fidelity) на уже готовом .pptx",
    )
    fidelity_cmd.add_argument("template", type=Path, help="Путь к .pptx-шаблону")
    fidelity_cmd.add_argument("pptx", type=Path, help="Готовый собранный .pptx (например, из deckforge generate)")
    fidelity_cmd.add_argument(
        "deck_json", type=Path,
        help="DeckSpec в JSON, сохранённый deckforge generate (<шаблон>__<пакет>__deck-t13.json)",
    )
    fidelity_cmd.set_defaults(func=_cmd_fidelity)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
