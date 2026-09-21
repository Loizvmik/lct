"""Замер текста — единственный способ измерить текст в проекте.

И укладка (`compose/builder.py`, эта задача), и будущий аудит «текст не
поместился в свою рамку» обязаны считать через `measure()`. Если бы у
сборки и проверки были два независимых расчёта одного и того же (площадная
эвристика тут, честный замер там), они бы разошлись молча — аудит начал бы
ругаться на то, чего сборка не видела, и наоборот. `patterns.py`
(`estimate_slot_chars`) — площадная эвристика по знакам, а не по глифам, и
это её собственная докстрока прямо объявляет временной заглушкой, которую
эта задача заменяет.

Замер — на Pillow (`ImageFont.getlength`), не на реальном рендере .pptx:
дешёвый способ узнать, сколько строк займёт текст в рамке заданной ширины
заданным кеглем, без запуска LibreOffice/PowerPoint на каждый слайд.

## Шрифт для замера

Метрики зависят от РЕАЛЬНОГО рисунка шрифта — гарнитура «похожая по
имени» может иметь другую среднюю ширину глифа. Цепочка подмены (`font_
file_for`, брифом дословно):

1. Шрифт шаблона, встроенный в сам `.pptx` (`ppt/fonts/*.fntdata`,
   `register_template_fonts` ниже) — если он был извлечён заранее.
2. Тот же шрифт, что и запрошен (`font_family`), но найденный в системе.
3. Liberation Sans (метрически совместим с Arial, свободно
   распространяется, часто уже стоит в CI/Linux-окружениях).
4. Arial.
5. Встроенный в Pillow шрифт (`ImageFont.load_default`), если вообще
   ничего из вышеперечисленного не нашлось — честная деградация, не
   исключение (см. `test_unknown_font_falls_back_without_raising`).

### Про `.fntdata` и то, чем это оказалось на самом деле

Бриф задачи (со слов постановщика) описывал `.fntdata` как TrueType,
обфусцированный XOR-ом первых 32 байт ключом из GUID в имени парта —
классическая схема ECMA-376 Part 4 §2.8.1 для **Word**-документов. На
факте (см. `_extract_eot_font` и разведку в отчёте задачи) все `.fntdata`
трёх учебных шаблонов — контейнеры **EOT** (Embedded OpenType, тот же
формат, что отдавал IE6-9 для веб-шрифтов), а не голый TTF. У EOT есть
свой честный, НЕобфусцированный заголовок (`EOTSize`/`FontDataSize`/
`Version`/`Flags`/имя семейства и т.д. — все поля читаются как есть, без
какого-либо ключа), а собственно шрифтовые байты внутри — в нашем случае у
всех трёх учебных шаблонов байт-в-байт один и тот же файл Play (тот же
md5, та же длина что у VK Tech, что у WorkSpace, что у Education) — сжаты
алгоритмом MicroType Express (`Flags & 0x4`, `TTEMBED_TTCOMPRESSED`).
Реализация MTX (проприетарный, недокументированный официально кодек;
референсный открытый декодер — `libeot`, ~85КБ плотного C) — самостоятельная
задача заметно большего объёма, чем эта; переносить и валидировать её
здесь означало бы или не уложиться в бюджет задачи, или получить кривой
декодер, который тихо портит метрики хуже честного отката на запасной
шрифт. Решение: `_extract_eot_font` честно парсит EOT-контейнер (реальный,
проверенный на всех трёх учебных файлах формат) и отдаёт сырые байты
шрифта, ТОЛЬКО когда `Flags` не несёт ни `TTEMBED_TTCOMPRESSED`, ни
`TTEMBED_XORENCRYPTDATA` (либо когда обфускация — чистый XOR константным
байтом `0x50`, тривиально обратимый без всякого GUID — тоже реализовано);
на MTX-сжатых данных возвращает `None`, и `font_file_for` уходит по цепочке
дальше, к Liberation Sans/Arial. На трёх учебных шаблонах это означает: код
корректно опознаёт контейнер и его метаданные (семейство, начертание), но
сам замер идёт по метрически близкому Arial — честно объявленное
ограничение, не тихая порча (см. отчёт задачи).

Task 9, повторная правка (находка №2 отчёта, "замер идёт не по настоящему
шрифту"). Практическая часть: `Play` (учебные шаблоны, OFL, Google Fonts)
теперь установлен в системе координатором — цепочка подмены находит его на
шаге 2 ("тот же шрифт, что и запрошен, но из системы"), и на всех трёх
учебных шаблонах замер снова идёт по настоящим метрикам Play, а не по
Arial (координатор явно подтвердил: запас на этот случай закладывать не
нужно, `.fntdata` при этом остаётся нечитаемым — MTX по-прежнему не
декодирован, просто тот же самый шрифт нашёлся ещё и в системе).

Честная часть — на будущее (шаблон с защищённым/недоступным где-либо
шрифтом): `measure()` теперь отдаёт `TextMetrics.font_source` —
`"exact"`, когда шрифт резолвился как встроенный в шаблон ИЛИ найденный в
системе ПОД ТЕМ ЖЕ именем семейства (`_resolve_font`, см. ниже), и
`"fallback"`, когда пришлось уйти на Liberation Sans/Arial/Pillow-дефолт.
В `"fallback"`-случае `height_in`/`longest_word_in` домножаются на
`_FALLBACK_SAFETY_MARGIN` (см. её докстроку про откуда число) — метрика
"влезает ли" не должна молча выдавать себя за точную, когда шрифт другой.
"""
from __future__ import annotations
import hashlib
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import PptxPackage

