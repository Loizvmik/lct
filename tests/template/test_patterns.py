"""Майнинг композиционных паттернов со слайдов-примеров — тесты дословно из
брифа Task 7 (Step 1), .superpowers/sdd/task-7-brief.md.

Как и в test_typography.py/test_grid.py/test_layouts.py/test_assets.py,
`profile_fixture` в телах тестов брифа нужен как параметр — иначе pytest не
подставит фикстуру и вызов упадёт с NameError; это единственная правка
против буквального текста брифа (тот же приём уже применялся в Task 3-6).
"""
from __future__ import annotations
import io
import zipfile

from conftest import ALL_TEMPLATES
from lxml import etree

from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.ooxml.walk import ShapeRef
from deckforge.template.grid import Grid
from deckforge.template.patterns import (
    Capacity,
    PatternSlot,
    RepeatSpec,
    _TierInfo,
    _capacity,
    _classify_kind,
    _find_repeat,
    _shape_text,
    _slide_is_dark,
    _split_content_decor,
    _tier_info,
    _to_decor,
)
from deckforge.template.theme import ThemeInfo
from deckforge.template.typography import TypeScale


def test_patterns_are_mined_from_every_template(profile_fixture):
    for name in ALL_TEMPLATES:
        patterns = profile_fixture(name).patterns
        assert len(patterns) >= 8, f"{name}: слишком мало паттернов, генератору не из чего выбирать"


def test_card_grid_is_detected_in_vk_tech(profile_fixture):
    """1553 автошейпа, 1502 с noFill — это карточные композиции."""
    patterns = profile_fixture("VK Tech шаблон.pptx").patterns
    cards = [p for p in patterns if p.kind == "cards"]
    assert cards
    assert any(p.repeat and p.repeat.count >= 3 for p in cards)


def test_two_column_pattern_has_symmetric_slots(profile_fixture):
    """Среди слотов top-score two_col-паттерна должна найтись пара с
    практически равной шириной — сами колонки.

    Не берём буквально "два самых левых слота" (было так до Task 7
    повторного ревью, находка №1): после починки пустого плейсхолдера
    (см. patterns.py::_split_content_decor) top-score two_col шаблона
    Education — slide15, где НАД левой колонкой ещё стоит headline,
    выровненный по тому же левому краю (0.0551 vs 0.0556) — "два самых
    левых слота" в этом случае оба из ЛЕВОЙ колонки (headline+body), а не
    пара левая/правая колонка. Перебираем все пары и берём с минимальной
    разницей ширины — она и есть настоящая пара колонок независимо от
    того, сколько ещё слотов (headline и т.п.) есть в паттерне."""
    patterns = profile_fixture("Шаблон презентации VK Education.pptx").patterns
    two_col = [p for p in patterns if p.kind == "two_col"]
    assert two_col
    slots = two_col[0].slots
    pairs = [(a, b) for i, a in enumerate(slots) for b in slots[i + 1:]]
    left, right = min(pairs, key=lambda ab: abs(ab[0].box.width - ab[1].box.width))
    assert abs(left.box.width - right.box.width) < 0.02


def test_every_pattern_has_a_headline_slot_or_is_marked_decorative(profile_fixture):
    for name in ALL_TEMPLATES:
        for pattern in profile_fixture(name).patterns:
            roles = {slot.role for slot in pattern.slots}
            assert "headline" in roles or pattern.kind in {"section", "image", "closing"}


def test_slots_respect_template_margins(profile_fixture):
    for name in ALL_TEMPLATES:
        grid = profile_fixture(name).grid
        for pattern in profile_fixture(name).patterns:
            for slot in pattern.slots:
                assert slot.box.left >= grid.margin_left - 0.01
                assert slot.box.right <= 1 - grid.margin_right + 0.01


