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
import dataclasses
import json
import shutil
import sys
import time
from pathlib import Path

from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import run_deterministic
from deckforge.compose.builder import build_deck
from deckforge.plan.outline import build_outline, load_content_pack
from deckforge.plan.variants import Variant, apply_variant
from deckforge.plan.writer import write_slides
from deckforge.provider.base import LLMProvider
from deckforge.provider.registry import ModelNotAllowed
from deckforge.provider.yandex import YandexProvider
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


def _spec_to_json(obj):
    """Датаклассы `plan.spec` в JSON-совместимую форму — только для отладки
    (`generate` дампит написанное `DeckSpec` рядом с собранными `.pptx`,
    чтобы был виден текст, который реально ушёл в сборку каждого варианта,
    без повторного дорогого вызова модели)."""
    if dataclasses.is_dataclass(obj):
        return {f.name: _spec_to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_spec_to_json(v) for v in obj]
    return obj


def _cmd_generate(args: argparse.Namespace) -> int:
    """Весь путь от брифа до готовой презентации (Task 13, "эта задача
    замыкает пайплайн"): разбор шаблона -> структура -> текст слайдов ->
    три варианта вёрстки -> сборка -> детерминированный аудит каждого
    варианта. Без ключа модели (`.env`) продолжает работать запасными
    вариантами на каждом шаге (`build_outline`/`write_slides` без `llm`) —
    результат хуже по содержанию, но пайплайн не падает."""
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

    deck = write_slides(outline, sources, profile, writer_llm)
    written_at = time.monotonic()
    print(f"Текст слайдов написан за {written_at - outlined_at:.1f}с")

    config = AuditConfig.load()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    debug_path = args.output_dir / f"{args.template.stem}__{args.content_pack.name}__deck-t13.json"
    debug_path.write_text(json.dumps(_spec_to_json(deck), ensure_ascii=False, indent=2), encoding="utf-8")
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
    generate_cmd.set_defaults(func=_cmd_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