PT_PER_INCH = 72.0

# Интерлиньяж по умолчанию, когда вызывающий не передал `line_spacing` из
# `TypeScale` (брифом: "LINE_SPACING берётся из TypeScale, а не константой"
# — константа здесь только запасной вариант для вызовов БЕЗ профиля,
# `builder.py` всегда передаёт `type_scale.heading_line_spacing`/
# `body_line_spacing` явно). 1.2 — типографская норма одинарного интервала
# с небольшим запасом, не откалибровано под три учебных шаблона.
_DEFAULT_LINE_SPACING = 1.2

_CACHE_DIR = Path(tempfile.gettempdir()) / "deckforge-fonts"

# Запас на приблизительность замера, когда точный шрифт недоступен и мы
# меряем текст запасной гарнитурой (Liberation Sans/Arial/Pillow-дефолт).
# Обоснование числа: сравнение средней ширины глифа между метрически НЕ
# откалиброванными друг под друга открытыми sans-гарнитурами (та же пара
# семейств, что видна в этом проекте — например Play против Arial)
# типично расходится на 10-20% (Liberation Sans — единственная в цепочке,
# что специально калибровалась под Arial "глиф-в-глиф"; для произвольной
# третьей гарнитуры такой калибровки нет и в общем случае не бывает).
# 1.15 — середина этого диапазона с округлением вверх: закладывает запас
# на типичный случай, не на held-out экстремум (моноширинные/узкие
# декоративные гарнитуры бывают ещё дальше, но такое либо не пройдёт даже
# с запасом, либо не является типичным телом слайда). Инфляция — только
# на выходные метрики (`height_in`/`longest_word_in`), не на сам перенос
# по словам: пересимулировать перенос на чужих метриках с инфляцией
# ширины было бы точнее, но кратно дороже, а после инфляции высоты и так
# получаем нужный сигнал "решительно не влезет" на 15% раньше, чем без
# запаса — честно объявленное упрощение, не молчаливая неточность.
_FALLBACK_SAFETY_MARGIN = 1.15


@dataclass(frozen=True)
class TextMetrics:
    lines: int
    height_in: float
    longest_word_in: float
    # "exact" — резолвился шрифт шаблона/системный шрифт ПОД ТЕМ ЖЕ именем
    # семейства; "fallback" — цепочка подмены дошла до Liberation Sans/
    # Arial/Pillow-дефолта. См. докстроку модуля и `_FALLBACK_SAFETY_MARGIN`.
    font_source: str = "exact"


# ---------------------------------------------------------------------------
# Замер
# ---------------------------------------------------------------------------


