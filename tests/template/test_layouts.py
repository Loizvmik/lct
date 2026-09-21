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

import pytest
from conftest import ALL_TEMPLATES
from PIL import Image

from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.template.grid import build_grid
from deckforge.template.layouts import (
    PlaceholderSlot, _argmax, _features, _geometry_scores, build_layout_catalog,
)
from deckforge.template.theme import pick_primary_master, read_theme


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


