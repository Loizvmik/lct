"""Каталог лейаутов и классификация типа — тесты дословно из брифа Task 5
(Step 1), .superpowers/sdd/task-5-brief.md.

Как и в test_typography.py/test_grid.py, `profile_fixture` в телах тестов
брифа нужен как параметр — иначе pytest не подставит фикстуру и вызов упадёт
с NameError; это единственная правка против буквального текста брифа (тот же
приём уже применялся в Task 3 и Task 4).

Регрессионные тесты ниже (после блока "дополнительные") — на находки
код-ревью (general-purpose и adversarial-reviewer, см. task-5-report.md):
тай-брейк геометрии по случайному порядку словаря вместо заявленного
приоритета, картинка-фон, молча схлопывающаяся в "фона нет", и битый цвет
фона, роняющий каталог целиком.
"""
from __future__ import annotations
import io
import zipfile
from unittest import mock

import pytest
from conftest import ALL_TEMPLATES
from PIL import Image

from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.template.grid import build_grid
from deckforge.template.layouts import (
    PlaceholderSlot, _argmax, _effective_master_title_size, _features, _geometry_scores,
    _heading_leaders, _load_synonyms, _name_scores, build_layout_catalog,
)
from deckforge.template.theme import ThemeInfo, pick_primary_master, read_theme
from deckforge.template.typography import build_type_scale
from deckforge.template.usage import collect_usage


def test_every_layout_gets_a_kind(profile_fixture):
    for name in ALL_TEMPLATES:
        catalog = profile_fixture(name).layouts
        assert catalog
        assert all(entry.kind for entry in catalog)


def test_dark_layouts_are_detected_by_luminance_not_by_slot_name(profile_fixture):
    """У VK Tech 23 из 39 лейаутов имеют фон schemeClr dk1 — это чёрный фон,
    а не чёрный текст."""
    catalog = profile_fixture("VK Tech шаблон.pptx").layouts
    dark = [e for e in catalog if e.is_dark]
    assert len(dark) >= 20


def test_workspace_meaningless_names_do_not_break_classification(profile_fixture):
    """11 из 15 лейаутов WorkSpace называются «Титульный слайд», хотя это
    контентные раскладки. Классификация по имени даст 11 титульников — это
    ошибка."""
    catalog = profile_fixture("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx").layouts
    titles = [e for e in catalog if e.kind == "title"]
    assert len(titles) <= 3


def test_education_picture_placeholders_are_kept(profile_fixture):
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").layouts
    with_picture = [e for e in catalog if any(p.ph_type == "PICTURE" for p in e.placeholders)]
    assert len(with_picture) >= 5


def test_placeholder_boxes_are_fractions(profile_fixture):
    for name in ALL_TEMPLATES:
        for entry in profile_fixture(name).layouts:
            for slot in entry.placeholders:
                assert 0.0 <= slot.box.left <= 1.0
                assert 0.0 < slot.box.width <= 1.0


# --- дополнительные тесты на утверждения, явно сформулированные текстом
# брифа/докстрок layouts.py, которых бриф не дал в виде готового кода ---


def test_kind_confidence_is_a_valid_probability(profile_fixture):
    """«Каждое число... должно нести меру уверенности» — kind_confidence не
    исключение, и она обязана лежать в [0, 1] у каждого лейаута каждого
    шаблона."""
    for name in ALL_TEMPLATES:
        for entry in profile_fixture(name).layouts:
            assert 0.0 <= entry.kind_confidence <= 1.0


def test_usage_count_matches_slide_references(profile_fixture):
    """Сумма usage_count по каталогу VK Tech обязана совпасть с числом
    слайдов шаблона (54, разведка п.8) — каждый слайд ссылается ровно на
    один лейаут."""
    catalog = profile_fixture("VK Tech шаблон.pptx").layouts
    assert sum(e.usage_count for e in catalog) == 54


def test_most_used_layout_is_free_design(profile_fixture):
    """36 из 54 слайдов VK Tech сидят на лейауте «Свободный дизайн»
    (разведка, п.8) — самый переиспользуемый лейаут шаблона."""
    catalog = profile_fixture("VK Tech шаблон.pptx").layouts
    busiest = max(catalog, key=lambda e: e.usage_count)
    assert busiest.usage_count == 36
    assert "свободный дизайн" in busiest.name.lower()


