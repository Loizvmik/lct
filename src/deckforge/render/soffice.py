"""Рендер `.pptx` в `.pdf`/`.png` через LibreOffice (`soffice`).

Две известные грабли (task-12-brief, раздел про soffice; подтверждено на
этой машине):

1. Два параллельных вызова `soffice` без собственного профиля пользователя
   дерутся за блокировку общего профиля (`~/Library/Application
   Support/LibreOffice`) — один из них виснет или падает. На каждый вызов
   заводится свой временный `-env:UserInstallation=file://...`, который
   удаляется по завершении вызова.
2. Без `FONTCONFIG_PATH`, указывающего на каталог fontconfig, видящий
   системные шрифты (`/opt/homebrew/etc/fonts` на этой машине), LibreOffice
   не находит установленный в систему шрифт шаблона (`Play`) и молча
   подставляет шрифт с засечками — PDF/PNG расходятся с `.pptx` не только
   по мелочам вёрстки, а по гарнитуре целиком.

PNG получаются через PDF (`pdftoppm`, если есть в PATH) — по слайду через
`--convert-to png` LibreOffice не умеет напрямую (экспортирует только первую
страницу многостраничного файла), поэтому это не запасной путь, а основной;
`--convert-to png:calc_png_Export`-подобный обход тоже не решает эту
проблему, тем же способом идёт `pdftocairo`, если `pdftoppm` не нашёлся.

## Выборочный рендер страниц (задача "разбор незнакомого шаблона в бюджет")

Живой замер на контрольном `ЛЦТ2026 Шаблон презентации.pptx` (37 слайдов,
изолированный прогон, чистый временный каталог): `to_pdf` (конвертация
ЦЕЛОГО файла) — 41.6с, `pdftoppm` НА ВСЕ 37 страниц при dpi=110 — ещё 25.4с
поверх. `soffice` не умеет конвертировать в PDF только часть слайдов (грабля
уже описана выше) — эта часть стоимости фиксированная и неустранимая этой
правкой. А вот `pdftoppm` РАЗМЕНИВАЕТ диапазон страниц (`-f`/`-l`) — тот же
замер, растрирование только 9 РАЗБРОСАННЫХ страниц из тех же 37 (по одному
вызову `pdftoppm -f N -l N` на страницу, раз страницы не идут подряд) — 4.6с
вместо 25.4с. `to_pngs(..., pages=[...])` ниже пользуется именно этим: сама
конвертация в PDF по-прежнему одна на весь файл (иначе нельзя), а
растрирование — только запрошенных страниц, не всех.
"""
from __future__ import annotations
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from deckforge.settings import Settings

APP_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"

# Дедлайн одного вызова soffice/pdftoppm по wall-clock — щедрый (ТЗ отводит
# 300с на всю генерацию колоды, рендер — только один из шагов конвейера),
# но конечный: зависший (не убитый системой) процесс LibreOffice не должен
# вешать вызывающий код навсегда.
_SOFFICE_TIMEOUT_SECONDS = 120.0
_PDFTOPPM_TIMEOUT_SECONDS = 60.0


class RenderError(RuntimeError):
    """soffice/pdftoppm завершились с ошибкой или не нашлись на машине."""


def _load_settings() -> Settings | None:
    try:
        return Settings.load(APP_YAML_PATH)
    except Exception:
        return None


def find_soffice() -> str | None:
    """Путь к бинарнику `soffice`, либо `None`, если не нашёлся ни в
    `config/app.yaml`, ни автопоиском (`RenderConfig.resolve_soffice`)."""
    settings = _load_settings()
    render = settings.render if settings is not None else None
    try:
        if render is not None:
            return render.resolve_soffice()
        # Конфиг недоступен вовсе (нет файла/битый YAML) — тот же автопоиск,
        # что и в RenderConfig.resolve_soffice, но с дефолтными кандидатами
        # без ключа render.soffice_path.
        from deckforge.settings import RenderConfig

        return RenderConfig().resolve_soffice()
    except RuntimeError:
        return None


def _fontconfig_path() -> str | None:
    settings = _load_settings()
    if settings is not None:
        return settings.render.resolve_fontconfig()
    from deckforge.settings import RenderConfig

    return RenderConfig().resolve_fontconfig()


def soffice_available() -> bool:
    return find_soffice() is not None


