"""Прогон-эталон (задача V4): шаблоны × наборы содержания × стили тем же
путём, что `deckforge generate`, и одна таблица чисел на прогон.

Девятьсот с лишним тестов отвечают, не сломалось ли что-то, но не отвечают,
стали ли презентации лучше. Эталон снимает с каждой готовой колоды одни и те
же числа (время, режим, клоны и запасные ступени, находки аудита по
серьёзности, оценки по картинке, верность шаблону, энтропию раскладок,
повторы фактов, редактируемость, вызовы модели и ожидание очереди) и пишет
их в markdown для глаз и в JSON для сравнения двух версий (`--compare`).

`--offline` гонит тот же путь без модели: все роли идут запасными путями.
Так эталон проверяется тестом и даёт нижнюю планку, которую модель обязана
превзойти."""
from __future__ import annotations
import argparse
import json
import re
import time
from pathlib import Path

# Колонки таблицы: ключ строки -> заголовок. Порядок тот же в markdown и в
# сравнении.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("seconds", "время, с"),
    ("mode", "режим"),
    ("slides", "слайдов"),
    ("clones", "клонов"),
    ("fallback_rungs", "запасных ступеней"),
    ("findings_critical", "находок critical"),
    ("findings_major", "находок major"),
    ("findings_minor", "находок minor"),
    ("visual_content_avg", "по картинке: содержание"),
    ("visual_design_avg", "по картинке: дизайн"),
    ("native_clone_rate", "доля клонов"),
    ("typography_palette_compliance", "типографика/палитра"),
    ("pattern_entropy", "энтропия раскладок"),
    ("repeated_facts", "повторов фактов"),
    ("editable_content_coverage", "редактируемость"),
    ("model_calls", "вызовов модели"),
    ("queue_wait_seconds", "ожидание очереди, с"),
)

_KEY_FIELDS = ("template", "pack", "style")

# Число как факт: не меньше двух знаков или с дробной частью, чтобы номера
# шагов «1», «2» не считались повтором. Пробелы-разделители тысяч склеиваются.
_NUMBER = re.compile(r"\d[\d\u00a0 ]*(?:[.,]\d+)?")
_YEAR = re.compile(r"(?:19|20)\d\d")