def measure(
    text: str, font_family: str, size_pt: float, box_width_in: float,
    *, line_spacing: float = _DEFAULT_LINE_SPACING,
) -> TextMetrics:
    """Сколько строк займёт `text` кеглем `size_pt` в рамке шириной
    `box_width_in` (высота не ограничивает — рамка эластична по высоте,
    вопрос "влезает ли высота" решает вызывающий сравнением `height_in` с
    реальной высотой слота).

    `\\x0b` (мягкий перенос строки, ровно то, что python-pptx отдаёт как
    `.text_frame.text` для `a:br`) считается ЖЁСТКИМ переносом строки, тем
    же, что и `\\n` — семантически это новая строка внутри абзаца, не
    пробел."""
    font_path, font_source = _resolve_font(font_family)
    font_key = str(font_path) if font_path is not None else None
    size_px = max(1, round(size_pt))
    max_width_pt = max(box_width_in, 0.0) * PT_PER_INCH

    normalized = text.replace("\x0b", "\n")
    total_lines = 0
    longest_word_pt = 0.0
    for para in normalized.split("\n"):
        if para == "":
            total_lines += 1
            continue
        line_count, para_longest = _wrap_paragraph(font_key, size_px, para, max_width_pt)
        total_lines += line_count
        longest_word_pt = max(longest_word_pt, para_longest)

    height_in = total_lines * (size_pt / PT_PER_INCH) * line_spacing
    longest_word_in = longest_word_pt / PT_PER_INCH
    if font_source == "fallback":
        # см. докстроку `_FALLBACK_SAFETY_MARGIN` — запас на приблизительность,
        # не на сам перенос по словам (тот уже посчитан выше запасным шрифтом).
        height_in *= _FALLBACK_SAFETY_MARGIN
        longest_word_in *= _FALLBACK_SAFETY_MARGIN
    return TextMetrics(lines=total_lines, height_in=height_in, longest_word_in=longest_word_in, font_source=font_source)


def _wrap_paragraph(font_key: str | None, size_px: int, para: str, max_width_pt: float) -> tuple[int, float]:
    """Жадный перенос по словам одного абзаца (без `\\n` внутри). Возвращает
    число получившихся строк и ширину самого длинного отдельного слова
    абзаца (в pt) — слово шире рамки всё равно становится отдельной
    строкой, а не обрезается здесь (решение "что делать, если не влезает
    даже одно слово" — за вызывающим, `builder.fits`)."""
    words = para.split(" ")
    lines = 0
    longest_word_pt = 0.0
    current = ""
    for word in words:
        if word:
            longest_word_pt = max(longest_word_pt, _line_width_pt(font_key, size_px, word))
        candidate = word if not current else f"{current} {word}"
        if not current or _line_width_pt(font_key, size_px, candidate) <= max_width_pt:
            current = candidate
        else:
            lines += 1
            current = word
    lines += 1  # последняя накопленная строка
    return lines, longest_word_pt


@lru_cache(maxsize=8192)
def _line_width_pt(font_key: str | None, size_px: int, text: str) -> float:
    """Ширина строки `text` в pt — кешируется по (шрифт, кегль, строка):
    без кеша слайд с таблицей на десяток строк мерится минутами (см. тест
    `test_measurement_is_cached` — одна и та же ячейка перемеряется сотни
    раз при перепланировке колонок)."""
    font = _load_font(font_key, size_px)
    return font.getlength(text)


@lru_cache(maxsize=256)
def _load_font(font_key: str | None, size_px: int):
    """Загрузка файла шрифта — отдельный кеш от `_line_width_pt` (брифом
    дословно: `@lru_cache` на загрузку шрифта И на замер строки, это два
    разных по стоимости шага — разбор файла шрифта на порядки дороже
    одного `getlength`)."""
    if font_key is not None:
        try:
            return ImageFont.truetype(font_key, size=size_px)
        except Exception:
            pass  # битый/нечитаемый файл шрифта — уходим на встроенный Pillow
    return ImageFont.load_default(size=size_px)


# ---------------------------------------------------------------------------
# Подбор файла шрифта — цепочка подмены
# ---------------------------------------------------------------------------

_EMBEDDED_FONTS: dict[str, Path] = {}

_FALLBACK_FAMILIES = ("Liberation Sans", "Arial")

_SYSTEM_FONT_DIRS = [
    Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"),
    Path("/Library/Fonts"), Path.home() / "Library" / "Fonts",
    Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
    Path("C:/Windows/Fonts"),
]


def _normalize_family(name: str) -> str:
    return name.strip().casefold()