def test_capacity_is_derived_from_measured_box_not_guessed(profile_fixture):
    """max_chars слота должен считаться замером текста в его рамке,
    иначе генератор напишет текст, который не влезет."""
    pattern = profile_fixture("VK Tech шаблон.pptx").patterns[0]
    headline = next(s for s in pattern.slots if s.role == "headline")
    assert 20 <= headline.max_chars <= 300


def test_soft_line_break_does_not_corrupt_slot_text(profile_fixture):
    """В шаблонах перенос строки — a:br, который text_frame отдаёт как \\x0b."""
    for name in ALL_TEMPLATES:
        for pattern in profile_fixture(name).patterns:
            for slot in pattern.slots:
                assert "\x0b" not in (slot.sample_text or "")


# === Task 7 повторное ревью — синтетические тесты на находки ==============
#
# Всё ниже — синтетика с заранее известным ответом, не через три учебных
# .pptx (профиль целиком не годится для проверки МЕХАНИЗМА напрямую: три
# известных файла могут случайно давать нужный агрегат по другой причине).
# Контрольный ЛЦТ2026 в тестах по-прежнему не участвует (см. conftest.py) —
# он прогоняется вручную, числа в отчёте задачи.

_SP_NS = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
)

_CANVAS = Canvas(width_emu=12192000, height_emu=6858000)


def _ref(
    box: Box, *, kind: str = "shape", element=None,
    is_placeholder: bool = False, ph_type: str | None = None,
) -> ShapeRef:
    """Минимальный синтетический ShapeRef — без реального lxml-дерева, когда
    он не нужен (`_find_repeat`/`_aligned` его не читают вовсе; для
    kind="picture" `_prelim_repeat_role` смотрит только на `box`)."""
    return ShapeRef(
        element=element, kind=kind, box=box, name="", shape_id="1",
        rotation=0.0, flip_h=False, flip_v=False, group_depth=0, group_chain=(),
        is_placeholder=is_placeholder, ph_type=ph_type, ph_idx=None,
    )


def _tier(*, step: str = "body", numeric: bool = False, bulleted: bool = False, text: str | None = "x") -> _TierInfo:
    return _TierInfo(step=step, size_pt=16.0, numeric=numeric, bulleted=bulleted,
                      text=text, align="l", color_hex=None)


def _row(lefts: list[float], top: float, width: float, height: float) -> list[Box]:
    return [Box(left=left, top=top, width=width, height=height) for left in lefts]


def _type_scale() -> TypeScale:
    return TypeScale(
        steps={"micro": 8.0, "caption": 10.0, "body": 14.0, "h2": 20.0, "h1": 32.0, "display": 44.0},
        heading_line_spacing=1.1, body_line_spacing=1.3, default_align="l",
        bold_is_idiomatic=True, italic_is_idiomatic=False, families=["Arial"],
    )


def _theme() -> ThemeInfo:
    return ThemeInfo(
        scheme={"lt1": "FFFFFF", "dk1": "000000", "dk2": "222222", "lt2": "EEEEEE", "accent1": "336699"},
        clr_map={}, major_font="Arial", minor_font="Arial", scheme_name="Office",
        font_scheme_degraded=False, text_styles_degraded=False, is_stock_office_palette=False,
    )


# --- находка №5: поиск повтора (_find_repeat) напрямую, суть задачи -------


def test_find_repeat_detects_even_row_as_repeat_of_n():
    """Ровный ряд из N одинаковых блоков с постоянным шагом опознаётся как
    повтор на N — это и есть основной случай, ради которого поиск повтора
    вообще существует (шесть нарисованных карточек → одна раскладка на N)."""
    boxes = _row([0.05, 0.24, 0.43, 0.62, 0.81], top=0.3, width=0.15, height=0.1)
    content = [_ref(b) for b in boxes]
    tiers = [_tier(step="h2") for _ in boxes]

    repeat, roles_by_index = _find_repeat(content, tiers)

    assert repeat is not None
    assert repeat.axis == "x"
    assert repeat.count == 5
    assert abs(repeat.step - 0.19) < 1e-9
    assert set(roles_by_index) == set(range(5))


