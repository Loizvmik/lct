"""Каталог ассетов (логотип/фон/иконки/фото) — тесты дословно из брифа Task 6
(Step 1), .superpowers/sdd/task-6-brief.md, + регрессии повторного код-ревью
(см. task-6-report.md, разделы "Находки код-ревью").

`profile_fixture` в телах тестов брифа нужен как параметр — иначе pytest не
подставит фикстуру и вызов упадёт с NameError; тот же приём, что уже
применялся в Task 3–5 (см. докстроку test_layouts.py).

Синтетика (блок после тестов брифа) нужна там, где сценария нет ни на одном
из трёх учебных файлов (второй, слабый признак фона; фон НАД содержимым;
картинка без координат; каждый из трёх исходов "логотип не найден";
несколько кандидатов в логотип; одиночная иконка без набора; смысл
уверенности фото/неклассифицированного) — тот же приём, что в
test_layouts.py (`_package_with_layout_bg` и синтетические тесты после
него): собирается минимальный .pptx-пакет напрямую из zip-частей, без
файлов из dataset/templates.
"""
from __future__ import annotations
import io
import itertools
import os
import zipfile
from dataclasses import dataclass, field

from conftest import ALL_TEMPLATES
from PIL import Image

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.template.assets import build_asset_catalog
from deckforge.template.layouts import Background, LayoutEntry


def test_logo_is_found_in_every_template(profile_fixture):
    """Логотип = маленький файл, много размещений в лейаутах, угловая позиция."""
    for name in ALL_TEMPLATES:
        catalog = profile_fixture(name).assets
        assert catalog.logo is not None, name
        assert catalog.logo.size_bytes < 200_000


def test_education_icon_set_is_recognised(profile_fixture):
    """206 изображений ровно 112×112 — иконочный сет опознаётся однозначно."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").assets
    assert len(catalog.icons) >= 100


def test_full_bleed_background_is_separated_from_icons(profile_fixture):
    """Изначальная версия сверяла `size_bytes > 100_000` у ВСЕХ фонов — это
    было следствием порога 300 КБ, который повторное код-ревью признало
    ошибочным (находка №1, см. докстроку `_background_confidence`): порог
    топил настоящие лёгкие подложки (WorkSpace `image27.png`, 18 КБ). После
    замены веса на `p:bg`/порядок отрисовки VK Tech находит фон и среди
    лёгких полноэкранных картинок (19 "скриншотов", 2046×1151, вес от ~8 КБ
    — они лежат в самом низу порядка отрисовки СВОИХ лейаутов, то есть
    структурно это и есть декоративная подложка конкретного лейаута, не
    контент поверх неё) — проверяем инвариант, который классификатор
    ДЕЙСТВИТЕЛЬНО обещает (confidence одной из двух определённых ступеней),
    а не наблюдение о весе файлов трёх учебных шаблонов."""
    catalog = profile_fixture("VK Tech шаблон.pptx").assets
    assert catalog.backgrounds
    assert all(b.confidence in (0.7, 1.0) for b in catalog.backgrounds)
    assert catalog.logo not in catalog.backgrounds
    bg_names = {b.part_name for b in catalog.backgrounds}
    icon_names = {i.part_name for i in catalog.icons}
    assert not (bg_names & icon_names)


def test_logo_placement_is_recorded_for_the_audit(profile_fixture):
    """Проверка T05 «логотип сдвинут с положенного места» сверяется с этим."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").assets
    assert catalog.logo_placements
    top = catalog.logo_placements[0]
    assert 0.0 <= top.box.left <= 0.3


# --- синтетическая инфраструктура ------------------------------------------

_RELS_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CANVAS = Canvas(width_emu=12192000, height_emu=6858000)  # 16:9, как test_layouts.py

_ids = itertools.count(2)


