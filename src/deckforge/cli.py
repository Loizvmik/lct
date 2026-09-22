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
from deckforge.compose.builder import build_deck
from deckforge.plan.outline import build_outline, load_content_pack
from deckforge.plan.spec import deck_spec_from_debug_dict, deck_spec_to_dict
from deckforge.plan.variants import Variant, apply_variant
from deckforge.plan.writer import DEFAULT_WRITER_MAX_WORKERS, write_slides
from deckforge.provider.base import LLMProvider
from deckforge.provider.registry import ModelNotAllowed
from deckforge.provider.yandex import YandexProvider
from deckforge.render.soffice import to_pngs
from deckforge.settings import Settings
from deckforge.template.profile import TemplateProfile

APP_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "app.yaml"


def _build_role_provider(role: str) -> LLMProvider | None:
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
            deadline_seconds=settings.llm.deadline_seconds,
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


def _cmd_parse(args: argparse.Namespace) -> int:
    namer = _build_namer()
    started = time.monotonic()
    profile = TemplateProfile.from_file(args.template, namer=namer)
    elapsed = time.monotonic() - started

    args.output.write_text(profile.to_json(), encoding="utf-8")

    print(f"{args.template.name}: разобрано за {elapsed:.3f}с, профиль записан в {args.output}")
    print(f"Лейаутов: {len(profile.layouts)}, паттернов: {len(profile.patterns)}")
    print(f"Роли палитры: {', '.join(f'{role}={hexv}' for role, hexv in sorted(profile.palette_roles.items())) or '(нет)'}")

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

    НЕ зовёт модельный аудит по картинке (C01-C11, `audit.visual.
    run_visual`) — ТЗ отводит пять минут на ГЕНЕРАЦИЮ колоды, а не на
    генерацию вместе с модельной проверкой смысла (живой замер задачи,
    `task-12-report.md`: 302.8с генерация + 152.8с аудит по картинке =
    455.6с, почти вдвое дольше бюджета). Аудит по картинке — отдельный шаг,
    `deckforge audit-visual`, который человек запускает на уже готовом
    файле (см. её docstring); эта функция честно печатает, что он не
    выполнялся и как его запустить — молчаливая тишина хуже отсутствия
    (то же правило, что и `VisualAuditResult.skipped_reason`)."""
    namer = _build_namer()
    outline_llm = _build_role_provider("outline")
    writer_llm = _build_role_provider("writer")

    started = time.monotonic()
    profile = TemplateProfile.from_file(args.template, namer=namer)
    parsed_at = time.monotonic()
    print(f"{args.template.name}: разобран за {parsed_at - started:.1f}с, паттернов: {len(profile.patterns)}")

    brief, sources, meta = load_content_pack(args.content_pack)
    target_slides = args.target_slides or meta.get("target_slides")
    outline = build_outline(
        brief, sources, profile, outline_llm, target_slides,
        title=meta.get("title", args.content_pack.name), language=meta.get("language", "ru"),
    )
    outlined_at = time.monotonic()
    print(f"Структура: {len(outline.slides)} слайдов за {outlined_at - parsed_at:.1f}с")

    # Текст слайдов пишется ПАРАЛЛЕЛЬНО (max_workers — config/app.yaml,
    # `llm.slide_writer_max_workers`) — слайды друг от друга не зависят, а
    # последовательное написание было девяноста процентами времени всей
    # генерации (живой замер, docstring `write_slides`/`task-12-report.md`).
    writer_max_workers = args.writer_max_workers if args.writer_max_workers is not None else _writer_max_workers()
    deck = write_slides(outline, sources, profile, writer_llm, max_workers=writer_max_workers)
    written_at = time.monotonic()
    print(f"Текст слайдов написан за {written_at - outlined_at:.1f}с")

    config = AuditConfig.load()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    debug_path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__deck-t13.json"
    debug_path.write_text(json.dumps(deck_spec_to_dict(deck), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Содержание (для отладки) записано в {debug_path}")
    variants = [Variant[v] for v in args.variants] if args.variants else list(Variant)

    for variant in variants:
        step_started = time.monotonic()
        variant_deck = apply_variant(deck, profile, variant)
        built_path = build_deck(variant_deck, profile, args.template, variant)
        built_at = time.monotonic()
        path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__{variant.value}-t13.pptx"
        shutil.copy2(built_path, path)
        findings = run_deterministic(path, profile, config)
        audited_at = time.monotonic()

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

    print(f"\nВсего: {time.monotonic() - started:.1f}с")
    print(
        "\nМодельный аудит по картинке (C01-C11) НЕ выполнялся — вынесен из генерации "
        "(ТЗ отводит 5 минут на генерацию колоды, не на генерацию вместе с модельной "
        "проверкой смысла; см. .superpowers/sdd/task-12-report.md). Запустите его отдельно "
        "на готовом .pptx из этого прогона:\n"
        f"  deckforge audit-visual {args.template} <один из .pptx выше> {debug_path} "
        f"--content-pack {args.content_pack}"
    )
    return 0


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