def test_find_repeat_rejects_row_with_one_outlier():
    """Ряд с одним выбивающимся (не на шаге) блоком повтором не считается —
    не "повтор на 4 из 5", а вовсе НЕ повтор: константный шаг ломается для
    всего ряда сразу (см. `_constant_step`), а не только для пары соседей
    вокруг выбившегося блока."""
    boxes = _row([0.05, 0.24, 0.43, 0.67, 0.86], top=0.3, width=0.15, height=0.1)
    content = [_ref(b) for b in boxes]
    tiers = [_tier(step="h2") for _ in boxes]

    repeat, roles_by_index = _find_repeat(content, tiers)

    assert repeat is None
    assert roles_by_index == {}


def test_find_repeat_merges_two_rows_of_different_kind_same_axis_and_step():
    """Два ряда РАЗНОГО рода содержимого (заголовок карточки + тело
    карточки) с одинаковым шагом и осью сливаются в ОДИН повтор — та самая
    пара title+body внутри карточки, ради которой `_find_repeat` вообще
    сливает кандидатов (см. её докстроку)."""
    lefts = [0.05, 0.24, 0.43, 0.62, 0.81]
    title_boxes = _row(lefts, top=0.10, width=0.15, height=0.05)
    body_boxes = _row(lefts, top=0.20, width=0.15, height=0.08)
    content = [_ref(b) for b in title_boxes] + [_ref(b) for b in body_boxes]
    tiers = [_tier(step="h2") for _ in title_boxes] + [_tier(step="body") for _ in body_boxes]

    repeat, roles_by_index = _find_repeat(content, tiers)

    assert repeat is not None
    assert repeat.axis == "x"
    assert repeat.count == 5
    assert len(roles_by_index) == 10  # обе группы (10 шейпов) вошли в один повтор


def test_find_repeat_does_not_merge_spatially_unrelated_rows_with_same_step():
    """Два ряда с одинаковым шагом, но пространственно НЕ связанные (разные
    x-координаты элементов, не просто разное "разное содержимое" — см.
    `_aligned`), сливаться не должны: одинаковый шаг сам по себе — не
    редкость, совпадение шага у двух независимых сеток на слайде не делает
    их одной сеткой."""
    lefts_a = [0.05, 0.24, 0.43, 0.62, 0.81]
    lefts_b = [0.10, 0.29, 0.48, 0.67, 0.86]  # тот же шаг 0.19, сдвинуто на 0.05
    row_a = _row(lefts_a, top=0.10, width=0.15, height=0.05)
    row_b = _row(lefts_b, top=0.60, width=0.15, height=0.05)
    content = [_ref(b) for b in row_a] + [_ref(b) for b in row_b]
    # Разный "род" (bulleted vs numeric), чтобы группы повтора изначально
    # были раздельными кандидатами, а не одной группой одинаковых шейпов ещё
    # на этапе группировки по роду содержимого.
    tiers = (
        [_tier(step="h2", bulleted=True) for _ in row_a]
        + [_tier(step="h2", numeric=True) for _ in row_b]
    )

    repeat, roles_by_index = _find_repeat(content, tiers)

    assert repeat is not None
    assert repeat.count == 5  # не 10 — группы НЕ слились
    assert len(roles_by_index) == 5


def test_find_repeat_distinguishes_vertical_from_horizontal_axis():
    """Повтор по вертикали и по горизонтали различаются в самом `axis`."""
    boxes = [Box(left=0.1, top=t, width=0.2, height=0.15) for t in (0.1, 0.3, 0.5, 0.7)]
    content = [_ref(b) for b in boxes]
    tiers = [_tier(step="body") for _ in boxes]

    repeat, _ = _find_repeat(content, tiers)

    assert repeat is not None
    assert repeat.axis == "y"
    assert repeat.count == 4