def _texts(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [t for key, v in node.items() if key not in ("pattern_id", "index", "kind") for t in _texts(v)]
    if isinstance(node, list):
        return [t for v in node for t in _texts(v)]
    return []


def count_repeated_facts(deck: dict) -> int:
    """Сколько чисел-фактов стоит больше чем на одном слайде. Грубо, зато
    детерминированно и без модели: писатель обязан не повторять факт на
    двух слайдах (`agents/slide-writer/AGENT.md`), и рост этого числа между
    версиями видно сразу."""
    seen: dict[str, set[int]] = {}
    for position, slide in enumerate(deck.get("slides", [])):
        for text in _texts(slide):
            for match in _NUMBER.finditer(text):
                token = re.sub(r"[\u00a0 ]", "", match.group()).replace(",", ".")
                if len(token.replace(".", "")) < 2 and "." not in token:
                    continue
                if _YEAR.fullmatch(token):
                    continue  # год повторяется законно: «план 2027» на трёх слайдах не повтор факта
                seen.setdefault(token, set()).add(position)
    return sum(1 for slides in seen.values() if len(slides) > 1)


def _row(template: Path, pack: Path, style: str, metrics: dict | None, error: str | None) -> dict:
    row: dict = {"template": template.name, "pack": pack.name, "style": style}
    if error is not None or metrics is None:
        row["error"] = error or "нет чисел по стилю"
        return row
    budget = metrics.get("budget", {})
    severity = metrics.get("findings_by_severity", {})
    row.update({
        "seconds": budget.get("elapsed_seconds"),
        "mode": budget.get("mode"),
        "slides": metrics.get("slides"),
        "clones": metrics.get("clones"),
        "fallback_rungs": metrics.get("fallback_rungs"),
        "findings_critical": severity.get("critical", 0),
        "findings_major": severity.get("major", 0),
        "findings_minor": severity.get("minor", 0),
        "visual_content_avg": metrics.get("visual_content_avg"),
        "visual_design_avg": metrics.get("visual_design_avg"),
        "native_clone_rate": metrics.get("native_clone_rate"),
        "typography_palette_compliance": metrics.get("typography_palette_compliance"),
        "pattern_entropy": metrics.get("pattern_entropy"),
        "repeated_facts": metrics.get("repeated_facts"),
        "editable_content_coverage": metrics.get("editable_content_coverage"),
        "model_calls": budget.get("model_calls"),
        "queue_wait_seconds": budget.get("queue_wait_seconds"),
        "ladder": metrics.get("ladder", {}),
        "editable_counts": metrics.get("editable_counts", {}),
        "queue_wait_by_class": budget.get("queue_wait_by_class", {}),
    })
    return row


def run_bench(
    templates: list[Path], packs: list[Path], styles: list[str], out_dir: Path, *, offline: bool = False,
) -> list[dict]:
    """Одна строка на тройку (шаблон, набор, стиль). Стили одной пары
    шаблон × набор идут одним `generate`, как у человека: общая структура,
    свои бюджеты. Упавший прогон даёт строку с `error`, а не роняет эталон."""
    from deckforge import cli

    rows: list[dict] = []
    previous = cli._OFFLINE["on"]
    cli._OFFLINE["on"] = offline
    try:
        for template in templates:
            for pack in packs:
                metrics: dict = {}
                run_dir = out_dir / f"{template.stem}__{pack.name}"
                args = argparse.Namespace(
                    template=template, content_pack=pack, output_dir=run_dir, target_slides=None,
                    styles=list(styles) or None, writer_max_workers=None, metrics=metrics,
                )
                error = None
                try:
                    cli._cmd_generate(args)
                except Exception as exc:  # noqa: BLE001: один прогон не должен ронять эталон
                    error = f"{type(exc).__name__}: {exc}"
                for style in styles or list(metrics):
                    rows.append(_row(template, pack, style, metrics.get(style), error))
    finally:
        cli._OFFLINE["on"] = previous
    return rows


def _fmt(value) -> str:
    if value is None:
        return "н/д"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def to_markdown(rows: list[dict]) -> str:
    head = ["шаблон", "набор", "стиль"] + [title for _k, title in COLUMNS]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        cells = [row["template"], row["pack"], row["style"]]
        if "error" in row:
            cells += [f"ошибка: {row['error']}"] + [""] * (len(COLUMNS) - 1)
        else:
            cells += [_fmt(row.get(key)) for key, _t in COLUMNS]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return "\n".join(lines) + "\n"


def compare(a: dict, b: dict) -> str:
    """Дельты `b - a` по числовым колонкам для общих троек. Строка, которой
    нет в одном из отчётов, помечается, а не выпадает молча."""
    def index(report: dict) -> dict[tuple, dict]:
        return {tuple(r[k] for k in _KEY_FIELDS): r for r in report.get("rows", [])}

    left, right = index(a), index(b)
    head = ["шаблон", "набор", "стиль"] + [title for _k, title in COLUMNS]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for key in sorted(set(left) | set(right)):
        cells = list(key)
        if key not in left or key not in right:
            cells += ["только в " + ("новом" if key not in left else "старом")] + [""] * (len(COLUMNS) - 1)
        else:
            for column, _t in COLUMNS:
                old, new = left[key].get(column), right[key].get(column)
                if isinstance(old, (int, float)) and isinstance(new, (int, float)) and not isinstance(old, bool):
                    delta = new - old
                    cells.append(f"{delta:+.2f}" if isinstance(delta, float) else f"{delta:+d}")
                elif old == new:
                    cells.append("=")
                else:
                    cells.append(f"{_fmt(old)} → {_fmt(new)}")
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


def write_report(rows: list[dict], out_dir: Path, *, offline: bool) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    payload = {"created": stamp, "offline": offline, "columns": [k for k, _t in COLUMNS], "rows": rows}
    json_path = out_dir / "bench.json"
    md_path = out_dir / "bench.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(rows), encoding="utf-8")
    return md_path, json_path