def _png(width: int, height: int, *, alpha: bool = False, seed: int = 0, noisy: bool = False) -> bytes:
    """PNG для синтетики. `seed` меняет цвет заливки — разные `seed` дают
    разный md5, иначе дедуп по содержимому (`_group_media_by_md5`) схлопнул
    бы "разные" синтетические картинки в один ассет. `noisy` — случайные
    пиксели вместо заливки, PNG почти не сжимается — способ получить
    контролируемо тяжёлый файл для тестов веса (см. `_photo_confidence`)."""
    mode = "RGBA" if alpha else "RGB"
    if noisy:
        channels = 4 if alpha else 3
        data = os.urandom(width * height * channels)
        img = Image.frombytes(mode, (width, height), data)
    else:
        r, g, b = (seed * 37) % 256, (seed * 59 + 17) % 256, (seed * 83 + 41) % 256
        fill = (r, g, b, 255) if alpha else (r, g, b)
        img = Image.new(mode, (width, height), fill)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pic(rid: str, x: int, y: int, cx: int, cy: int) -> str:
    i = next(_ids)
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{i}" name="pic{i}"/>'
        "<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )


def _shape(x: int = 0, y: int = 0, cx: int = 100, cy: int = 100) -> str:
    """Обычный (не картиночный) шейп — заполнитель порядка отрисовки для
    тестов `under_content` (находка код-ревью №1)."""
    i = next(_ids)
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name="sp{i}"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:sp>'
    )


def _broken_group_pic(rid: str) -> str:
    """Группа без `a:chOff`/`a:chExt` — `_read_group_frame` вернёт `None`,
    картинка внутри получит `box=None` (см. `_shape_ref`/`_walk_container`
    в ooxml/walk.py) — для теста `boxless_placements`."""
    i = next(_ids)
    inner = _pic(rid, 0, 0, 100, 100)
    return (
        f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{i}" name="g{i}"/>'
        "<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>" + inner + "</p:grpSp>"
    )


def _bg_pic(rid: str) -> str:
    return (
        "<p:bg><p:bgPr><a:blipFill>"
        f'<a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch>'
        "</a:blipFill><a:effectLst/></p:bgPr></p:bg>"
    )


def _sp_tree(shapes_xml: str) -> str:
    return (
        '<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr/>" + shapes_xml + "</p:spTree>"
    )


@dataclass
class _Part:
    shapes_xml: str = ""
    bg_xml: str = ""
    image_refs: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _rel_lines(image_refs: tuple[tuple[str, str], ...]) -> str:
    return "".join(
        f'<Relationship Id="{rid}" Type="{_REL_BASE}/image" Target="../media/{name}"/>'
        for rid, name in image_refs
    )


def _root_xml(tag: str, part: _Part) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<p:{tag} xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
        f'<p:cSld name="s">{part.bg_xml}{_sp_tree(part.shapes_xml)}</p:cSld>'
        f"</p:{tag}>"
    )


def _pkg(*, media: dict[str, bytes], layout: _Part | None = None, master: _Part | None = None,
          slides: tuple[_Part, ...] = ()) -> tuple[PptxPackage, Canvas]:
    """Минимальный синтетический .pptx: только части, которые
    `build_asset_catalog` реально читает (`pkg.media()`/`pkg.names()`/
    `pkg.xml()`/`pkg.rels()`/`pkg.related()`) — без presentation.xml/темы/
    `_rels/.rels`, которые ему не нужны (холст передаётся вызывающим кодом
    напрямую, см. сигнатуру `build_asset_catalog`)."""
    layout = layout or _Part()
    master = master or _Part()
    files: dict[str, bytes | str] = {
        "ppt/slideLayouts/slideLayout1.xml": _root_xml("sldLayout", layout),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
            f"{_RELS_HEADER}<Relationships {_RELS_NS}>" + _rel_lines(layout.image_refs) + "</Relationships>"
        ),
        "ppt/slideMasters/slideMaster1.xml": _root_xml("sldMaster", master),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
            f"{_RELS_HEADER}<Relationships {_RELS_NS}>" + _rel_lines(master.image_refs) + "</Relationships>"
        ),
    }
    for idx, part in enumerate(slides, start=1):
        files[f"ppt/slides/slide{idx}.xml"] = _root_xml("sld", part)
        files[f"ppt/slides/_rels/slide{idx}.xml.rels"] = (
            f"{_RELS_HEADER}<Relationships {_RELS_NS}>" + _rel_lines(part.image_refs) + "</Relationships>"
        )
    for name, content in media.items():
        files[f"ppt/media/{name}"] = content

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r")), _CANVAS


