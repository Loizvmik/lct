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
(0.9–1.1 для квадрата, 5–30% для ширины логотипа, 8% для иконки, 95%/60%
для фона, 50 КБ для фото) — литерал брифа, не подобраны по трём файлам.
Единственное значение, которое пришлось интерпретировать самостоятельно,
а не переписать дословно, — «четверть у края» логотипа (см. `_EDGE_BAND`
и её докстроку), откалибровано не по ответам трёх файлов, а обосновано
отдельно на месте.

**Порог 300 КБ у фона убран (повторное код-ревью, находка №1).** Вес файла
оказался слабым признаком: WorkSpace `image27.png` (600×362, с альфой,
18830 Б) — настоящая полноэкранная подложка (96%×103% площади слайда), но
падала в "не классифицировано" из-за нижнего порога по весу. Вес заменён
на два признака, которые действительно отличают фон от контента, — оба по
структуре документа, не по наблюдению за тремя файлами: (1) картинка,
на которую ссылается `p:bg` макета/мастера/слайда, — фон по определению
формата OOXML, без всяких порогов (см. `_bg_image_target`, `Placement.
from_bg`); (2) картинка-шейп почти во весь слайд, лежащая под остальным
содержимым в порядке отрисовки (см. `Placement.under_content`), — фон;
такая же картинка НАД содержимым — не фон (это разводит декоративную
подложку и полноэкранный скриншот-контент, которые раньше пытался
развести вес). См. `_background_confidence`.

**Фон на самом слайде (не только в макете/мастере) — находка №2.** Обход
`p:bg` раньше применялся только к макетам и мастеру («вне области этой
задачи» — предположение, зашитое по трём учебным файлам). Контрольный
ЛЦТ2026 его опровергает: шесть картинок заданы через `p:bg` прямо на
слайдах. Обход `p:bg` теперь одинаков для слайдов/макетов/мастера.

**Причина у пустого результата — находка №3.** `AssetCatalog.logo_reason`
и `AssetRef.reason` объясняют, почему логотип не найден или почему
конкретный ассет попал в `unclassified`, — по аналогии с `UnresolvedColor`
(ooxml/color.py) и `ThemeFallback` (usage.py): пустой результат — не
ошибка разбора, но должен быть виден человеку с причиной, а не выглядеть
неотличимым от "не искали".

**Признак иконки учитывает связный набор размеров — находка №4.** Прежде
квадрат+альфа+мелкое размещение пропускал бы и одиночную квадратную
фотографию с альфой (например, круглый аватар в отзыве). Иконки почти
никогда не приходят поодиночке: несколько файлов с ТОЧНО совпадающими
пиксельными размерами — надёжный признак набора (подтверждён на двух
файлах: 206×112×112 у Education, 667×48×48 на контрольном). Членство в
таком наборе поднимает уверенность; одиночный кандидат без набора
получает уверенность, прижатую к слабой зоне (см. `_ICON_STANDALONE_FACTOR`),
а не отбрасывается — среди одиночных попадаются настоящие иконки
нестандартного размера (VK Tech: 25 из 36 иконок не входят ни в один
трёхэлементный набор, но это не аватарки, а мелкие декоративные PNG с
альфой, размещённые стабильно мелко).

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
# Сколько файлов с ТОЧНО совпадающими пиксельными размерами считаются
# "набором" (находка код-ревью №4, см. докстроку модуля) — не литерал брифа,
# изобретено заново: 3 — минимум, при котором совпадение размеров пикселей
# у разных файлов перестаёт правдоподобно объясняться случайностью (два
# независимых фото одного разрешения — рядовое совпадение, три и больше —
# нет). Подтверждено на двух файлах с большим запасом (206 и 667), но само
# число 3 — не подогнано под них, они бы прошли и при 3, и при 30.
_ICON_SET_MIN = 3
# Множитель уверенности для иконки-одиночки (не входит в набор ни с одним
# другим файлом её пиксельного размера) — не 0 (полностью отбросить такие
# кандидаты нельзя, среди них попадаются настоящие иконки нестандартного
# размера, см. докстроку модуля) и не 1 (это ослабленный сигнал: без
# подтверждения соседями по набору квадрат+альфа+мелкое размещение — то же
# самое, чем формально является и круглый аватар в отзыве). 0.4 гарантированно
# держит итоговую уверенность одиночки ниже 0.5 при любой доле квалифицирующих
# размещений (макс. доля 1.0 × 0.4 = 0.4) — консервативнее, чем оставлять
# случай неразличимым от уверенного набора.
_ICON_STANDALONE_FACTOR = 0.4