def _run_soffice(args: list[str], *, cwd: Path | None = None) -> None:
    soffice = find_soffice()
    if soffice is None:
        raise RenderError(
            "soffice не найден — установите LibreOffice или укажите render.soffice_path "
            "в config/app.yaml."
        )

    # Свой профиль на каждый вызов (грабля №1, см. докстроку модуля) —
    # уникальный каталог по uuid, не по имени файла: два параллельных
    # вызова НА ОДНОМ И ТОМ ЖЕ .pptx (типичный кейс — рендер PDF и PNG
    # одной колоды подряд/параллельно) иначе снова получили бы общий путь.
    profile_dir = Path(tempfile.gettempdir()) / f"deckforge-soffice-{uuid.uuid4().hex}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    import os

    env = dict(os.environ)
    fontconfig = _fontconfig_path()
    if fontconfig:
        env["FONTCONFIG_PATH"] = fontconfig

    cmd = [
        soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault",
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        *args,
    ]
    try:
        result = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_SOFFICE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"soffice не уложился в {_SOFFICE_TIMEOUT_SECONDS:.0f}с: {' '.join(cmd)}") from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    if result.returncode != 0:
        raise RenderError(
            f"soffice завершился с кодом {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )


def to_pdf(pptx: Path, out_dir: Path) -> Path:
    """Конвертирует `pptx` в PDF в `out_dir` (создаётся при необходимости).

    Имя выходного файла — `soffice`'овское по умолчанию: `{stem}.pdf`.
    """
    pptx = Path(pptx)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _run_soffice(["--convert-to", "pdf", "--outdir", str(out_dir), str(pptx)])

    out_path = out_dir / f"{pptx.stem}.pdf"
    if not out_path.exists():
        raise RenderError(f"soffice не создал ожидаемый файл {out_path}")
    return out_path


def _pdftoppm_binary() -> str | None:
    return shutil.which("pdftoppm") or shutil.which("pdftocairo")


def to_pngs(pptx: Path, out_dir: Path, dpi: int = 110, pages: list[int] | None = None) -> list[Path]:
    """Растрирует слайды `pptx` в отдельные PNG (`{stem}-N.png`), по
    возрастанию номера страницы, с разрешением `dpi` точек на дюйм.

    Идёт через PDF (`pdftoppm -r {dpi} -png`, см. докстроку модуля), не
    напрямую `soffice --convert-to png` — тот экспортирует только первую
    страницу многостраничного файла.

    `pages` — `None` (по умолчанию) растрирует ВСЕ страницы файла, одним
    вызовом `pdftoppm`, как раньше. Список конкретных 1-based номеров
    страниц растрирует ТОЛЬКО их — по одному вызову `pdftoppm -f N -l N` на
    страницу (poppler не принимает произвольный несмежный список страниц
    одним диапазоном `-f`/`-l`, только сплошной интервал; страницы задачи
    "разбор незнакомого шаблона в бюджет" разбросаны по файлу, не идут
    подряд — см. докстроку модуля про живой замер, почему это того стоит).
    Конвертация в PDF (`to_pdf`) в обоих случаях одна на весь файл — этого
    шага `pages` не касается, `soffice` не умеет конвертировать частично."""
    pptx = Path(pptx)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = to_pdf(pptx, out_dir)

    poppler_bin = _pdftoppm_binary()
    if poppler_bin is None:
        raise RenderError(
            "ни pdftoppm, ни pdftocairo не найдены в PATH — установите poppler "
            "(`brew install poppler`) для растрирования превью."
        )

    prefix = out_dir / pptx.stem
    if pages is None:
        cmds = [[poppler_bin, "-r", str(dpi), "-png", str(pdf_path), str(prefix)]]
    else:
        # Один вызов на страницу (см. докстроку выше) — каждый пишет ровно
        # один файл `{stem}-{page}.png` (тот же формат имени, что и полный
        # рендер, `-f N -l N` не меняет схему именования poppler'ом), так
        # что дальнейшая сортировка/поиск по `{stem}-*.png` работает
        # одинаково для обеих веток.
        cmds = [
            [poppler_bin, "-r", str(dpi), "-png", "-f", str(page), "-l", str(page), str(pdf_path), str(prefix)]
            for page in pages
        ]

    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=_PDFTOPPM_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise RenderError(f"{poppler_bin} не уложился в {_PDFTOPPM_TIMEOUT_SECONDS:.0f}с") from exc
        if result.returncode != 0:
            raise RenderError(f"{poppler_bin} завершился с кодом {result.returncode}:\n{result.stderr}")

    pngs = sorted(out_dir.glob(f"{pptx.stem}-*.png"), key=_page_number)
    if not pngs:
        raise RenderError(f"{poppler_bin} не создал ни одного PNG в {out_dir}")
    return pngs


def _page_number(path: Path) -> int:
    """`{stem}-3.png`/`{stem}-03.png` → 3 — числовая, не лексикографическая
    сортировка (иначе `-10` встаёт перед `-2`)."""
    suffix = path.stem.rsplit("-", 1)[-1]
    try:
        return int(suffix)
    except ValueError:
        return 0