def font_file_for(family: str) -> Path | None:
    """Файл шрифта для замера — цепочка подмены целиком (см. докстроку
    модуля): встроенный в шаблон → тот же шрифт из системы → Liberation
    Sans → Arial → `None` (замер тогда падает на встроенный Pillow, см.
    `_load_font`). Часть интерфейса брифа дословно (Task 9, Step 1) —
    сигнатура `family -> Path | None` не меняется; провенанс (точный шрифт
    или запасной) отдаёт `_resolve_font`, которым пользуется `measure()`."""
    return _resolve_font(family)[0]


def _resolve_font(family: str) -> tuple[Path | None, str]:
    """То же самое, что `font_file_for`, плюс провенанс: `"exact"`, когда
    файл резолвился ПОД ТЕМ ЖЕ именем семейства (встроенный в шаблон или
    найденный в системе), `"fallback"`, когда цепочка ушла на Liberation
    Sans/Arial/Pillow-дефолт (см. `TextMetrics.font_source`,
    `_FALLBACK_SAFETY_MARGIN`)."""
    key = _normalize_family(family)
    embedded = _EMBEDDED_FONTS.get(key)
    if embedded is not None:
        return embedded, "exact"

    found = _system_font_file(family)
    if found is not None:
        return found, "exact"
    for fallback in _FALLBACK_FAMILIES:
        found = _system_font_file(fallback)
        if found is not None:
            return found, "fallback"
    return None, "fallback"


@lru_cache(maxsize=128)
def _system_font_file(family: str) -> Path | None:
    key = _normalize_family(family)
    found = _fc_match(family, key)
    if found is not None:
        return found
    return _scan_font_dirs(key)