_PHOTO_MIN_BYTES = 50_000
# Насыщение уверенности фото (находка №6, см. докстроку AssetRef.confidence):
# файл тяжелее порога в это число раз даёт confidence=1.0; на самом пороге —
# 0.25. Не подобрано по файлам — просто делает шкалу монотонной и ограниченной.
_PHOTO_CONFIDENCE_SATURATION = 4

_FULL_BLEED_BOX = Box(left=0.0, top=0.0, width=1.0, height=1.0)


@dataclass(frozen=True)
class Placement:
    """Одно размещение ассета на слайде/лейауте/мастере.

    `part_kind` — где стоит шейп ("slide"/"layout"/"master"), не откуда
    унаследован фон: у фона, заданного через `p:bg` (см. `_bg_image_target`),
    `part_kind` — часть, которая САМА объявляет `p:bg` (слайд, лейаут или
    мастер — находка код-ревью №2 добавила слайд к этому списку: раньше
    `p:bg` слайда не проверялся вовсе, см. докстроку модуля), даже если у
    лейаута своего `p:bg` нет и он наследует фон мастера — в этом случае
    размещение попадает только на мастер, лейаут его не повторяет (иначе
    один и тот же фон-фото давал бы размещение на КАЖДОМ лейауте, который
    его наследует, и искусственно раздувал бы счётчик).

    `from_bg` — `True`, если это размещение получено из `p:bg` (см.
    `_bg_image_target`), а не из шейпа-картинки (`p:pic`). Фон, заданный
    через `p:bg`, — фон по определению формата OOXML, без всяких порогов
    (находка код-ревью №1, см. докстроку модуля) — `box` у такого
    размещения всегда `_FULL_BLEED_BOX`, не измеренный размер.

    `under_content` — только для НЕ-`p:bg` (шейповых) размещений: `True`,
    если в документном порядке этой части не меньше шейпов идёт ПОСЛЕ этой
    картинки (значит, рисуются поверх неё), чем ДО неё (см. `walk_shapes`:
    порядок — z-order снизу вверх, предзаказный). Различает декоративную
    подложку (лежит под содержимым) и полноэкранную картинку-контент
    (лежит НАД остальным, например скриншот, которому ничего не полагается
    поверх) — находка код-ревью №1: вес это не различал, порядок отрисовки
    различает. Для `p:bg`-размещений неприменимо, оставлено `False`.
    """
    part_name: str
    part_kind: str
    box: Box
    from_bg: bool = False
    under_content: bool = False