# --- находка №1: пустой плейсхолдер — контент, не декор -------------------

_EMPTY_TITLE_PLACEHOLDER_XML = f"""
<p:sp {_SP_NS}>
  <p:nvSpPr>
    <p:cNvPr id="2" name="Title 1"/>
    <p:cNvSpPr/>
    <p:nvPr><p:ph type="title"/></p:nvPr>
  </p:nvSpPr>
  <p:spPr>
    <a:xfrm><a:off x="457200" y="274638"/><a:ext cx="8229600" cy="1143000"/></a:xfrm>
  </p:spPr>
  <p:txBody>
    <a:bodyPr/>
    <a:p><a:endParaRPr lang="ru-RU"/></a:p>
  </p:txBody>
</p:sp>
"""

_EMPTY_NON_PLACEHOLDER_XML = f"""
<p:sp {_SP_NS}>
  <p:nvSpPr><p:cNvPr id="3" name="Rectangle 1"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
  <p:spPr>
    <a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></a:xfrm>
    <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
  </p:spPr>
  <p:txBody><a:bodyPr/><a:p/></p:txBody>
</p:sp>
"""


def test_split_content_decor_keeps_empty_placeholder_as_content():
    """Слайды 8/9/10 контрольного ЛЦТ2026: `<p:ph type="title"/>` с
    геометрией, но без единого `a:r` — раньше уходил в decor, теперь это
    контентный слот (см. patterns.py::_split_content_decor)."""
    element = etree.fromstring(_EMPTY_TITLE_PLACEHOLDER_XML)
    ref = _ref(Box(0.05, 0.05, 0.6, 0.15), element=element, is_placeholder=True, ph_type="title")

    content, decor = _split_content_decor([ref], rels={}, logo_target=None, bg_targets=set())

    assert content == [ref]
    assert decor == []


def test_split_content_decor_still_treats_empty_non_placeholder_as_decor():
    """Регрессия: фигура без текста, НЕ являющаяся плейсхолдером, остаётся
    декором — находка №1 чинит только плейсхолдеры, не любую пустую фигуру."""
    element = etree.fromstring(_EMPTY_NON_PLACEHOLDER_XML)
    ref = _ref(Box(0.4, 0.4, 0.05, 0.05), element=element, is_placeholder=False, ph_type=None)

    content, decor = _split_content_decor([ref], rels={}, logo_target=None, bg_targets=set())

    assert decor == [ref]
    assert content == []


def test_tier_info_for_empty_placeholder_has_no_sample_text_but_is_title_tier():
    """Пустой title-плейсхолдер: `sample_text` (через `_TierInfo.text`) —
    `None` (нечего показать как текст-рыбу), но ступень всё равно h1 —
    слот остаётся кандидатом в headline, а не выпадает из типографического
    разбора вовсе."""
    element = etree.fromstring(_EMPTY_TITLE_PLACEHOLDER_XML)
    ref = _ref(Box(0.05, 0.05, 0.6, 0.15), element=element, is_placeholder=True, ph_type="title")

    tier = _tier_info(ref, _CANVAS, _type_scale(), _theme())

    assert tier is not None
    assert tier.text is None
    assert tier.step == "h1"


# --- находка №3: мягкий перенос строки не склеивает слова ------------------

_SOFT_BREAK_XML = f"""
<p:sp {_SP_NS}>
  <p:nvSpPr><p:cNvPr id="4" name="Text"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
  <p:spPr/>
  <p:txBody>
    <a:bodyPr/>
    <a:p>
      <a:r><a:t>Заголовок</a:t></a:r>
      <a:br/>
      <a:r><a:t>в две или в одну строчку</a:t></a:r>
    </a:p>
  </p:txBody>
</p:sp>
"""