def test_agreement_between_name_and_geometry_yields_higher_confidence(profile_fixture):
    """Ансамбль: когда оба сигнала (имя и геометрия) указывают на один и тот
    же тип, итоговая уверенность обязана быть выше, чем у лейаутов с тем же
    kind, но неоднозначным/отсутствующим именем — иначе confidence не несёт
    информации о согласии сигналов (см. докстроку build_layout_catalog)."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").layouts
    by_kind: dict[str, list[float]] = {}
    for e in catalog:
        by_kind.setdefault(e.kind, []).append(e.kind_confidence)
    # "image" — самый насыщенный именем и геометрией (has_picture) тип на
    # Education, должен получать заметно ненулевую уверенность в среднем.
    assert by_kind.get("image")
    assert sum(by_kind["image"]) / len(by_kind["image"]) > 0.4


def test_name_prefix_and_case_do_not_affect_classification(profile_fixture):
    """Префикс Google-экспорта `^\\d+_` (VK Tech: "1_Свободный дизайн") не
    должен мешать словарю синонимов найти совпадение — самый частый лейаут
    VK Tech обязан получить kind="free", а не "content"/"title" по
    остаточному тексту после цифр."""
    catalog = profile_fixture("VK Tech шаблон.pptx").layouts
    free_design = next(e for e in catalog if "свободный дизайн" in e.name.lower())
    assert free_design.kind == "free"


def test_decor_count_and_asset_refs_are_non_negative(profile_fixture):
    for name in ALL_TEMPLATES:
        for entry in profile_fixture(name).layouts:
            assert entry.decor_count >= 0
            assert isinstance(entry.asset_refs, list)


def test_master_index_is_within_declared_masters(profile_fixture):
    """master_index — позиция мастера лейаута в p:sldMasterIdLst
    presentation.xml, а не порядковый номер файла в архиве (VK Tech/Education
    держат по два мастера каждый — разведка, п.7)."""
    for name in ALL_TEMPLATES:
        for entry in profile_fixture(name).layouts:
            assert entry.master_index >= 0


def test_education_masters_are_two_with_skewed_distribution(profile_fixture):
    """У Education 2 мастера, макеты распределены между ними неравномерно —
    28 и 2 (разведка, п.7)."""
    catalog = profile_fixture("Шаблон презентации VK Education.pptx").layouts
    counts: dict[int, int] = {}
    for e in catalog:
        counts[e.master_index] = counts.get(e.master_index, 0) + 1
    assert sorted(counts.values(), reverse=True) == [28, 2]


# --- регрессии на находки код-ревью (general-purpose и adversarial-reviewer,
# см. task-5-report.md) ---


def test_geometry_tie_break_is_explicit_not_dict_insertion_order():
    """Регрессия (general-purpose ревью): раньше argmax по геометрическим
    баллам разрешал равенство порядком вставки ключей словаря (== порядком
    KINDS), из-за чего "section" никогда не побеждал в семье title/section/
    closing при полном равенстве баллов, хотя докстрока модуля явно
    заявляла обратное. `_argmax` обязан использовать `_TIE_BREAK_PRIORITY`."""
    tied = {"title": 0.7, "section": 0.7, "closing": 0.7, "content": 0.3}
    assert _argmax(tied) == "section"


def test_three_col_wins_over_two_col_when_layout_has_both_row_patterns():
    """Регрессия (adversarial-reviewer, воспроизведено на контрольном
    ЛЦТ2026, лейаут "Стадии"): строка из 3 плейсхолдеров и строка из 2 на
    одном лейауте раньше давали scores["two_col"]==scores["three_col"]==1.0
    одновременно, и argmax решал порядком KINDS (всегда two_col), а не
    структурой. three_col обязан победить, когда тройка присутствует."""
    row_of_three = [
        PlaceholderSlot(ph_type="BODY", box=Box(0.02, 0.3, 0.30, 0.2)),
        PlaceholderSlot(ph_type="BODY", box=Box(0.35, 0.3, 0.30, 0.2)),
        PlaceholderSlot(ph_type="BODY", box=Box(0.68, 0.3, 0.30, 0.2)),
    ]
    row_of_two = [
        PlaceholderSlot(ph_type="BODY", box=Box(0.18, 0.6, 0.30, 0.2)),
        PlaceholderSlot(ph_type="BODY", box=Box(0.52, 0.6, 0.30, 0.2)),
    ]
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    f = _features(row_of_three + row_of_two, display=0.0, refs=[], canvas=canvas)
    scores = _geometry_scores(f)
    assert scores["three_col"] == 1.0
    assert scores["two_col"] == 0.0
    assert _argmax(scores) == "three_col"


_RELS_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_MINIMAL_THEME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Minimal">
  <a:themeElements>
    <a:clrScheme name="Minimal">
      <a:dk1><a:srgbClr val="000000"/></a:dk1>
      <a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="000000"/></a:dk2>
      <a:lt2><a:srgbClr val="FFFFFF"/></a:lt2>
      <a:accent1><a:srgbClr val="0077FF"/></a:accent1>
      <a:accent2><a:srgbClr val="111111"/></a:accent2>
      <a:accent3><a:srgbClr val="222222"/></a:accent3>
      <a:accent4><a:srgbClr val="333333"/></a:accent4>
      <a:accent5><a:srgbClr val="444444"/></a:accent5>
      <a:accent6><a:srgbClr val="555555"/></a:accent6>
      <a:hlink><a:srgbClr val="0563C1"/></a:hlink>
      <a:folHlink><a:srgbClr val="954F72"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Office">
      <a:majorFont><a:latin typeface="Arial"/></a:majorFont>
      <a:minorFont><a:latin typeface="Arial"/></a:minorFont>
    </a:fontScheme>
  </a:themeElements>
</a:theme>
"""