@dataclass(frozen=True)
class AssetRef:
    """Контракт интерфейса брифа (Step 1) дословно + `confidence`/`reason`
    сверху — единообразие с соседними модулями (`LayoutEntry.kind_confidence`
    и т.п., см. «Требования к работе» брифа).

    `confidence` — мера уверенности в присвоенной категории, не «уверенность,
    что это вообще картинка», и её СМЫСЛ разный по категориям (находка
    код-ревью №6 — здесь не унифицирован, а назван по месту, раз унификация
    стёрла бы разные по природе сигналы в один):
    - фон/логотип/иконка — доля квалифицирующих размещений среди всех
      размещений ассета (см. `_background_confidence`/`_logo_confidence`/
      `_icon_confidence`): ассет, который ВСЕГДА ведёт себя как заявленная
      категория, увереннее, чем тот, что лишь иногда так выглядит;
    - `photos` — монотонная функция веса файла относительно
      `_PHOTO_MIN_BYTES` (см. `_photo_confidence`): у категории «всё
      остальное крупнее порога» нет позитивного признака, который можно
      измерить долей размещений, но ранжировать её элементы по надёжности
      всё равно нужно (находка №6: раньше было фиксированных `0.5` для
      всех — потребитель не мог отличить файл ровно на пороге от файла в
      разы тяжелее); смысл значения здесь — «насколько уверенно это
      существенный файл», не «доля квалифицирующих размещений»;
    - `unclassified` — всегда `0.0` (по определению ни один классификатор
      не сработал, ранжировать нечего).

    `reason` — только для `unclassified` (находка код-ревью №3, по аналогии
    с `UnresolvedColor`/`ThemeFallback`, см. докстроку модуля): человекочитаемый
    текст, какого признака не хватило для каждой из проверенных категорий.
    `None` у всех остальных категорий — там причина совпадает с самой
    категорией и отдельного объяснения не требует.
    """
    part_name: str
    width: int | None
    height: int | None
    size_bytes: int
    has_alpha: bool
    square: bool
    placements: list[Placement] = field(default_factory=list)
    confidence: float = 0.0
    reason: str | None = None