def test_shape_text_does_not_glue_words_across_soft_line_break():
    """Регрессия на реальную склейку ("Заголовокв две или в одну строчку",
    встречалось 15/9/84/10 раз в четырёх шаблонах) — тест, который ловит
    именно склейку слов, а не только отсутствие `\\x0b` (старый тест этого
    не делал и потому не заметил бы порчу)."""
    element = etree.fromstring(_SOFT_BREAK_XML)

    text = _shape_text(element)

    assert "Заголовокв" not in text
    assert "Заголовок" in text
    assert "в две или в одну строчку" in text
    assert "\x0b" not in text


# --- находка №4: тёмный фон — переиспользует p:bg-логику layouts.py -------


def _package_from_slide_xml(slide_xml: str) -> PptxPackage:
    """Синтетический пакет из одного slide1.xml — тот же приём, что и в
    tests/template/test_grid.py (`_package_from_slide_xml`)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>',
        )
        zf.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r"))


_DARK_GRADIENT_BG_SLIDE_XML = f"""
<p:sld {_SP_NS}>
  <p:cSld>
    <p:bg>
      <p:bgPr>
        <a:gradFill>
          <a:gsLst>
            <a:gs pos="0"><a:srgbClr val="0A0A0A"/></a:gs>
            <a:gs pos="100000"><a:srgbClr val="1A1A1A"/></a:gs>
          </a:gsLst>
        </a:gradFill>
      </p:bgPr>
    </p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_slide_is_dark_recognizes_gradient_background_without_decor_shape():
    """Task 7 повторное ревью, находка №4: раньше `_slide_is_dark` смотрел
    ТОЛЬКО на крупную декоративную фигуру со сплошной заливкой — слайд без
    единой декоративной фигуры, но с тёмным градиентным `p:bg`, тихо
    получал `is_dark=False`. Слайд ниже НЕ содержит ни одной декоративной
    фигуры вовсе (пустой spTree) — единственный источник ответа теперь
    p:bg-цепочка, переиспользованная у layouts.py."""
    pkg = _package_from_slide_xml(_DARK_GRADIENT_BG_SLIDE_XML)

    is_dark = _slide_is_dark(pkg, _theme(), "ppt/slides/slide1.xml", None, decor=[], bg_image_cache={})

    assert is_dark is True


# --- находка №6 (мелочи): DecorShape.fill_kind + средний цвет --------------

_GRADIENT_DECOR_SHAPE_XML = f"""
<p:sp {_SP_NS}>
  <p:nvSpPr><p:cNvPr id="5" name="Plaque"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
  <p:spPr>
    <a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></a:xfrm>
    <a:gradFill>
      <a:gsLst>
        <a:gs pos="0"><a:srgbClr val="000000"/></a:gs>
        <a:gs pos="100000"><a:srgbClr val="FFFFFF"/></a:gs>
      </a:gsLst>
    </a:gradFill>
  </p:spPr>
</p:sp>
"""


def test_to_decor_reports_fill_kind_and_average_color_for_gradient():
    """Раньше градиентная/узорная/картиночная плашка отдавала
    `has_fill=True, fill_hex=None` — "заливка есть, цвет неизвестен".
    Теперь `fill_kind` честно называет вид заливки, а `fill_hex` — средний
    цвет (для градиента — среднее стоп-точек)."""
    element = etree.fromstring(_GRADIENT_DECOR_SHAPE_XML)
    ref = _ref(Box(0.0, 0.0, 0.1, 0.1), element=element)

    decor = _to_decor(None, {}, ref, _theme(), {})

    assert decor.fill_kind == "gradient"
    assert decor.has_fill is True
    assert decor.fill_hex == "#808080"  # среднее (000000, FFFFFF)


# --- находка №6 (мелочи): _capacity учитывает размер последней карточки ---