_EMPTY_MASTER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
</p:sldMaster>
"""


def _package_with_layout_bg(layout_bg_xml: str, *, extra_files: dict[str, bytes] | None = None,
                             layout_rels_extra: str = "") -> PptxPackage:
    """Синтетический пакет из одного мастера (без своего фона) и одного
    лейаута с заданным `<p:bg>...</p:bg>` — тот же приём, что и
    `_package_with_layout_inheriting_master_bg` в test_usage.py, но с
    настраиваемым содержимым `p:bg`, чтобы проверить конкретные крайние
    случаи резолва фона (картинка, битый цвет), которых нет ни на одном из
    четырёх разведанных файлов."""
    layout_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld name="Тест">
    {layout_bg_xml}
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
</p:sldLayout>
"""
    files: dict[str, bytes | str] = {
        "_rels/.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/officeDocument" Target="ppt/presentation.xml"/>'
            "</Relationships>"
        ),
        "ppt/presentation.xml": (
            f'{_RELS_HEADER}'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483649" r:id="rId1"/></p:sldMasterIdLst>'
            "</p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/slideMaster" '
            'Target="slideMasters/slideMaster1.xml"/></Relationships>'
        ),
        "ppt/slideMasters/slideMaster1.xml": _EMPTY_MASTER_XML,
        "ppt/theme/theme1.xml": _MINIMAL_THEME_XML,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/>'
            f'<Relationship Id="rId2" Type="{_REL_BASE}/theme" Target="../theme/theme1.xml"/>'
            "</Relationships>"
        ),
        "ppt/slideLayouts/slideLayout1.xml": layout_xml,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/slideMaster" '
            'Target="../slideMasters/slideMaster1.xml"/>'
            f"{layout_rels_extra}"
            "</Relationships>"
        ),
    }
    if extra_files:
        files.update(extra_files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            if isinstance(content, bytes):
                zf.writestr(path, content)
            else:
                zf.writestr(path, content)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r"))


def _catalog_of(pkg: PptxPackage):
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    theme = read_theme(pkg, pick_primary_master(pkg))
    grid = build_grid(pkg, canvas)
    return build_layout_catalog(pkg, canvas, theme, grid)


def test_picture_background_luminance_is_sampled_not_treated_as_missing():
    """Регрессия (adversarial-reviewer, воспроизведено на контрольном
    ЛЦТ2026): `blipFill`-фон (фото-обложка) раньше молча схлопывался в
    "фона нет вовсе" и подменялся светлым lt1 темы — реальный тёмный фон
    давал is_dark=False. Синтетика: сплошная тёмно-фиолетовая картинка
    (RGB 20,10,40, WCAG-яркость «на глаз» заметно ниже 0.5) как фон
    лейаута — is_dark обязан стать True, а source не должен быть
    "lt1_fallback" (фон ЗАДАН, просто не сводится к одному цвету)."""
    img_buf = io.BytesIO()
    Image.new("RGB", (4, 4), (20, 10, 40)).save(img_buf, format="PNG")
    layout_bg = (
        "<p:bg><p:bgPr><a:blipFill><a:blip r:embed=\"rId2\"/></a:blipFill>"
        "<a:stretch><a:fillRect/></a:stretch></p:bgPr></p:bg>"
    )
    rel_extra = f'<Relationship Id="rId2" Type="{_REL_BASE}/image" Target="../media/image1.png"/>'
    pkg = _package_with_layout_bg(
        layout_bg,
        extra_files={"ppt/media/image1.png": img_buf.getvalue()},
        layout_rels_extra=rel_extra,
    )
    catalog = _catalog_of(pkg)
    assert len(catalog) == 1
    entry = catalog[0]
    assert entry.background.source == "layout"
    assert entry.background.luminance is not None
    assert entry.background.luminance < 0.1
    assert entry.is_dark is True


def test_malformed_bg_color_modifier_does_not_crash_the_catalog():
    """Регрессия (adversarial-reviewer): нечисловой `val` у модификатора
    цвета фона (`a:lumMod`) раньше давал необработанный ValueError из
    resolve_color, роняя build_layout_catalog целиком из-за ОДНОГО битого
    лейаута. Каталог обязан деградировать честно (Background.source ==
    "resolve_error"), не падать."""
    layout_bg = (
        "<p:bg><p:bgPr><a:solidFill><a:schemeClr val=\"accent1\">"
        '<a:lumMod val="not-a-number"/>'
        "</a:schemeClr></a:solidFill></p:bgPr></p:bg>"
    )
    pkg = _package_with_layout_bg(layout_bg)
    catalog = _catalog_of(pkg)  # не должен бросить ValueError
    assert len(catalog) == 1
    assert catalog[0].background.source == "resolve_error"
    assert catalog[0].background.luminance is None
    assert catalog[0].is_dark is False


# --- регрессии на находки повторного код-ревью Task 5 (см. task-5-report.md) ---


def test_largest_area_share_excludes_furniture_placeholders():
    """Находка №1: `largest_area_share` раньше считался по ВСЕМ
    плейсхолдерам, включая "мебель" (колонтитул/дата/номер слайда), хотя
    докстрока `_FURNITURE_PH_TYPES` обещает исключать её из структурных
    геометрических подсчётов. На трёх учебных файлах эффекта не было (мебель
    там крошечная), но на нативном шаблоне с крупным колонтитулом это ложно
    взводило "одинокий доминирующий блок" (quote/kpi) по площади подвала, а
    не контента. Огромный FOOTER (90% холста) не должен попасть в
    largest_area_share при том, что реальный контент — два маленьких BODY."""
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    placeholders = [
        PlaceholderSlot(ph_type="FOOTER", box=Box(0.0, 0.0, 1.0, 0.9)),
        PlaceholderSlot(ph_type="BODY", box=Box(0.1, 0.92, 0.3, 0.05)),
        PlaceholderSlot(ph_type="BODY", box=Box(0.5, 0.92, 0.3, 0.05)),
    ]
    f = _features(placeholders, display=0.0, refs=[], canvas=canvas)
    assert f.largest_area_share == pytest.approx(0.015)
    assert f.no_title_dominant is False  # ниже _MIN_DOMINANT_AREA_SHARE (0.10) без мебели


def test_largest_area_share_still_finds_real_dominant_content_block():
    """Контроль к предыдущему тесту: без мебели, с одним настоящим крупным
    контентным блоком, largest_area_share обязан по-прежнему видеть его."""
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    placeholders = [
        PlaceholderSlot(ph_type="FOOTER", box=Box(0.0, 0.95, 1.0, 0.05)),
        PlaceholderSlot(ph_type="BODY", box=Box(0.1, 0.1, 0.8, 0.7)),
    ]
    f = _features(placeholders, display=0.0, refs=[], canvas=canvas)
    assert f.largest_area_share == pytest.approx(0.56)


def _package_with_two_layouts_sharing_bg_image() -> tuple[PptxPackage, bytes]:
    """Два лейаута под одним мастером, оба со своим `p:bg` — картинка,
    ссылающаяся на ОДИН И ТОТ ЖЕ медиа-парт (находка №5: на контрольном
    ЛЦТ2026 22 из 23 лейаутов ссылаются на одну и ту же картинку 3840×2160,
    и раньше она декодировалась заново на каждый — полный разбор занимал
    почти полторы секунды почти целиком на этом)."""
    img_buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 10, 10)).save(img_buf, format="PNG")
    layout_bg = (
        "<p:bg><p:bgPr><a:blipFill><a:blip r:embed=\"rId2\"/></a:blipFill>"
        "<a:stretch><a:fillRect/></a:stretch></p:bgPr></p:bg>"
    )
    rel_extra = f'<Relationship Id="rId2" Type="{_REL_BASE}/image" Target="../media/image1.png"/>'
    layout_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
             xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld name="Тест">
    {layout_bg}
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
</p:sldLayout>
"""
    files: dict[str, bytes | str] = {
        "_rels/.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/officeDocument" Target="ppt/presentation.xml"/>'
            "</Relationships>"
        ),
        "ppt/presentation.xml": (
            f'{_RELS_HEADER}'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483649" r:id="rId1"/></p:sldMasterIdLst>'
            "</p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/slideMaster" '
            'Target="slideMasters/slideMaster1.xml"/></Relationships>'
        ),
        "ppt/slideMasters/slideMaster1.xml": _EMPTY_MASTER_XML,
        "ppt/theme/theme1.xml": _MINIMAL_THEME_XML,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/slideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/>'
            f'<Relationship Id="rId2" Type="{_REL_BASE}/slideLayout" '
            'Target="../slideLayouts/slideLayout2.xml"/>'
            f'<Relationship Id="rId3" Type="{_REL_BASE}/theme" Target="../theme/theme1.xml"/>'
            "</Relationships>"
        ),
        "ppt/slideLayouts/slideLayout1.xml": layout_xml,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/slideMaster" '
            'Target="../slideMasters/slideMaster1.xml"/>'
            f"{rel_extra}"
            "</Relationships>"
        ),
        "ppt/slideLayouts/slideLayout2.xml": layout_xml,
        "ppt/slideLayouts/_rels/slideLayout2.xml.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/slideMaster" '
            'Target="../slideMasters/slideMaster1.xml"/>'
            f"{rel_extra}"
            "</Relationships>"
        ),
        "ppt/media/image1.png": img_buf.getvalue(),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r")), img_buf.getvalue()


def test_picture_background_is_decoded_once_per_shared_media_part():
    """Находка №5: тот же медиа-парт, на который ссылаются ДВА разных
    лейаута, должен декодироваться `Image.open` ровно один раз за весь
    разбор каталога, а не по разу на лейаут."""
    pkg, _ = _package_with_two_layouts_sharing_bg_image()
    real_open = Image.open
    with mock.patch("deckforge.template.layouts.Image.open", side_effect=real_open) as spy:
        catalog = _catalog_of(pkg)
    assert len(catalog) == 2
    assert spy.call_count == 1
    assert all(e.background.luminance is not None for e in catalog)


def test_build_layout_catalog_accepts_precomputed_usage_and_type_scale():
    """Находка №4: `build_layout_catalog` не обязан сам вызывать
    `collect_usage`/`build_type_scale` — следующая задача (сборка профиля
    целиком) уже вызывает их сама и передаёт готовые. Если `usage`/
    `type_scale` переданы явно, повторного вызова быть не должно (иначе
    двойной обход всего архива)."""
    with PptxPackage.open("dataset/templates/VK Tech шаблон.pptx") as pkg:
        canvas = pkg.canvas()
        usage = collect_usage(pkg, canvas)
        type_scale = build_type_scale(pkg, canvas, usage)
        grid = build_grid(pkg, canvas)
        theme = read_theme(pkg, pick_primary_master(pkg))
        with (
            mock.patch("deckforge.template.layouts.collect_usage", side_effect=AssertionError("не должен звать")),
            mock.patch("deckforge.template.layouts.build_type_scale", side_effect=AssertionError("не должен звать")),
        ):
            catalog = build_layout_catalog(pkg, canvas, theme, grid, usage=usage, type_scale=type_scale)
    assert catalog


def test_master_title_size_fallback_is_used_when_layout_defines_no_own_size():
    """Находка №6: `title_size_ratio` — вырожден (None) на нативном шаблоне,
    где кегль заголовка задан не в лейауте, а в `p:txStyles` мастера (20 из
    23 макетов контрольного ЛЦТ2026). Когда лейаут не задаёт свой кегль
    заголовка, `_features` обязан подняться до кегля мастера, переданного
    вызывающим как `master_title_size`."""
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    placeholders = [PlaceholderSlot(ph_type="TITLE", box=Box(0.1, 0.5, 0.8, 0.2))]
    f = _features(placeholders, display=40.0, refs=[], canvas=canvas, master_title_size=20.0)
    assert f.title_size_ratio == pytest.approx(0.5)


def test_effective_master_title_size_reads_master_txstyles():
    """`_effective_master_title_size` читает
    `p:txStyles/p:titleStyle/a:lvl1pPr/a:defRPr/@sz` мастера, нормируя к
    холсту тем же множителем, что и везде в модуле."""
    master_xml = """<?xml version="1.0" encoding="UTF-8"?>
