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
import sys
import time
from pathlib import Path

from deckforge.provider.base import LLMProvider
from deckforge.provider.registry import ModelNotAllowed
from deckforge.provider.yandex import YandexProvider
from deckforge.settings import Settings
from deckforge.template.profile import TemplateProfile

APP_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "app.yaml"


def _build_namer() -> LLMProvider | None:
    """Провайдер для роли `palette_namer`, либо `None`, если секретов нет
    или их не хватает для клиента — в обоих случаях `parse` обязан
    продолжить работу с запасным вариантом именования, не падать."""
    try:
        settings = Settings.load(APP_YAML_PATH)
    except Exception:
        return None
    if not settings.yandex_api_key or not settings.yandex_folder_id:
        return None
    try:
        return YandexProvider(
            model=settings.llm.model_for("palette_namer"),
            api_key=settings.yandex_api_key,
            folder_id=settings.yandex_folder_id,
            deadline_seconds=settings.llm.deadline_seconds,
        )
    except (ValueError, ModelNotAllowed):
        return None


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deckforge", description="DeckForge — разбор .pptx-шаблонов в дизайн-систему")
    sub = parser.add_subparsers(dest="command", required=True)

    parse_cmd = sub.add_parser("parse", help="Разобрать .pptx-шаблон в TemplateProfile")
    parse_cmd.add_argument("template", type=Path, help="Путь к .pptx-шаблону")
    parse_cmd.add_argument("-o", "--output", type=Path, required=True, help="Куда записать профиль (JSON)")
    parse_cmd.set_defaults(func=_cmd_parse)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