def test_capacity_accounts_for_last_item_own_footprint_not_only_step():
    """`span/step + 1` переоценивал вместимость на единицу: N карточек с
    шагом `step` занимают `(N-1)*step + item_size`, не `N*step` — последняя
    карточка занимает ещё и собственную ширину сверх шага."""
    grid = Grid(
        margin_left=0.05, margin_right=0.05, margin_top=0.05, margin_bottom=0.05,
        columns=[], gutter=0.0, anchors={},
    )
    repeat = RepeatSpec(axis="x", count=3, step=0.2, slot_roles=["card_body"])
    slot = PatternSlot(
        role="card_body", box=Box(left=0.1, top=0.1, width=0.15, height=0.1),
        size_pt=14.0, color_hex=None, align="l", max_chars=10, wraps=False, sample_text=None,
    )
    content: list[ShapeRef] = []

    capacity = _capacity(content, [slot], repeat, grid)

    # span = 1 - 0.05 - 0.05 = 0.9; item_size = 0.15 (ширина слота повтора)
    # старая формула (без учёта item_size): int(0.9/0.2) + 1 = 5 (переоценка)
    # новая формула: int((0.9 - 0.15)/0.2) + 1 = int(3.75) + 1 = 4
    assert capacity.max_items == 4


# === Task 7 повторное ревью — дефект №1: карточки по структуре, не по
# типографике (найдено предыдущим исполнителем на слайде 9 контрольного
# ЛЦТ2026: сетка из пяти карточек участников, каждая — три строки ОДНОГО
# кегля, ни одна не набрана заголовочным кеглем) ===========================


def _card_slot(role: str, left: float, top: float, *, width: float = 0.15, height: float = 0.03) -> PatternSlot:
    return PatternSlot(
        role=role, box=Box(left=left, top=top, width=width, height=height),
        size_pt=14.0, color_hex=None, align="l", max_chars=10, wraps=False, sample_text="x",
    )


def test_classify_kind_recognizes_cards_by_repeat_structure_not_typography():
    """Пять карточек по три элемента одного кегля — карточку делает
    СТРУКТУРА повтора (число групп и число элементов на группу), не
    заголовочный кегль внутри карточки. Slot_roles повтора здесь --
    ['card_body'] целиком, БЕЗ card_title/kpi_value — старое условие
    (`_CARD_TITLE_LIKE_ROLES & repeat_roles`) на этих данных было бы
    красным по существу, не по сигнатуре."""
    lefts = [0.05, 0.24, 0.43, 0.62, 0.81]
    slots = [
        _card_slot("card_body", left, top)
        for left in lefts
        for top in (0.30, 0.36, 0.42)
    ]
    slots.append(PatternSlot(
        role="headline", box=Box(left=0.05, top=0.05, width=0.6, height=0.1),
        size_pt=32.0, color_hex=None, align="l", max_chars=20, wraps=False, sample_text="Заголовок",
    ))
    repeat = RepeatSpec(axis="x", count=5, step=0.19, slot_roles=["card_body"], group_size=3)
    roles_present = {"card_body", "headline"}

    kind = _classify_kind([], slots, repeat, roles_present, _CANVAS)

    assert kind == "cards"


def test_classify_kind_does_not_treat_repeat_of_single_lines_as_cards():
    """Повтор из пяти ОДИНОЧНЫХ строк (одна строка на элемент, group_size=1)
    карточками не считается — структуре не хватает "нескольких элементов в
    каждой группе", одного заголовочного кегля тоже нет, чтобы притянуть
    старое условие."""
    lefts = [0.05, 0.24, 0.43, 0.62, 0.81]
    slots = [_card_slot("bullet", left, 0.30) for left in lefts]
    slots.append(PatternSlot(
        role="headline", box=Box(left=0.05, top=0.05, width=0.6, height=0.1),
        size_pt=32.0, color_hex=None, align="l", max_chars=20, wraps=False, sample_text="Заголовок",
    ))
    repeat = RepeatSpec(axis="x", count=5, step=0.19, slot_roles=["bullet"], group_size=1)
    roles_present = {"bullet", "headline"}

    kind = _classify_kind([], slots, repeat, roles_present, _CANVAS)

    assert kind != "cards"
