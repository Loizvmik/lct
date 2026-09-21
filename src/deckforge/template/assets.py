"""Каталог медиа шаблона: логотип, фоны, иконки, фото.

Инвентарь `PptxPackage.media()` даёт только метаданные файла (размер,
пиксели, альфа, md5) — сам по себе он не говорит, ЧТО изображение такое.
Разведка (см. бриф задачи) установила, что дедупликация по md5 между
шаблонами бесполезна (общих картинок нет вовсе), а внутри одного файла
дубликаты по содержимому тоже не встретились ни разу — тем не менее дедуп
по md5 сделан (`_group_media_by_md5`), потому что ничто не гарантирует его
отсутствие на произвольном четвёртом шаблоне, а два разных имени части с
одинаковым содержимым, посчитанные как два разных ассета, задвоили бы
число размещений логотипа и увели бы классификатор в сторону.

Единственный рабочий сигнал — СОВОКУПНОСТЬ признаков (бриф, раздел
«Требования к работе»): логотип — маленький файл + ссылка макета/мастера +
скромная ширина размещения + положение у края; фон — размещение почти во
весь холст; иконка — квадрат с альфой и мелкое размещение. Числа порогов
(0.9–1.1 для квадрата, 5–30% для ширины логотипа, 8% для иконки, 300 КБ и
95%/60% для фона, 50 КБ для фото) — литерал брифа, не подобраны по трём
файлам. Единственное значение, которое пришлось интерпретировать
самостоятельно, а не переписать дословно, — «четверть у края» логотипа
(см. `_EDGE_BAND` и её докстроку) и объединение порога 300 КБ между двумя
ветвями фона (см. `_background_confidence`) — оба случая откалиброваны не
по ответам трёх файлов, а обоснованы отдельно на месте.

Классификация не декодирует пиксели ни одного файла: у `PptxPackage.media()`
размеры и альфа уже читаются из заголовка (см. её докстроку), а фон/лого/
иконка этой задачи различаются по метаданным и геометрии размещения, не по
содержимому картинки. Кэш декодирования (как у `_picture_luminance` в
layouts.py) здесь не нужен именно поэтому.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field

from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.package import MediaEntry, PptxPackage
from deckforge.ooxml.walk import walk_shapes
from deckforge.template.layouts import LayoutEntry

# --- пороги классификатора (см. докстроку модуля про их происхождение) -----

_BG_MIN_BYTES = 300_000
_BG_FULL_BLEED_RATIO = 0.95
_BG_AREA_RATIO = 0.6

_LOGO_MAX_BYTES = 200_000
_LOGO_MIN_WIDTH = 0.05
_LOGO_MAX_WIDTH = 0.30
# "Одна из четвертей у края" (бриф, Step 2) — буквальное деление холста на 4
# равные полосы по каждой оси (1/4 = 0.25), а не число, подобранное по трём
# файлам: логотип у края — это центр размещения в крайней четверти по
# горизонтали ИЛИ по вертикали (см. `_near_edge`).
_EDGE_BAND = 0.25

_SQUARE_MIN = 0.9
_SQUARE_MAX = 1.1
_ICON_MAX_WIDTH = 0.08

_PHOTO_MIN_BYTES = 50_000

_FULL_BLEED_BOX = Box(left=0.0, top=0.0, width=1.0, height=1.0)


@dataclass(frozen=True)
class Placement:
    """Одно размещение ассета на слайде/лейауте/мастере.

    `part_kind` — где стоит шейп ("slide"/"layout"/"master"), не откуда
    унаследован фон: у фона, заданного через `p:bg` (см. `_bg_image_target`),
    `part_kind` — часть, которая САМА объявляет `p:bg` (лейаут или мастер),
    даже если у лейаута своего `p:bg` нет и он наследует фон мастера —
    в этом случае размещение попадает только на мастер, лейаут его не
    повторяет (иначе один и тот же фон-фото давал бы размещение на КАЖДОМ
    лейауте, который его наследует, и искусственно раздувал бы счётчик).
    """
    part_name: str
    part_kind: str
    box: Box


@dataclass(frozen=True)
class AssetRef:
    """Контракт интерфейса брифа (Step 1) дословно + `confidence` сверху —
    единообразие с соседними модулями (`LayoutEntry.kind_confidence` и
    т.п., см. «Требования к работе» брифа): мера уверенности в присвоенной
    категории, не «уверенность, что это вообще картинка». Для
    `unclassified` — всегда `0.0` (по определению ни один классификатор не
    сработал), для `photos` — фиксированная `0.5` (эта категория — то, что
    осталось после исключения остальных, у неё нет позитивного признака,
    который можно измерить, см. `_PHOTO_CONFIDENCE`).
    """
    part_name: str
    width: int | None
    height: int | None
    size_bytes: int
    has_alpha: bool
    square: bool
    placements: list[Placement] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class AssetCatalog:
    """Контракт интерфейса брифа (Step 1) дословно + `boxless_placements`.

    `boxless_placements` — не в перечне брифа, добавлено по прямому
    требованию задачи: «картинка без координат для классификации по
    размещению не годится — считай такие отдельно и покажи их число в
    результате» (см. докстроку `ShapeRef.box` в ooxml/walk.py и
    `_collect_placements`). Число шейпов-картинок, у которых удалось
    резолвить целевой медиа-файл, но не удалось вычислить `Box`
    (сломанная группа выше по дереву, см. `_read_group_frame`) — они не
    участвуют ни в одном списке ниже.
    """
    logo: AssetRef | None
    logo_placements: list[Placement]
    backgrounds: list[AssetRef]
    icons: list[AssetRef]
    photos: list[AssetRef]
    unclassified: list[AssetRef]
    boxless_placements: int = 0


@dataclass(frozen=True)
class _AssetGroup:
    """Один ассет после дедупа по md5 — `canonical` даёт метаданные (у всех
    частей с одинаковым md5 они идентичны: тот же байт-контент), а
    `part_names` — полный набор имён партов с этим содержимым, нужен для
    проверки «ссылается макет/мастер» (там речь о ЛЮБОМ имени-дубликате)."""
    canonical: MediaEntry
    part_names: frozenset[str]


@dataclass
class _Built:
    group: _AssetGroup
    placements: list[Placement]


def build_asset_catalog(pkg: PptxPackage, canvas: Canvas, layouts: list[LayoutEntry]) -> AssetCatalog:
    groups = _group_media_by_md5(pkg.media())
    name_to_group: dict[str, _AssetGroup] = {
        name: group for group in groups for name in group.part_names
    }
    layout_master_refs = _layout_master_image_refs(pkg, layouts)
    placements_by_group, boxless = _collect_placements(pkg, canvas, layouts, name_to_group)

    built = [
        _Built(group=group, placements=placements_by_group.get(id(group), []))
        for group in groups
    ]

    backgrounds: list[AssetRef] = []
    rest: list[_Built] = []
    for b in built:
        confidence = _background_confidence(b)
        if confidence is not None:
            backgrounds.append(_to_ref(b, confidence))
        else:
            rest.append(b)

    logo: AssetRef | None = None
    logo_built: _Built | None = None
    qualifying = [b for b in rest if _logo_confidence(b, layout_master_refs) is not None]
    if qualifying:
        # «Число размещений максимально» (бриф, Step 2) — сортировка по
        # ВСЕМ размещениям ассета, не только по тем, что попали в
        # квалифицирующее окно ширины/края (см. проверку на разведанных
        # файлах в отчёте задачи: и у WorkSpace, и у Education победитель
        # по этому критерию совпал с реальным логотипом).
        qualifying.sort(key=lambda b: (-len(b.placements), b.group.canonical.name))
        logo_built = qualifying[0]
        logo = _to_ref(logo_built, _logo_confidence(logo_built, layout_master_refs))

    icons: list[AssetRef] = []
    photos: list[AssetRef] = []
    unclassified: list[AssetRef] = []
    for b in rest:
        if b is logo_built:
            continue
        icon_confidence = _icon_confidence(b)
        if icon_confidence is not None:
            icons.append(_to_ref(b, icon_confidence))
        elif b.group.canonical.size_bytes > _PHOTO_MIN_BYTES:
            photos.append(_to_ref(b, _PHOTO_CONFIDENCE))
        else:
            unclassified.append(_to_ref(b, 0.0))

    logo_placements = sorted(logo_built.placements, key=lambda p: p.box.top) if logo_built else []

    return AssetCatalog(
        logo=logo, logo_placements=logo_placements, backgrounds=backgrounds,
        icons=icons, photos=photos, unclassified=unclassified, boxless_placements=boxless,
    )


_PHOTO_CONFIDENCE = 0.5


def _to_ref(b: _Built, confidence: float) -> AssetRef:
    canonical = b.group.canonical
    return AssetRef(
        part_name=canonical.name, width=canonical.width, height=canonical.height,
        size_bytes=canonical.size_bytes, has_alpha=canonical.has_alpha,
        square=_is_square(canonical.width, canonical.height),
        placements=b.placements, confidence=confidence,
    )


def _group_media_by_md5(media: list[MediaEntry]) -> list[_AssetGroup]:
    by_md5: dict[str, list[MediaEntry]] = defaultdict(list)
    for entry in media:
        by_md5[entry.md5].append(entry)

    groups = []
    for entries in by_md5.values():
        entries_sorted = sorted(entries, key=lambda e: e.name)
        groups.append(_AssetGroup(
            canonical=entries_sorted[0],
            part_names=frozenset(e.name for e in entries_sorted),
        ))
    return groups


def _master_parts(pkg: PptxPackage) -> list[str]:
    return sorted(
        n for n in pkg.names()
        if n.startswith("ppt/slideMasters/slideMaster") and n.endswith(".xml")
    )


def _layout_master_image_refs(pkg: PptxPackage, layouts: list[LayoutEntry]) -> set[str]:
    """Имена медиа-частей, на которые ссылается хотя бы один лейаут или
    мастер отношением типа `image` — надёжный признак фирменного ассета
    (разведка брифа, п.3). `layout.asset_refs` уже посчитан каталогом
    лейаутов (Task 5) и включает и декоративные картинки, и картинку фона
    лейаута — переиспользуем его вместо повторного `pkg.related()` по
    каждому лейауту. Для мастеров такого готового поля нет (у `LayoutEntry`
    нет отдельной записи на мастер), поэтому мастера опрашиваются здесь
    напрямую — их всего один-два на файл, дёшево."""
    refs: set[str] = set()
    for layout in layouts:
        refs.update(layout.asset_refs)
    for master_part in _master_parts(pkg):
        refs.update(pkg.related(master_part, "image"))
    return refs


def _picture_target(element, rels: dict[str, str]) -> str | None:
    blip_fill = element.find(qn("p:blipFill"))
    blip = blip_fill.find(qn("a:blip")) if blip_fill is not None else None
    rid = blip.get(qn("r:embed")) if blip is not None else None
    return rels.get(rid) if rid else None


def _bg_image_target(root, rels: dict[str, str]) -> str | None:
    """Картинка фона, заданная через `p:cSld/p:bg/p:bgPr/a:blipFill` —
    НЕ шейп, `walk_shapes` её не видит вовсе (обходит только `p:spTree`).
    На контрольном ЛЦТ2026 фон мастера (3840×2160, разведка п.11) задан
    именно так, а не картинкой-шейпом на весь слайд, как у VK Tech
    (`image7.png`, см. отчёт задачи) — без этой ветки фон контрольного
    файла остался бы вовсе не найден классификатором ниже."""
    c_sld = root.find(qn("p:cSld"))
    bg = c_sld.find(qn("p:bg")) if c_sld is not None else None
    bg_pr = bg.find(qn("p:bgPr")) if bg is not None else None
    blip_fill = bg_pr.find(qn("a:blipFill")) if bg_pr is not None else None
    blip = blip_fill.find(qn("a:blip")) if blip_fill is not None else None
    rid = blip.get(qn("r:embed")) if blip is not None else None
    return rels.get(rid) if rid else None


def _collect_placements(
    pkg: PptxPackage, canvas: Canvas, layouts: list[LayoutEntry], name_to_group: dict[str, _AssetGroup],
) -> tuple[dict[int, list[Placement]], int]:
    """Обходит слайды, лейауты и мастера и собирает `Placement` для каждого
    ассета — и через шейпы-картинки (`p:pic`), и через фон `p:bg`
    (см. `_bg_image_target`). Возвращает размещения по `id(_AssetGroup)`
    (группы — временные объекты этого вызова, `id()` как ключ корректен и
    дешевле хеширования по содержимому) и число картинок-шейпов, чей
    целевой медиа-файл резолвился, но `Box` — нет (см. докстроку
    `AssetCatalog.boxless_placements`)."""
    slide_parts = sorted(
        n for n in pkg.names() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
    )
    layout_parts = [layout.part_name for layout in layouts]
    master_parts = _master_parts(pkg)

    placements: dict[int, list[Placement]] = defaultdict(list)
    boxless = 0

    parts_with_kind = (
        [(p, "slide") for p in slide_parts]
        + [(p, "layout") for p in layout_parts]
        + [(p, "master") for p in master_parts]
    )
    for part_name, part_kind in parts_with_kind:
        root = pkg.xml(part_name)
        rels = pkg.rels(part_name)

        for ref in walk_shapes(root, canvas, include_groups=False):
            if ref.kind != "picture":
                continue
            target = _picture_target(ref.element, rels)
            group = name_to_group.get(target) if target else None
            if group is None:
                continue
            if ref.box is None:
                boxless += 1
                continue
            placements[id(group)].append(Placement(part_name=part_name, part_kind=part_kind, box=ref.box))

        if part_kind == "slide":
            continue  # p:bg на слайдах вне области этой задачи (разведка п.8: фон живёт в макетах/мастере)
        bg_target = _bg_image_target(root, rels)
        group = name_to_group.get(bg_target) if bg_target else None
        if group is not None:
            placements[id(group)].append(Placement(part_name=part_name, part_kind=part_kind, box=_FULL_BLEED_BOX))

    return placements, boxless


def _is_square(width: int | None, height: int | None) -> bool:
    if not width or not height:
        return False
    ratio = width / height
    return _SQUARE_MIN <= ratio <= _SQUARE_MAX


def _background_confidence(b: _Built) -> float | None:
    """`None` — не фон. Иначе — уверенность: `1.0` за точное полноэкранное
    размещение (≥95%×95%), `0.7` за более слабую ветку (площадь >60%, но не
    обязательно полный охват по обеим осям) — та же асимметрия, что и у
    `Background.source` в layouts.py: более прямой сигнал получает более
    высокую уверенность, а не одинаковую отметку "фон/не фон".

    Порог 300 КБ применён к ОБЕИМ веткам, хотя бриф пишет его дословно
    только у второй ("файл > 300 КБ с размещением > 60% площади"). Без него
    первая ветка (только процент размещения, без порога размера) ловит
    полноэкранные СКРИНШОТЫ VK Tech (19 штук 2046×1151, разведка п.6,
    23–36 КБ каждый — плоский UI сжимается в разы лучше фото) — они
    размещены на весь лейаут, но это контент, не декоративный фон.
    Скриншот и декоративное фото различаются по назначению, не по
    метаданным, единственный доступный прокси — вес: фотографический фон
    на весь кадр из разведанных файлов ни разу не оказался легче 300 КБ.
    Число не подобрано под три файла заново — это то же число, что бриф
    уже даёт для второй ветки, применённое последовательно к первой."""
    if b.group.canonical.size_bytes <= _BG_MIN_BYTES:
        return None
    best: float | None = None
    for p in b.placements:
        if p.box.width >= _BG_FULL_BLEED_RATIO and p.box.height >= _BG_FULL_BLEED_RATIO:
            best = 1.0
        elif p.box.area > _BG_AREA_RATIO:
            best = best or 0.7
    return best


def _near_edge(box: Box) -> bool:
    center_x = box.left + box.width / 2
    center_y = box.top + box.height / 2
    return (
        center_x <= _EDGE_BAND or center_x >= 1 - _EDGE_BAND
        or center_y <= _EDGE_BAND or center_y >= 1 - _EDGE_BAND
    )


def _logo_confidence(b: _Built, layout_master_refs: set[str]) -> float | None:
    """`None` — не кандидат в логотип. Иначе — доля размещений ассета,
    попавших в окно "ширина 5–30% и центр у края" (бриф, Step 2), среди
    ВСЕХ его размещений: логотип, показанный на каждом лейауте в одном и
    том же угловом виде, получает уверенность близкую к 1.0; ассет,
    который лишь ИНОГДА встаёт похоже на логотип, а на других лейаутах
    ведёт себя иначе (декор, не бренд-знак) — низкую."""
    canonical = b.group.canonical
    if canonical.size_bytes >= _LOGO_MAX_BYTES:
        return None
    if not (b.group.part_names & layout_master_refs):
        return None
    total = len(b.placements)
    if total == 0:
        return None
    qualifying = sum(
        1 for p in b.placements
        if _LOGO_MIN_WIDTH <= p.box.width <= _LOGO_MAX_WIDTH and _near_edge(p.box)
    )
    if qualifying == 0:
        return None
    return round(qualifying / total, 3)


def _icon_confidence(b: _Built) -> float | None:
    """`None` — не иконка. Иначе — доля размещений мельче 8% ширины холста
    среди всех размещений ассета (та же логика, что у `_logo_confidence`:
    квадратный файл с альфой, который почти всегда стоит мелко, —
    увереннее, чем такой же файл, который где-то занимает половину слайда)."""
    canonical = b.group.canonical
    if not _is_square(canonical.width, canonical.height):
        return None
    if not canonical.has_alpha:
        return None
    total = len(b.placements)
    if total == 0:
        return None
    small = sum(1 for p in b.placements if p.box.width < _ICON_MAX_WIDTH)
    if small == 0:
        return None
    return round(small / total, 3)