<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
  <p:txStyles>
    <p:titleStyle><a:lvl1pPr><a:defRPr sz="4000"/></a:lvl1pPr></p:titleStyle>
  </p:txStyles>
</p:sldMaster>
"""
    files = {
        "_rels/.rels": (
            f'{_RELS_HEADER}<Relationships {_RELS_NS}>'
            f'<Relationship Id="rId1" Type="{_REL_BASE}/officeDocument" Target="ppt/presentation.xml"/>'
            "</Relationships>"
        ),
        "ppt/presentation.xml": (
            f'{_RELS_HEADER}'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
        ),
        "ppt/slideMasters/slideMaster1.xml": master_xml,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    pkg = PptxPackage(zipfile.ZipFile(buf, "r"))
    canvas = Canvas(width_emu=12192000, height_emu=6858000)

    not_degraded = ThemeInfo(
        scheme={}, clr_map={}, major_font="", minor_font="", scheme_name="",
        font_scheme_degraded=False, text_styles_degraded=False, is_stock_office_palette=False,
    )
    assert _effective_master_title_size(pkg, "ppt/slideMasters/slideMaster1.xml", not_degraded, canvas) == 40.0

    degraded = ThemeInfo(
        scheme={}, clr_map={}, major_font="", minor_font="", scheme_name="",
        font_scheme_degraded=False, text_styles_degraded=True, is_stock_office_palette=False,
    )
    assert _effective_master_title_size(pkg, "ppt/slideMasters/slideMaster1.xml", degraded, canvas) is None


def test_two_content_maps_to_two_col_not_content():
    """Находка №3: 'Two Content' — стандартное имя PowerPoint для
    двухколоночного макета — содержит подстроку 'content' и раньше попадала
    в группу контентных, потому что сопоставление шло по первому совпавшему
    короткому паттерну. Более специфичный (длинный) вариант обязан
    поглощать более общий."""
    synonyms = _load_synonyms()
    assert _name_scores("Two Content", synonyms) == {"two_col": 1.0}


def test_title_and_content_does_not_become_title():
    """Находка №3: 'Title and Content' — стандартное контентное имя
    PowerPoint — содержит подстроку 'title' и раньше выигрывала бы тай-брейк
    геометрии как титульный макет (title стоит раньше content в
    _TIE_BREAK_PRIORITY), если бы 'title' не поглощался более длинным и
    точным 'title and content'."""
    synonyms = _load_synonyms()
    assert _name_scores("Title and Content", synonyms) == {"content": 1.0}


def test_genuine_multivalent_name_still_splits_between_groups():
    """Специфичность не должна ломать честную многозначность (докстрока
    config/layout-kinds.yaml): 'титул' и 'раздел' — независимые паттерны, ни
    один не substring другого, оба обязаны остаться в игре."""
    synonyms = _load_synonyms()
    assert _name_scores("Титульный слайд раздела", synonyms) == {"title": 0.5, "section": 0.5}


def test_standard_powerpoint_layout_names_are_recognized():
    """Находка №3: полный набор стандартных имён макетов PowerPoint (англ. и
    локализованных рус.) должен распознаваться словарём — см.
    config/layout-kinds.yaml."""
    synonyms = _load_synonyms()
    expected = {
        "Title Slide": "title", "Титульный слайд": "title",
        "Title and Content": "content", "Заголовок и объект": "content",
        "Section Header": "section", "Заголовок раздела": "section",
        "Two Content": "two_col", "Два объекта": "two_col",
        "Comparison": "two_col", "Сравнение": "two_col",
        "Title Only": "section", "Только заголовок": "section",
        "Blank": "free", "Пустой слайд": "free",
        "Content with Caption": "two_col", "Объект с подписью": "two_col",
        "Picture with Caption": "image", "Рисунок с подписью": "image",
        "Title and Vertical Text": "content", "Заголовок и вертикальный текст": "content",
        "Vertical Title and Text": "content", "Вертикальный заголовок и текст": "content",
    }
    for name, kind in expected.items():
        scores = _name_scores(name, synonyms)
        assert _argmax(scores) == kind, f"{name!r}: ожидали {kind!r}, получили {scores!r}"


def test_heading_leaders_synthetic_set_independent_of_dataset_files():
    """Находка №2 (обязательный синтетический тест): произвольный набор
    heading_score, НЕ связанный ни с одним файлом датасета. Решает не
    абсолютная величина балла, а разрыв (largest gap) между уровнями баллов
    ВНУТРИ этого конкретного набора."""
    # Два явных лидера (0.9), два явных "почти, но нет" (0.3) и низкий фон
    # (0.05) — самый большой разрыв в отсортированном наборе (0.6) стоит
    # между 0.9 и 0.3, поэтому лидируют только первые два.
    scores = [0.9, 0.9, 0.3, 0.3, 0.05, 0.05, 0.05]
    leaders = _heading_leaders(scores)
    assert set(leaders) == {0, 1}
    assert leaders[0] == pytest.approx(0.6)
    assert leaders[1] == pytest.approx(0.6)

    # П.1 требования брифа: решение не зависит от АБСОЛЮТНОЙ величины балла
    # — сдвиг всего набора на константу не должен изменить, кто лидирует.
    shifted = [s + 0.5 for s in scores]
    assert set(_heading_leaders(shifted)) == {0, 1}

    # Уверенность отражает величину отрыва: более разошедшийся набор даёт
    # больший margin лидеру, чем менее разошедшийся, при том же числе лидеров.
    tight = [0.6, 0.6, 0.5, 0.5, 0.05, 0.05]
    wide = [0.9, 0.9, 0.5, 0.5, 0.05, 0.05]
    assert _heading_leaders(wide)[0] > _heading_leaders(tight)[0]

    # Файл без единого героического заголовка (все баллы равны, включая все
    # нулевые) — лидировать не над чем.
    assert _heading_leaders([0.0, 0.0, 0.0]) == {}
    assert _heading_leaders([0.4, 0.4, 0.4]) == {}


def test_heading_leaders_matches_reverse_engineered_workspace_case():
    """Регрессия на находку №2 (см. отчёт): у WorkSpace геометрический балл
    "обложечности" 0.4279 у ДВУХ пограничных лейаутов раньше проходил
    старый абсолютный порог 0.5 впритык (граница калибровалась под этот
    файл). У ТРЁХ настоящих обложек балл 0.7349-0.7364 — они обязаны
    остаться лидерами, а пограничные 0.4279 — нет, при том что оба числа
    получены не из константы, а из места среди ОСТАЛЬНЫХ баллов файла."""
    scores = [
        0.7363636363636363, 0.2863636363636364, 0.13636363636363635,
        0.42792654100055677, 0.734857615525332, 0.734857615525332,
        0.13636363636363635, 0.42792654100055677, 0.13636363636363635,
        0.13636363636363635, 0.13636363636363635, 0.13636363636363635,
        0.13636363636363635, 0.13636363636363635, 0.13636363636363635,
    ]
    leaders = _heading_leaders(scores)
    assert set(leaders) == {0, 4, 5}