def _fc_match(family: str, key: str) -> Path | None:
    """`fc-match` (fontconfig) — самый надёжный кросс-платформенный способ
    найти файл шрифта по имени семейства (macOS с Homebrew, большинство
    Linux). fontconfig ВСЕГДА возвращает какой-то шрифт (правило
    подстановки), даже когда точного совпадения нет — поэтому семейство
    результата проверяется буквально, а не принимается на веру: иначе
    "Play"/"Liberation Sans" на машине без них молча подменялись бы первым
    попавшимся шрифтом системы (Hiragino/Verdana), а не честно катились бы
    дальше по цепочке."""
    if shutil.which("fc-match") is None:
        return None
    try:
        result = subprocess.run(
            ["fc-match", "--format=%{file}|%{family}\n", family],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if "|" not in line:
        return None
    file_part, family_part = line.split("|", 1)
    names = {_normalize_family(n) for n in family_part.split(",")}
    if key not in names:
        return None
    path = Path(file_part)
    return path if path.is_file() else None


def _scan_font_dirs(key: str) -> Path | None:
    """Запасной ручной обход типичных каталогов шрифтов — на случай, если
    `fc-match` недоступен (окружение без fontconfig)."""
    candidates = {key, key.replace(" ", ""), key.replace(" ", "-")}
    for base in _SYSTEM_FONT_DIRS:
        if not base.is_dir():
            continue
        try:
            entries = list(base.rglob("*"))
        except OSError:
            continue
        for path in entries:
            if path.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            stem_key = _normalize_family(path.stem)
            if stem_key in candidates or stem_key.startswith(f"{key}-") or stem_key.startswith(f"{key} "):
                return path
    return None


# ---------------------------------------------------------------------------
# Извлечение встроенных шрифтов шаблона
# ---------------------------------------------------------------------------

# EOT: см. докстроку модуля. Заголовок описан W3C Member Submission
# "Embedded OpenType (EOT) File Format" (2008-03-05), §"EOT File Header".
_EOT_MAGIC = 0x504C  # обязателен по спецификации на любом валидном EOT
_EOT_MAGIC_OFFSET = 0x22
_EOT_FIXED_HEADER_SIZE = 0x52  # до FamilyNameSize включительно

_TTEMBED_TTCOMPRESSED = 0x00000004
_TTEMBED_XORENCRYPTDATA = 0x10000000
_EOT_XOR_BYTE = 0x50

_SFNT_SIGNATURES = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")


def register_template_fonts(template_path: Path) -> dict[str, Path]:
    """Извлекает встроенные шрифты `.pptx`-шаблона (`p:embeddedFontLst` в
    `ppt/presentation.xml`) и регистрирует их для `font_file_for`/`measure`
    — вызывается сборщиком (`builder.build_deck`) ДО укладки слайдов, чтобы
    замер шёл по настоящему шрифту шаблона, если его удалось извлечь (см.
    докстроку модуля про то, когда это не получается — MTX-сжатые данные).

    Возвращает фактически извлечённые {нормализованное семейство: путь} —
    может быть пустым словарём (шаблон без встроенных шрифтов вовсе, или
    все они не поддаются извлечению), это не ошибка."""
    extracted: dict[str, Path] = {}
    try:
        with PptxPackage.open(template_path) as pkg:
            for family, part_name in _embedded_font_parts(pkg):
                try:
                    data = pkg.part(part_name)
                except KeyError:
                    continue
                font_bytes = _extract_eot_font(data)
                if font_bytes is None:
                    continue
                path = _write_font_cache(part_name, font_bytes)
                extracted[_normalize_family(family)] = path
    except Exception:
        # Разбор пакета/XML сломан — регистрация шрифтов не должна ронять
        # сборку колоды, замер просто идёт по цепочке подмены целиком.
        return {}
    _EMBEDDED_FONTS.update(extracted)
    return extracted


def _embedded_font_parts(pkg: PptxPackage) -> list[tuple[str, str]]:
    """[(семейство, имя парта .fntdata)] по `p:embeddedFontLst` —
    `p:regular` и `p:bold` дают одно и то же семейство (замеру шрифт
    начертания не важен, `TypeScale.bold_is_idiomatic` решает жирность
    отдельно, см. builder.py); обе записи регистрируются под одним
    семейством, последняя (bold) в порядке обхода побеждает — не
    принципиально, метрики body/regular и bold отличаются мало."""
    root = pkg.xml(pkg.presentation_part())
    lst = root.find(qn("p:embeddedFontLst"))
    if lst is None:
        return []
    rels = pkg.rels(pkg.presentation_part())
    out: list[tuple[str, str]] = []
    for embedded_font in lst.findall(qn("p:embeddedFont")):
        font_el = embedded_font.find(qn("p:font"))
        family = font_el.get("typeface") if font_el is not None else None
        if not family:
            continue
        for tag in ("p:regular", "p:bold", "p:italic", "p:boldItalic"):
            ref = embedded_font.find(qn(tag))
            rid = ref.get(qn("r:id")) if ref is not None else None
            part_name = rels.get(rid) if rid else None
            if part_name:
                out.append((family, part_name))
    return out


def _extract_eot_font(data: bytes) -> bytes | None:
    """Сырые байты sfnt-шрифта (TTF/OTF) из `.fntdata`, либо `None`, если
    данные сжаты MTX (см. докстроку модуля — декодер не реализован) или
    формат вовсе не распознан.

    Данные уже являются валидным sfnt (нет EOT-обёртки вовсе — на случай
    будущего шаблона, экспортированного другим инструментом) — отдаются
    как есть."""
    if data[:4] in _SFNT_SIGNATURES:
        return data

    if len(data) < _EOT_FIXED_HEADER_SIZE + 2:
        return None
    try:
        eot_size, font_data_size, _version, flags = struct.unpack_from("<IIII", data, 0)
        magic = struct.unpack_from("<H", data, _EOT_MAGIC_OFFSET)[0]
    except struct.error:
        return None
    if magic != _EOT_MAGIC or eot_size != len(data) or font_data_size <= 0 or font_data_size > eot_size:
        return None  # не EOT (или битый заголовок) — распознать не можем

    if flags & _TTEMBED_TTCOMPRESSED:
        return None  # MTX — не реализовано, честно отдаём "не нашли"

    font_bytes = data[eot_size - font_data_size:]
    if flags & _TTEMBED_XORENCRYPTDATA:
        font_bytes = bytes(b ^ _EOT_XOR_BYTE for b in font_bytes)

    return font_bytes if font_bytes[:4] in _SFNT_SIGNATURES else None


def _write_font_cache(part_name: str, font_bytes: bytes) -> Path:
    """Кеш извлечённых шрифтов на диске, адресуемый по содержимому — тот же
    файл шаблона, открытый повторно (частый случай: несколько колод на
    одном шаблоне подряд), не переизвлекает и не перезаписывает байты."""
    digest = hashlib.sha256(font_bytes).hexdigest()[:16]
    suffix = Path(part_name).suffix or ".ttf"
    path = _CACHE_DIR / f"{digest}{suffix if suffix != '.fntdata' else '.ttf'}"
    if not path.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(font_bytes)
    return path