@dataclass
class AssetCatalog:
    """Контракт интерфейса брифа (Step 1) дословно + `boxless_placements` +
    `logo_reason`.

    `boxless_placements` — не в перечне брифа, добавлено по прямому
    требованию задачи: «картинка без координат для классификации по
    размещению не годится — считай такие отдельно и покажи их число в
    результате» (см. докстроку `ShapeRef.box` в ooxml/walk.py и
    `_collect_placements`). Число шейпов-картинок, у которых удалось
    резолвить целевой медиа-файл, но не удалось вычислить `Box`
    (сломанная группа выше по дереву, см. `_read_group_frame`) — они не
    участвуют ни в одном списке ниже.

    `logo_reason` — заполнено, только когда `logo is None` (находка
    код-ревью №3, см. докстроку модуля): почему логотип не найден, один из
    трёх исходов — кандидатов не было вовсе (в шаблоне нет маленького
    файла, на который ссылается макет/мастер); кандидаты были, но ни один
    не прошёл окно ширины/края; кандидатов было несколько прошедших порог,
    но они делят максимум по числу размещений — явного победителя нет
    (см. `build_asset_catalog`). `None`, когда логотип найден — причина
    совпадает с самим найденным `AssetRef`.
    """
    logo: AssetRef | None
    logo_placements: list[Placement]
    backgrounds: list[AssetRef]
    icons: list[AssetRef]
    photos: list[AssetRef]
    unclassified: list[AssetRef]
    boxless_placements: int = 0
    logo_reason: str | None = None


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
    logo_reason: str | None = None
    # Кандидатский пул ДО проверки окна ширины/края (те же два первых гейта,
    # что и внутри `_logo_confidence`) — нужен отдельно от `qualifying`,
    # чтобы различить «кандидатов не было вовсе» и «были, но не прошли
    # признак» (находка код-ревью №3, см. докстроку модуля).
    raw_candidates = [
        b for b in rest
        if b.group.canonical.size_bytes < _LOGO_MAX_BYTES and (b.group.part_names & layout_master_refs)
    ]
    qualifying = [b for b in raw_candidates if _logo_confidence(b, layout_master_refs) is not None]
    if not raw_candidates:
        logo_reason = (
            f"нет ни одного файла < {_LOGO_MAX_BYTES // 1000} КБ, на который ссылается "
            "макет или мастер — предпосылок для логотипа в шаблоне нет вовсе"
        )
    elif not qualifying:
        logo_reason = (
            f"{len(raw_candidates)} файл(ов) малы и привязаны к макету/мастеру, но "
            f"ни один не встал шириной {_LOGO_MIN_WIDTH:.0%}–{_LOGO_MAX_WIDTH:.0%} холста "
            "с центром у края хотя бы на одном размещении"
        )
    else:
        # «Число размещений максимально» (бриф, Step 2) — сортировка по
        # ВСЕМ размещениям ассета, не только по тем, что попали в
        # квалифицирующее окно ширины/края (см. проверку на разведанных
        # файлах в отчёте задачи: и у WorkSpace, и у Education победитель
        # по этому критерию совпал с реальным логотипом).
        qualifying.sort(key=lambda b: (-len(b.placements), b.group.canonical.name))
        top_count = len(qualifying[0].placements)
        tied = [b for b in qualifying if len(b.placements) == top_count]
        if len(tied) > 1:
            # Находка код-ревью №3: раньше тай-брейк по имени партa ВСЕГДА
            # выбирал кого-то, даже когда число размещений у лидеров равно —
            # то есть ничего не «выделило» победителя, выбор был случайным
            # относительно признака брифа. Честнее сказать «не смогли
            # выбрать», чем выдать произвольного из равных.
            logo_reason = (
                f"{len(qualifying)} кандидатов прошли порог ширины/края, но "
                f"{len(tied)} из них делят максимум по числу размещений "
                f"({top_count}) — явного победителя нет"
            )
        else:
            logo_built = qualifying[0]
            logo = _to_ref(logo_built, _logo_confidence(logo_built, layout_master_refs))

    # Карта "пиксельный размер → сколько РАЗНЫХ ассетов имеют его точно" —
    # признак связного набора иконок (находка код-ревью №4, см. докстроку
    # модуля и `_icon_confidence`). Считается по всему `rest` за вычетом
    # логотипа: не имеет смысла позволять логотипу (уже классифицированному)
    # искусственно расширять набор одинаковых размеров у соседних картинок.
    size_set_counts: dict[tuple[int, int], int] = defaultdict(int)
    for b in rest:
        if b is logo_built:
            continue
        w, h = b.group.canonical.width, b.group.canonical.height
        if w and h:
            size_set_counts[(w, h)] += 1

    icons: list[AssetRef] = []
    photos: list[AssetRef] = []
    unclassified: list[AssetRef] = []
    for b in rest:
        if b is logo_built:
            continue
        icon_confidence = _icon_confidence(b, size_set_counts)
        if icon_confidence is not None:
            icons.append(_to_ref(b, icon_confidence))
        elif b.group.canonical.size_bytes > _PHOTO_MIN_BYTES:
            photos.append(_to_ref(b, _photo_confidence(b.group.canonical.size_bytes)))
        else:
            unclassified.append(_to_ref(b, 0.0, _unclassified_reason(b)))

    logo_placements = sorted(logo_built.placements, key=lambda p: p.box.top) if logo_built else []

    return AssetCatalog(
        logo=logo, logo_placements=logo_placements, backgrounds=backgrounds,
        icons=icons, photos=photos, unclassified=unclassified, boxless_placements=boxless,
        logo_reason=logo_reason,
    )


def _photo_confidence(size_bytes: int) -> float:
    """Монотонная, ограниченная `1.0` (находка код-ревью №6, см. докстроку
    `AssetRef.confidence`): файл на самом пороге `_PHOTO_MIN_BYTES` даёт
    `0.25`, тяжелее в `_PHOTO_CONFIDENCE_SATURATION` раз и больше — `1.0`."""
    return round(min(1.0, size_bytes / (_PHOTO_MIN_BYTES * _PHOTO_CONFIDENCE_SATURATION)), 3)


