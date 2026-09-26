"""`export_bundle` — выгрузка готовой колоды сразу в три формата (ТЗ,
дословно: "Экспорт в .html, .pptx, .pdf") плюс постраничные PNG-превью
(интерфейсу и будущему визуальному аудиту, Task 12 брифа — превью нужен и
там, и там, поэтому рендерится один раз здесь, не дважды в разных местах).

`.pptx` не пересобирается — он уже есть на диске (`pptx_path`, готовый файл
`compose.builder.build_deck`), `export_bundle` только копирует путь до него
в `Bundle`. `.pdf`/`.png` — через `deckforge.render.soffice` (LibreOffice, с
изоляцией параллельных вызовов и системным шрифтом шаблона — обе грабли
задокументированы там). `.html` — через `deckforge.export.html.to_html_report`.

`deck_spec` — необязательный: интерфейс брифа (`export_bundle(pptx_path,
profile, out_dir)`) не несёт его вовсе, а `to_html` без содержания плана
по-прежнему честно строит страницу из самого `.pptx` (см. докстроку
`export.html`) — просто без мягкой привязки `data-kind` к `SlideSpec.kind`.
Когда `deck_spec` под рукой (обычный путь вызова из `cli.generate`, где
план уже есть) — передаётся явно, разметка получается точнее.

`budget`/`risky_slides` (задача L) — необязательный снимок бюджета прогона
и слайдов, ушедших на аудит по картинке; прокидываются буквально в
`to_html_report` (см. её докстроку), сам экспорт их не читает."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from deckforge.export.html import to_html_report
from deckforge.plan.spec import DeckSpec
from deckforge.render.soffice import to_pdf, to_pngs
from deckforge.template.profile import TemplateProfile

# ТЗ: "превью постранично, с указанием разрешения" — 110 DPI, тот же дефолт,
# что и `render.soffice.to_pngs` (см. её докстроку: читаемо на превью-панели
# интерфейса и хватает деталей визуальному аудиту по картинке, не раздувая
# файл десятками мегапикселей на слайд).
DEFAULT_PREVIEW_DPI = 110


@dataclass
class Bundle:
    pptx: Path
    pdf: Path
    html: Path
    # Постраничные PNG-превью — не часть трёх форматов ТЗ, но обязательная
    # часть этой задачи (брифом пользователя: "рендер превью в PNG нужен и
    # интерфейсу, и будущему аудиту — сделай его частью этой задачи").
    pngs: list[Path] = field(default_factory=list)
    preview_dpi: int = DEFAULT_PREVIEW_DPI
    # Честные деградации (шрифт шаблона не нашёлся в системе и т.п.) —
    # тот же принцип, что и `TemplateProfile.warnings`: не молчать.
    warnings: list[str] = field(default_factory=list)


def export_bundle(
    pptx_path: Path, profile: TemplateProfile, out_dir: Path, *,
    deck_spec: DeckSpec | None = None, preview_dpi: int = DEFAULT_PREVIEW_DPI,
    budget: dict | None = None, risky_slides: dict[int, dict] | None = None,
) -> Bundle:
    pptx_path = Path(pptx_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # PDF рендерится ровно один раз: soffice конвертирует pptx->pdf для
    # итогового `.pdf` бандла, а `to_pngs` получает готовый файл через
    # `pdf_path=`, а не гоняет ту же конвертацию заново (замер: 302.8с на
    # двойной рендер против лимита ТЗ в 300с на всю генерацию колоды).
    pdf_path = to_pdf(pptx_path, out_dir)
    pngs = to_pngs(pptx_path, out_dir / "preview", dpi=preview_dpi, pdf_path=pdf_path)

    spec = deck_spec if deck_spec is not None else DeckSpec(title=pptx_path.stem, language="ru", slides=[])
    html_result = to_html_report(
        spec, profile, pptx_path, out_dir / f"{pptx_path.stem}.html",
        budget=budget, risky_slides=risky_slides,
    )

    return Bundle(
        pptx=pptx_path, pdf=pdf_path, html=html_result.path, pngs=pngs,
        preview_dpi=preview_dpi, warnings=list(html_result.warnings),
    )
