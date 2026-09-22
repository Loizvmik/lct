"""Драйвер сборки пакета submission/ для промежуточной сдачи DeckForge.

Повторяет путь `deckforge generate` (cli._cmd_generate) один в один (parse ->
outline -> write -> photos -> per-variant apply_variant/build_deck/audit), но
добавляет export_bundle (pdf/html/png) на каждый вариант и пишет пофазный
замер времени + находки аудита в JSON рядом с пакетом, для сведения в
итоговую таблицу отчёта.

Использование:
    uv run python scripts/build_submission.py \
        <template.pptx> <content_pack_dir> <submission_subdir> \
        --variants dense airy visual \
        --stats-out scratchpad/stats/<slug>.json

Каждый вариант кладётся в <submission_subdir>/<variant>/ с .pptx/.pdf/.html и
preview/*.png внутри.
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from deckforge.audit.config import AuditConfig
from deckforge.audit.deterministic import run_deterministic
from deckforge.cli import _build_namer, _build_pattern_kind_vlm, _build_role_provider, _writer_agent_max_steps, _writer_max_workers
from deckforge.compose.builder import build_deck
from deckforge.export.bundle import export_bundle
from deckforge.plan.outline import build_outline, load_content_pack
from deckforge.plan.photos import assign_photos, load_content_pack_photos
from deckforge.plan.variants import Variant, apply_variant
from deckforge.plan.writer import write_slides
from deckforge.template.profile import TemplateProfile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("content_pack", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--variants", nargs="+", default=["dense", "airy", "visual"])
    parser.add_argument("--stats-out", type=Path, required=True)
    args = parser.parse_args()

    template = args.template.resolve()
    content_pack = args.content_pack.resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    args.stats_out.parent.mkdir(parents=True, exist_ok=True)

    stats: dict = {
        "template": template.name,
        "content_pack": content_pack.name,
        "variants": {},
    }

    namer = _build_namer()
    vision = _build_pattern_kind_vlm()
    outline_llm = _build_role_provider("outline")
    writer_llm = _build_role_provider("writer")

    t0 = time.monotonic()
    profile = TemplateProfile.from_file(template, namer=namer, vision=vision)
    t_parsed = time.monotonic()
    parse_s = t_parsed - t0
    print(f"[{template.name}] разобран за {parse_s:.1f}с, паттернов: {len(profile.patterns)}", flush=True)

    brief, sources, meta = load_content_pack(content_pack)
    target_slides = meta.get("target_slides")
    outline = build_outline(
        brief, sources, profile, outline_llm, target_slides,
        title=meta.get("title", content_pack.name), language=meta.get("language", "ru"),
    )
    t_outlined = time.monotonic()
    outline_s = t_outlined - t_parsed
    print(f"[{template.name}] структура: {len(outline.slides)} слайдов за {outline_s:.1f}с", flush=True)

    deck = write_slides(
        outline, sources, profile, writer_llm,
        max_workers=_writer_max_workers(), agent_max_steps=_writer_agent_max_steps(),
    )
    t_written = time.monotonic()
    write_s = t_written - t_outlined
    print(f"[{template.name}] текст слайдов написан за {write_s:.1f}с", flush=True)

    photos = load_content_pack_photos(content_pack)
    photo_llm = _build_role_provider("photo_picker") if photos else None
    deck, photo_report = assign_photos(deck, photos, photo_llm)
    t_photos = time.monotonic()
    photos_s = t_photos - t_written
    placed = photo_report.placed_count if photos else 0
    print(f"[{template.name}] фотографии: {len(photos)} пришло, {placed} распределено за {photos_s:.1f}с", flush=True)
    user_photos = {p.name: p.path for p in photos}

    config = AuditConfig.load()

    stats["parse_s"] = round(parse_s, 1)
    stats["outline_s"] = round(outline_s, 1)
    stats["write_s"] = round(write_s, 1)
    stats["photos_assign_s"] = round(photos_s, 1)
    stats["photos_available"] = len(photos)
    stats["photos_placed_by_planner"] = placed
    stats["photo_notes"] = list(photo_report.notes)
    stats["outline_slide_count"] = len(outline.slides)

    for variant_name in args.variants:
        variant = Variant[variant_name]
        variant_dir = out_dir / variant_name
        variant_dir.mkdir(parents=True, exist_ok=True)

        step_started = time.monotonic()
        variant_deck = apply_variant(deck, profile, variant)
        built_path = build_deck(variant_deck, profile, template, variant, user_photos=user_photos)
        build_s = time.monotonic() - step_started

        pptx_name = f"{template.stem}__{content_pack.name}__{variant_name}.pptx"
        pptx_path = variant_dir / pptx_name
        shutil.copy2(built_path, pptx_path)

        audit_started = time.monotonic()
        findings = run_deterministic(pptx_path, profile, config)
        audit_s = time.monotonic() - audit_started

        by_severity: dict[str, int] = {}
        for f in findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_check: dict[str, int] = {}
        for f in findings:
            by_check[f.check_id] = by_check.get(f.check_id, 0) + 1

        # Сколько фотографий контент-пакета реально встало в финальный .pptx
        # этого варианта (не план, а факт: разные варианты могут выбрать
        # раскладку без картиночного слота под уже назначенную фотографию).
        placed_in_pptx = _count_embedded_photos(pptx_path, user_photos)

        export_started = time.monotonic()
        bundle = export_bundle(pptx_path, profile, variant_dir, deck_spec=variant_deck)
        export_s = time.monotonic() - export_started

        total_s = (t_photos - t0) + build_s + audit_s + export_s

        print(
            f"[{template.name}][{variant_name}] {len(variant_deck.slides)} слайдов, "
            f"сборка {build_s:.1f}с, аудит {audit_s:.1f}с, выгрузка {export_s:.1f}с, "
            f"итого(с общими фазами) {total_s:.1f}с",
            flush=True,
        )
        print(f"  находки по серьёзности: {by_severity or '(нет)'}", flush=True)
        print(f"  находки по видам: {dict(sorted(by_check.items()))}", flush=True)
        print(f"  фотографий в файле: {placed_in_pptx}", flush=True)
        if bundle.warnings:
            print(f"  предупреждения экспорта: {bundle.warnings}", flush=True)

        stats["variants"][variant_name] = {
            "slide_count": len(variant_deck.slides),
            "build_s": round(build_s, 1),
            "audit_s": round(audit_s, 2),
            "export_s": round(export_s, 1),
            "total_s": round(total_s, 1),
            "findings_by_severity": by_severity,
            "findings_by_check": by_check,
            "findings_total": len(findings),
            "photos_embedded": placed_in_pptx,
            "pptx": str(pptx_path.relative_to(REPO_ROOT)),
            "pdf": str(bundle.pdf.relative_to(REPO_ROOT)),
            "html": str(bundle.html.relative_to(REPO_ROOT)),
            "png_count": len(bundle.pngs),
            "export_warnings": list(bundle.warnings),
        }

        args.stats_out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    total_wall = time.monotonic() - t0
    stats["total_wall_s"] = round(total_wall, 1)
    args.stats_out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{template.name}] всего по шаблону: {total_wall:.1f}с", flush=True)
    return 0


def _count_embedded_photos(pptx_path: Path, user_photos: dict[str, Path]) -> int:
    """Считает, сколько байтовых пар совпало между `ppt/media/*` собранного
    .pptx и файлами контент-пакета (по содержимому, не по имени — python-pptx
    переименовывает медиа при вставке, имя файла контент-пакета не сохраняется
    внутри архива)."""
    import hashlib
    import zipfile

    if not user_photos:
        return 0
    wanted = set()
    for path in user_photos.values():
        try:
            wanted.add(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        except OSError:
            continue
    if not wanted:
        return 0
    found = set()
    with zipfile.ZipFile(pptx_path) as zf:
        for name in zf.namelist():
            if not name.startswith("ppt/media/"):
                continue
            digest = hashlib.sha256(zf.read(name)).hexdigest()
            if digest in wanted:
                found.add(digest)
    return len(found)


if __name__ == "__main__":
    raise SystemExit(main())