def _unclassified_reason(b: _Built) -> str:
    """Текст для `AssetRef.reason` (находка код-ревью №3, см. докстроку
    модуля): какого именно признака не хватило и иконке, и фото — элемент
    долетает до `unclassified`, только провалив ОБЕ проверки."""
    canonical = b.group.canonical
    if canonical.width is None or canonical.height is None:
        icon_miss = "нет пиксельных размеров файла (заголовок не прочитан)"
    elif not _is_square(canonical.width, canonical.height):
        icon_miss = f"не квадратная ({canonical.width}×{canonical.height})"
    elif not canonical.has_alpha:
        icon_miss = "нет альфа-канала"
    elif not b.placements:
        icon_miss = "нет размещений на слайдах/лейаутах/мастере"
    elif not any(p.box.width < _ICON_MAX_WIDTH for p in b.placements):
        icon_miss = f"ни одно размещение не мельче {_ICON_MAX_WIDTH:.0%} ширины холста"
    else:
        icon_miss = "формально проходит как иконка (не должно случаться)"  # см. _icon_confidence
    return (
        f"не иконка: {icon_miss}; не фото: вес {canonical.size_bytes} Б "
        f"≤ порога {_PHOTO_MIN_BYTES} Б"
    )


def _to_ref(b: _Built, confidence: float, reason: str | None = None) -> AssetRef:
    canonical = b.group.canonical
    return AssetRef(
        part_name=canonical.name, width=canonical.width, height=canonical.height,
        size_bytes=canonical.size_bytes, has_alpha=canonical.has_alpha,
        square=_is_square(canonical.width, canonical.height),
        placements=b.placements, confidence=confidence, reason=reason,
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
    файла остался бы вовсе не найден классификатором ниже.

    Читает СВОЙ `p:bg` части (слайда/лейаута/мастера, кто бы ни был
    `root`) — не резолвит наследование сама (см. докстроку `Placement`):
    у слайда/лейаута без собственного `p:bg` вернёт `None`, и вызывающий
    код честно не создаст размещения на этой части, а не подставит фон
    родителя задним числом. Находка код-ревью №2 (см. докстроку модуля):
    раньше эта функция вызывалась только для лейаутов/мастера — контрольный
    файл задаёт фон и НА САМИХ слайдах (`p:bg` слайда — то же самое поле
    формата, что и у лейаута/мастера, разница только в том, ЧЬЯ это часть),
    поэтому `_collect_placements` теперь опрашивает её для всех трёх видов
    частей одинаково."""
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
    (см. `_bg_image_target`, одинаково для всех трёх видов частей —
    находка код-ревью №2, см. докстроку модуля). Возвращает размещения по
    `id(_AssetGroup)` (группы — временные объекты этого вызова, `id()` как
    ключ корректен и дешевле хеширования по содержимому) и число
    картинок-шейпов, чей целевой медиа-файл резолвился, но `Box` — нет
    (см. докстроку `AssetCatalog.boxless_placements`)."""
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

        # Материализуется один раз — нужен и порядковый индекс каждой
        # картинки среди ВСЕХ шейпов части (для `under_content`, находка
        # код-ревью №1, см. докстроку `Placement`), и общее число шейпов.
        shapes = list(walk_shapes(root, canvas, include_groups=False))
        total_shapes = len(shapes)
        for idx, ref in enumerate(shapes):
            if ref.kind != "picture":
                continue
            target = _picture_target(ref.element, rels)
            group = name_to_group.get(target) if target else None
            if group is None:
                continue
            if ref.box is None:
                boxless += 1
                continue
            # «Под остальным содержимым» — по крайней мере столько же
            # шейпов рисуется ПОСЛЕ этой картинки (сверху, документный
            # порядок — z-order снизу вверх), сколько ДО неё. Полноэкранная
            # картинка НАД содержимым (например, скриншот, вставленный
            # последним) это условие не пройдёт — намеренно, это и есть
            # разграничение "фон/контент" находки №1.
            under_content = idx * 2 <= total_shapes - 1
            placements[id(group)].append(Placement(
                part_name=part_name, part_kind=part_kind, box=ref.box, under_content=under_content,
            ))

        bg_target = _bg_image_target(root, rels)
        group = name_to_group.get(bg_target) if bg_target else None
        if group is not None:
            placements[id(group)].append(Placement(
                part_name=part_name, part_kind=part_kind, box=_FULL_BLEED_BOX, from_bg=True,
            ))

    return placements, boxless


def _is_square(width: int | None, height: int | None) -> bool:
    if not width or not height:
        return False
    ratio = width / height
    return _SQUARE_MIN <= ratio <= _SQUARE_MAX


def _background_confidence(b: _Built) -> float | None:
    """`None` — не фон. Иначе — уверенность: `1.0` за фон, заданный через
    `p:bg` (фон по определению формата, см. `Placement.from_bg`) ИЛИ за
    точное полноэкранное размещение картинкой-шейпом под содержимым
    (`under_content`, ≥95%×95%); `0.7` за более слабую ветку (площадь >60%,
    но не обязательно полный охват по обеим осям, тоже под содержимым) —
    та же асимметрия, что и у `Background.source` в layouts.py: более
    прямой сигнал получает более высокую уверенность, а не одинаковую
    отметку "фон/не фон".

    Вес файла (было — жёсткий порог 300 КБ на ОБЕИХ ветках) убран как
    основной признак (находка код-ревью №1, см. докстроку модуля): он
    ошибался на реальном файле — WorkSpace `image27.png` (600×362, с
    альфой, 18830 Б, размещение 96%×103%) настоящая полноэкранная подложка,
    но 18830 Б << 300 000, порог топил её в "не классифицировано". Раньше
    вес был единственным доступным прокси, чтобы не спутать декоративный
    фон с полноэкранными СКРИНШОТАМИ VK Tech (19 штук 2046×1151, разброс
    23–1987 КБ, а не "23–36 КБ", как ошибочно утверждала прежняя версия
    этого комментария) — сейчас их разводит `under_content`: и декоративный
    фон, и скриншот равно "полноэкранные", но фон лежит ПОД остальным
    содержимым лейаута, а скриншот (если это контент, а не декоративная
    подложка конкретного лейаута) — НАД ним либо единственный шейп части."""
    if any(p.from_bg for p in b.placements):
        return 1.0
    best: float | None = None
    for p in b.placements:
        if not p.under_content:
            continue
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


def _icon_confidence(b: _Built, size_set_counts: dict[tuple[int, int], int]) -> float | None:
    """`None` — не иконка. Иначе — доля размещений мельче 8% ширины холста
    среди всех размещений ассета (та же логика, что у `_logo_confidence`:
    квадратный файл с альфой, который почти всегда стоит мелко, —
    увереннее, чем такой же файл, который где-то занимает половину слайда),
    домноженная на `_ICON_STANDALONE_FACTOR`, если файл — единственный
    своего точного пиксельного размера (находка код-ревью №4, см. докстроку
    модуля).

    Признак брифа (квадрат + альфа + мелкое размещение) сам по себе не
    смотрит на исходное разрешение файла — маленькая квадратная ФОТОГРАФИЯ
    с альфой (круглый аватар в отзыве, например) проходит его один в один
    так же, как настоящая иконка. Разрешение как порог тут не спасает: на
    контрольном файле 31 настоящая иконка экспортирована в 1290×1290 —
    порог по пикселям выкинул бы и её. Связный набор — файлов ТОЧНО того
    же пиксельного размера, что и у `b` (см. `size_set_counts`,
    `_ICON_SET_MIN`) — устойчивее: иконки почти всегда приходят пачкой
    одинаковых размеров, а случайное совпадение пиксельных размеров у
    нескольких ФОТО (тем более 3+) маловероятно. Одиночка не отбрасывается
    совсем — среди них попадаются настоящие иконки, у которых просто нет
    других файлов того же кадра (VK Tech: 25 из 36 найденных иконок ни с
    кем не делят точный пиксельный размер) — но уверенность прижимается
    к слабой зоне, а не остаётся неотличимой от подтверждённого набора."""
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
    ratio = small / total
    set_size = size_set_counts.get((canonical.width, canonical.height), 1)
    if set_size < _ICON_SET_MIN:
        ratio *= _ICON_STANDALONE_FACTOR
    return round(ratio, 3)
