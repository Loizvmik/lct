"""Прогон-эталон `deckforge bench` (задача V4): офлайн, без модели, на одном
шаблоне, одном наборе содержания и одном стиле."""
from __future__ import annotations
import json
from pathlib import Path

import deckforge.cli as cli_module
from deckforge.bench import COLUMNS, compare, count_repeated_facts
from deckforge.cli import main

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")
CONTENT_PACK = Path("fixtures/content-packs/queue-latency")


def test_offline_bench_writes_markdown_and_json_without_the_model(monkeypatch, tmp_path, capsys):
    def _no_model(*_args, **_kwargs):
        raise AssertionError("в режиме --offline модель не строится")

    # Ключ мог бы быть в окружении: офлайн обязан обойтись без провайдера.
    monkeypatch.setenv("YANDEX_API_KEY", "fake")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "fake")
    monkeypatch.setattr(cli_module, "YandexProvider", _no_model)
    out_dir = tmp_path / "bench"

    code = main([
        "bench", str(TEMPLATE), "--pack", str(CONTENT_PACK), "--style", "dense", "--offline", "-o", str(out_dir),
    ])

    assert code == 0, capsys.readouterr().out
    report = json.loads((out_dir / "bench.json").read_text(encoding="utf-8"))
    assert report["offline"] is True
    [row] = report["rows"]
    assert (row["template"], row["pack"], row["style"]) == (TEMPLATE.name, CONTENT_PACK.name, "dense")
    assert "error" not in row
    for key, _title in COLUMNS:
        assert key in row, key
    assert row["model_calls"] == 0
    assert row["slides"] > 0
    assert row["clones"] + row["fallback_rungs"] == row["slides"]
    assert 0.0 <= row["editable_content_coverage"] <= 1.0
    assert row["mode"] in ("full", "fast", "emergency")
    markdown = (out_dir / "bench.md").read_text(encoding="utf-8")
    assert "редактируемость" in markdown and "VK Tech" in markdown
    assert cli_module._OFFLINE["on"] is False, "флаг офлайна снимается после прогона"

    # Сравнение отчёта с самим собой: все дельты нулевые.
    capsys.readouterr()
    assert main(["bench", "--compare", str(out_dir / "bench.json"), str(out_dir / "bench.json")]) == 0
    assert "+0" in capsys.readouterr().out


def test_compare_reports_deltas_and_rows_missing_on_one_side():
    base = {"template": "t.pptx", "pack": "p", "style": "dense"}
    a = {"rows": [{**base, "seconds": 100.0, "clones": 5, "mode": "full"}]}
    b = {"rows": [
        {**base, "seconds": 90.5, "clones": 7, "mode": "fast"},
        {**base, "style": "airy", "seconds": 80.0},
    ]}

    table = compare(a, b)

    assert "-9.50" in table and "+2" in table
    assert "full → fast" in table
    assert "только в новом" in table


def test_repeated_facts_count_numbers_on_several_slides_but_not_years_or_step_numbers():
    deck = {"slides": [
        {"headline": "Пилот 2026", "blocks": [{"text": "Срок упал на 42% за 3 недели"}]},
        {"headline": "Итоги 2026", "blocks": [{"text": "Минус 42 % к сроку, бюджет 2,4 млн"}]},
        {"headline": "Бюджет", "blocks": [{"text": "Нужно 2,4 млн ₽, шаг 3"}]},
    ]}

    assert count_repeated_facts(deck) == 2  # «42» и «2.4»; 2026 и 3 не в счёт
