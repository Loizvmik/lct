"""Тесты `deckforge generate`/`deckforge audit-visual` (эта задача: аудит по
картинке выносится из бюджета генерации ТЗ — «5 минут», см. `.superpowers/
sdd/task-12-report.md`).

Оба сценария офлайн (без `YANDEX_API_KEY`) — быстрые, без сети/soffice-
рендера картинок (аудит по картинке без модели честно пропускает и рендер:
незачем рендерить превью, которые некому смотреть)."""
from __future__ import annotations
import json
from pathlib import Path

import deckforge.cli as cli_module
from deckforge.cli import main

TEMPLATE = Path("dataset/templates/VK Tech шаблон.pptx")
CONTENT_PACK = Path("fixtures/content-packs/queue-latency")


def test_generate_passes_writer_max_workers_override_to_write_slides(monkeypatch, tmp_path):
    """`--writer-max-workers` — нужен для честного замера "до/после"
    (задача: сравнить параллельную запись текста слайдов с последовательной
    на ОДНОМ и том же коде, не угадывать по старому коммиту)."""
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    captured = {}
    real_write_slides = cli_module.write_slides

    def _spy(outline, contracts, sources, profile, llm, *, max_workers, **rest):
        # `**rest` — чтобы шпион не ломался от новых именованных аргументов
        # `write_slides`: тест проверяет проброс одного параметра.
        captured["max_workers"] = max_workers
        return real_write_slides(outline, contracts, sources, profile, llm, max_workers=max_workers, **rest)

    monkeypatch.setattr(cli_module, "write_slides", _spy)
    out_dir = tmp_path / "decks"

    main([
        "generate", str(TEMPLATE), str(CONTENT_PACK), "-o", str(out_dir),
        "--variant", "dense", "--writer-max-workers", "1",
    ])
    assert captured["max_workers"] == 1


def test_generate_does_not_run_visual_audit_and_says_how_to_run_it(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    out_dir = tmp_path / "decks"

    exit_code = main([
        "generate", str(TEMPLATE), str(CONTENT_PACK), "-o", str(out_dir), "--variant", "dense",
    ])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "audit-visual" in out, "отчёт обязан назвать команду, которой запустить аудит по картинке отдельно"
    assert "не выполнял" in out.lower(), "отчёт обязан честно сказать, что модельный аудит по картинке НЕ выполнялся"


def test_audit_visual_runs_on_a_finished_pptx_without_regenerating_it(monkeypatch, capsys, tmp_path):
    """`run_visual` не зависит от сборки — эта команда работает на уже
    готовом `.pptx` (из `generate`) и сохранённом рядом `DeckSpec` (json),
    не вызывая модель для написания текста заново."""
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    out_dir = tmp_path / "decks"
    main(["generate", str(TEMPLATE), str(CONTENT_PACK), "-o", str(out_dir), "--variant", "dense"])
    capsys.readouterr()  # очистить вывод generate — интересен только вывод audit-visual

    pptx_files = list(out_dir.glob("*dense-t13.pptx"))
    json_files = list(out_dir.glob("*deck-t13.json"))
    assert pptx_files and json_files
    pptx_path, deck_json_path = pptx_files[0], json_files[0]

    # Дамп обязан быть настоящим DeckSpec (не сырым `dataclasses.fields`
    # без тега `type` у блока) — round-trip проверен отдельно в
    # `tests/plan/test_spec.py`; здесь достаточно, что CLI его пишет и сам
    # же умеет прочитать.
    data = json.loads(deck_json_path.read_text(encoding="utf-8"))
    assert data["slides"]
    assert all("type" in b for s in data["slides"] for b in s["blocks"])

    exit_code = main(["audit-visual", str(TEMPLATE), str(pptx_path), str(deck_json_path)])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "Детерминированный аудит" in out
    assert "не выполнял" in out.lower() or "не задан" in out.lower()  # честная деградация без ключа
    assert "Сводный отчёт" in out


def test_generate_plans_layouts_per_style_before_the_text(monkeypatch, capsys, tmp_path):
    """Задача P: у каждого стиля свои раскладки, назначенные до текста;
    писатель получает контракты, и раскладка из контракта доходит до
    плана собранной колоды."""
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    captured: dict = {}
    real_write_slides = cli_module.write_slides

    def _spy(outline, contracts, sources, profile, llm, **kw):
        captured[kw["style"]] = [c.pattern_id for c in contracts]
        return real_write_slides(outline, contracts, sources, profile, llm, **kw)

    monkeypatch.setattr(cli_module, "write_slides", _spy)
    # Писатель подменён моделью, которая отвечает ровно по контракту: без
    # ключа запасной текст пуст, и слайды ушли бы в разделители.
    from tests.plan.test_writer import _ContractLLM

    real_build = cli_module._build_role_provider
    monkeypatch.setattr(
        cli_module, "_build_role_provider",
        lambda role, **kw: _ContractLLM() if role == "writer" else real_build(role, **kw),
    )
    out_dir = tmp_path / "decks"

    exit_code = main([
        "generate", str(TEMPLATE), str(CONTENT_PACK), "-o", str(out_dir), "--variant", "dense", "--variant", "visual",
    ])
    assert exit_code == 0

    assert set(captured) == {cli_module.Variant.dense, cli_module.Variant.visual}
    assert captured[cli_module.Variant.dense] != captured[cli_module.Variant.visual]
    for style, planned in captured.items():
        written = json.loads(next(out_dir.glob(f"*__{style.value}-deck.json")).read_text(encoding="utf-8"))
        same = sum(a == b["pattern_id"] for a, b in zip(planned, written["slides"]))
        assert same >= len(planned) - 2, (planned, [s["pattern_id"] for s in written["slides"]])
    out = capsys.readouterr().out
    assert "раскладки назначены" in out and "мест в пределах контракта" in out
    assert list(out_dir.glob("*__outline.json"))