def _fake_layout(asset_refs: tuple[str, ...] = ()) -> LayoutEntry:
    """`LayoutEntry` собран вручную, без `build_layout_catalog`:
    `build_asset_catalog` из всего контракта Task 5 использует только
    `part_name` и `asset_refs` (см. `_layout_master_image_refs`), остальные
    поля ему не нужны — тема/грид/юзкейс для синтетики Task 6 лишние."""
    return LayoutEntry(
        layout_id="slideLayout1", part_name="ppt/slideLayouts/slideLayout1.xml", name="Тест",
        master_index=0, kind="content", kind_confidence=0.5,
        background=Background(color=None, source="lt1_fallback", luminance=None),
        is_dark=False, placeholders=[], decor_count=0, asset_refs=list(asset_refs),
    )


_W, _H = _CANVAS.width_emu, _CANVAS.height_emu


# --- находка №1: фон без порога по весу, по p:bg и по порядку отрисовки ----


def test_lightweight_full_bleed_picture_under_content_is_background():
    """Регрессия (повторное код-ревью, находка №1): раньше порог 300 КБ
    топил лёгкую полноэкранную подложку в "не классифицировано" — WorkSpace
    `image27.png`, 18830 Б, 96%×103% площади. Синтетика: крошечный (~150 Б)
    полноэкранный PNG, лежащий ПЕРВЫМ среди трёх шейпов лейаута (под
    остальным содержимым, `under_content=True`) — обязан стать фоном
    независимо от веса."""
    img = _png(4, 4, seed=1)
    part = _Part(
        shapes_xml=_pic("rId1", 0, 0, _W, _H) + _shape() + _shape(),
        image_refs=(("rId1", "bg.png"),),
    )
    pkg, canvas = _pkg(media={"bg.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    assert len(img) < 1000  # проверка постановки: файл действительно лёгкий
    names = {b.part_name for b in catalog.backgrounds}
    assert "ppt/media/bg.png" in names
    bg = next(b for b in catalog.backgrounds if b.part_name == "ppt/media/bg.png")
    assert bg.confidence == 1.0


def test_full_bleed_picture_over_content_is_not_background():
    """Находка №1 (негативная часть): полноэкранная картинка, нарисованная
    ПОСЛЕДНЕЙ (поверх остальных двух шейпов — `under_content=False`), это
    контент/скриншот, не декоративная подложка — не должна попадать в
    `backgrounds`, даже при 100%×100% размещении."""
    img = _png(4, 4, seed=2)
    part = _Part(
        shapes_xml=_shape() + _shape() + _pic("rId1", 0, 0, _W, _H),
        image_refs=(("rId1", "top.png"),),
    )
    pkg, canvas = _pkg(media={"top.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    bg_names = {b.part_name for b in catalog.backgrounds}
    assert "ppt/media/top.png" not in bg_names


def test_background_area_branch_gives_weaker_confidence():
    """Вторая, более слабая ветка (площадь >60%, но не полный охват по
    обеим осям) не покрыта ни одним из четырёх тестов брифа — только
    полноэкранная ветка. Картинка 72%×88% (area≈0.634>0.6, но
    width<0.95) под остальным содержимым — фон с `confidence=0.7`, не 1.0."""
    cx, cy = int(_W * 0.72), int(_H * 0.88)
    img = _png(4, 4, seed=3)
    part = _Part(
        shapes_xml=_pic("rId1", 0, 0, cx, cy) + _shape() + _shape(),
        image_refs=(("rId1", "weak.png"),),
    )
    pkg, canvas = _pkg(media={"weak.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    bg = next(b for b in catalog.backgrounds if b.part_name == "ppt/media/weak.png")
    assert bg.confidence == 0.7


def test_bg_reference_is_background_regardless_of_size_or_zorder():
    """`p:bg` — фон по определению формата OOXML: не измеряется размещением
    и порядком отрисовки вовсе, `box` фиксирован (`_FULL_BLEED_BOX`).
    Крошечная картинка, заданная через `p:bg` лейаута — фон с
    confidence=1.0 без всяких дополнительных условий."""
    img = _png(2, 2, seed=4)
    part = _Part(bg_xml=_bg_pic("rId1"), image_refs=(("rId1", "bgref.png"),))
    pkg, canvas = _pkg(media={"bgref.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    bg = next(b for b in catalog.backgrounds if b.part_name == "ppt/media/bgref.png")
    assert bg.confidence == 1.0


# --- находка №2: p:bg на слайдах -------------------------------------------


def test_slide_level_bg_is_detected_as_background():
    """Регрессия (находка №2): контрольный ЛЦТ2026 задаёт фон `p:bg` прямо
    на слайдах (не только в лейауте/мастере) — раньше `_collect_placements`
    пропускал `p:bg` слайдов явным `continue`. Синтетика: `p:bg` слайда без
    какого-либо фона лейаута/мастера — обязан быть найден."""
    img = _png(2, 2, seed=5)
    slide = _Part(bg_xml=_bg_pic("rId1"), image_refs=(("rId1", "slidebg.png"),))
    pkg, canvas = _pkg(media={"slidebg.png": img}, slides=(slide,))
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    names = {b.part_name for b in catalog.backgrounds}
    assert "ppt/media/slidebg.png" in names
    bg = next(b for b in catalog.backgrounds if b.part_name == "ppt/media/slidebg.png")
    assert bg.confidence == 1.0
    placement = bg.placements[0]
    assert placement.part_kind == "slide"
    assert placement.from_bg is True


# --- картинка без координат («boxless_placements») -------------------------


def test_boxless_picture_is_counted_not_placed():
    """«Картинка без координат для классификации по размещению не годится —
    считай такие отдельно» (см. докстроку `AssetCatalog.boxless_placements`)
    — до сих пор непокрыто ни одним тестом. Картинка внутри сломанной
    группы (нет `a:chOff`/`a:chExt`) не получает `Box` — учитывается в
    `boxless_placements`, но не появляется ни в одном из списков каталога."""
    img = _png(4, 4, seed=6)
    part = _Part(shapes_xml=_broken_group_pic("rId1"), image_refs=(("rId1", "broken.png"),))
    pkg, canvas = _pkg(media={"broken.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    assert catalog.boxless_placements == 1
    all_names = {
        r.part_name
        for r in [catalog.logo] if r
    } | {r.part_name for r in catalog.backgrounds + catalog.icons + catalog.photos + catalog.unclassified}
    # boxless-картинка резолвилась (target найден), но без Box размещений
    # у неё нет вовсе — она всё равно попадёт в unclassified (0 размещений,
    # маленький файл), просто без единого Placement.
    unclassified = next(r for r in catalog.unclassified if r.part_name == "ppt/media/broken.png")
    assert unclassified.placements == []


# --- находка №3: причина у logo=None и у unclassified -----------------------


def test_logo_reason_when_no_candidates_at_all():
    """Первый из трёх исходов: в шаблоне нет ни одного маленького файла,
    привязанного к лейауту/мастеру — предпосылок для логотипа нет вовсе."""
    img = _png(4, 4, seed=7)
    part = _Part(shapes_xml=_pic("rId1", 100, 100, 500000, 500000))
    pkg, canvas = _pkg(media={"solo.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout(asset_refs=())])
    assert catalog.logo is None
    assert catalog.logo_reason is not None
    assert "нет" in catalog.logo_reason and "вовсе" in catalog.logo_reason


def test_logo_reason_when_candidates_fail_feature_window():
    """Второй исход: файл маленький и привязан к лейауту, но размещён по
    центру и крупно (не проходит окно ширины 5–30%/края) — кандидат был,
    но не прошёл признак."""
    img = _png(4, 4, seed=8)
    cx, cy = int(_W * 0.5), int(_H * 0.5)
    x, y = (_W - cx) // 2, (_H - cy) // 2  # по центру — далеко от края
    part = _Part(shapes_xml=_pic("rId1", x, y, cx, cy), image_refs=(("rId1", "center.png"),))
    pkg, canvas = _pkg(media={"center.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout(asset_refs=("ppt/media/center.png",))])
    assert catalog.logo is None
    assert catalog.logo_reason is not None
    assert "малы" in catalog.logo_reason or "не встал" in catalog.logo_reason


def test_logo_reason_when_candidates_tie():
    """Третий исход: два разных маленьких кандидата, оба привязаны к
    лейауту, оба проходят окно ширины/края с ОДИНАКОВЫМ числом размещений —
    явного победителя нет, `logo=None` с объясняющей причиной, а не
    случайный выбор по имени партa."""
    img_a = _png(4, 4, seed=9)
    img_b = _png(4, 4, seed=10)
    cx, cy = int(_W * 0.1), int(_H * 0.1)
    corner = (0, 0)
    shapes = _pic("rId1", corner[0], corner[1], cx, cy) + _pic("rId2", corner[0], corner[1], cx, cy)
    part = _Part(shapes_xml=shapes, image_refs=(("rId1", "a.png"), ("rId2", "b.png")))
    pkg, canvas = _pkg(media={"a.png": img_a, "b.png": img_b}, layout=part)
    layout = _fake_layout(asset_refs=("ppt/media/a.png", "ppt/media/b.png"))
    catalog = build_asset_catalog(pkg, canvas, [layout])
    assert catalog.logo is None
    assert catalog.logo_reason is not None
    assert "делят максимум" in catalog.logo_reason


def test_logo_chosen_by_max_placements_among_several_candidates():
    """Несколько кандидатов проходят порог, но число размещений у них
    РАЗНОЕ — побеждает тот, у кого больше, и `logo_reason` пуст (логотип
    найден, объяснять нечего)."""
    img_a = _png(4, 4, seed=11)
    img_b = _png(4, 4, seed=12)
    cx, cy = int(_W * 0.1), int(_H * 0.1)
    shapes = (
        _pic("rId1", 0, 0, cx, cy) * 1
        + _pic("rId2", 0, 0, cx, cy)
        + _pic("rId2", 0, 0, cx, cy)
        + _pic("rId2", 0, 0, cx, cy)
    )
    part = _Part(shapes_xml=shapes, image_refs=(("rId1", "a.png"), ("rId2", "b.png")))
    pkg, canvas = _pkg(media={"a.png": img_a, "b.png": img_b}, layout=part)
    layout = _fake_layout(asset_refs=("ppt/media/a.png", "ppt/media/b.png"))
    catalog = build_asset_catalog(pkg, canvas, [layout])
    assert catalog.logo is not None
    assert catalog.logo.part_name == "ppt/media/b.png"
    assert catalog.logo_reason is None


# --- находка №4: связный набор иконок ---------------------------------------


def test_icon_set_membership_gets_full_confidence():
    """Три РАЗНЫХ файла (разный md5, иначе дедуп схлопнёт их в один ассет)
    точно одного пиксельного размера (40×40), квадратные, с альфой,
    размещённые всегда мелко — связный набор (`_ICON_SET_MIN=3`), полная,
    не ослабленная уверенность."""
    media = {f"icon{i}.png": _png(40, 40, alpha=True, seed=i) for i in range(3)}
    cx, cy = 400000, 400000  # 400000/12192000 ≈ 3.3% < 8% порога иконки
    shapes = "".join(_pic(f"rId{i}", 0, 0, cx, cy) for i in range(3))
    refs = tuple((f"rId{i}", f"icon{i}.png") for i in range(3))
    part = _Part(shapes_xml=shapes, image_refs=refs)
    pkg, canvas = _pkg(media=media, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    icon_names = {i.part_name: i for i in catalog.icons}
    for name in media:
        ref = icon_names[f"ppt/media/{name}"]
        assert ref.confidence == 1.0  # одно размещение, все квалифицирующие


def test_standalone_square_alpha_gets_weak_confidence_not_full():
    """Регрессия (находка №4): одиночная квадратная картинка с альфой
    (например, круглый аватар в отзыве) БЕЗ набора того же пиксельного
    размера — формально проходит признак брифа (квадрат+альфа+мелкое
    размещение) один в один, но не должна получать такую же уверенность,
    как подтверждённый набор. Уникальный размер (77×77, ни с кем не
    делится) — уверенность прижата к слабой зоне."""
    img = _png(77, 77, alpha=True, seed=20)
    cx, cy = 400000, 400000
    part = _Part(shapes_xml=_pic("rId1", 0, 0, cx, cy), image_refs=(("rId1", "avatar.png"),))
    pkg, canvas = _pkg(media={"avatar.png": img}, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    ref = next(i for i in catalog.icons if i.part_name == "ppt/media/avatar.png")
    assert ref.confidence < 0.5  # ослаблено, не спутать с уверенным набором


def test_icon_high_resolution_set_member_is_not_penalized_by_resolution():
    """«Не выбрасывает иконки высокого разрешения» (прямое требование
    задачи): набор из трёх файлов 1024×1024 (реалистичный размер экспорта
    icon-библиотеки), размещённых мелко — классифицируются как иконки с
    полной уверенностью, признак — набор, а не разрешение файла."""
    media = {f"hires{i}.png": _png(1024, 1024, alpha=True, seed=30 + i) for i in range(3)}
    cx, cy = 400000, 400000
    shapes = "".join(_pic(f"rId{i}", 0, 0, cx, cy) for i in range(3))
    refs = tuple((f"rId{i}", f"hires{i}.png") for i in range(3))
    part = _Part(shapes_xml=shapes, image_refs=refs)
    pkg, canvas = _pkg(media=media, layout=part)
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    icon_names = {i.part_name for i in catalog.icons}
    for name in media:
        assert f"ppt/media/{name}" in icon_names


# --- находка №6: смысл confidence у фото и содержимое unclassified ---------


def test_photo_confidence_scales_with_size_above_threshold():
    """Раньше `photos` всегда получали `confidence=0.5` — потребитель не мог
    ранжировать фотографии по надёжности (находка №6). Два несвязанных
    несжимаемых (случайный шум) файла — оба заведомо не квадратные
    (проверка иконки отсекается первой же проверкой square), оба тяжелее
    `_PHOTO_MIN_BYTES` (50 000 Б) — но один заметно тяжелее другого,
    и его confidence обязан быть строго больше."""
    light = _png(140, 130, noisy=True, seed=40)   # ~54 КБ сырых данных
    heavy = _png(400, 300, noisy=True, seed=41)   # ~360 КБ сырых данных
    assert 50_000 < len(light) < 90_000
    assert len(heavy) > 200_000
    pkg, canvas = _pkg(media={"light.png": light, "heavy.png": heavy}, layout=_Part())
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    photo_by_name = {p.part_name: p for p in catalog.photos}
    assert "ppt/media/light.png" in photo_by_name
    assert "ppt/media/heavy.png" in photo_by_name
    light_ref = photo_by_name["ppt/media/light.png"]
    heavy_ref = photo_by_name["ppt/media/heavy.png"]
    assert 0.0 < light_ref.confidence < 1.0
    assert heavy_ref.confidence == 1.0
    assert heavy_ref.confidence > light_ref.confidence


def test_unclassified_item_has_explanatory_reason():
    """«Каждый элемент в "не классифицировано" приходит без причины» —
    находка №3. Маленький (< 50 КБ), не квадратный, без альфы файл — не
    иконка (не квадрат) и не фото (легче порога) — `unclassified` с
    непустой человекочитаемой причиной."""
    img = _png(10, 4, alpha=False, seed=50)
    assert len(img) <= 50_000
    pkg, canvas = _pkg(media={"tiny.png": img}, layout=_Part())
    catalog = build_asset_catalog(pkg, canvas, [_fake_layout()])
    ref = next(r for r in catalog.unclassified if r.part_name == "ppt/media/tiny.png")
    assert ref.confidence == 0.0
    assert ref.reason
    assert "не квадратная" in ref.reason
    assert "не фото" in ref.reason
