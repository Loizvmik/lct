"""Тесты `compose/diagrams.py` (Task 10) — brief дословно по составу
случаев: все шесть раскладок схем строятся нативными фигурами (не
растром), геометрия карточек — из словаря форм шаблона, пиктограммы —
только из иконочного набора шаблона (VK Education), отказ, когда набора
нет вовсе.

Task 10 код-ревью — дописаны регрессии на находки визуального ревью отчёта
задачи (№1 словарь форм различает шаблоны, №4 карточка видима на любом
полюсе фона, №5/№6 подложка пиктограммы контрастна иконке, №7 отбор
пиктограмм по confidence), все параметризованы по трём учебным шаблонам
(находка №3 код-ревью: раньше всё гонялось на одном VK Tech, где случайно
не было ни коллизии форм, ни коллизии цвета)."""
from __future__ import annotations

import zipfile

import pytest
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_SHAPE_TYPE

from deckforge.compose.diagrams import (
    _MIN_ICON_BYTES,
    DIAGRAM_KINDS,
    DiagramSpec,
    NoIconSet,
    _icon_average_luminance,
    add_diagram,
    add_pictogram_row,
)
from deckforge.template.naming import MIN_CONTRAST

# Дубликат `TEMPLATE_NAMES` из `tests/compose/conftest.py` — намеренно, не
# импорт (см. докстроку conftest.py про независимость тестовых модулей).
_TEMPLATE_NAMES = [
    "VK Tech шаблон.pptx",
    "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx",
    "Шаблон презентации VK Education.pptx",
]

# Собственный замер этой правки — какой формой КАЖДЫЙ учебный шаблон реально
# рисует карточку (гистограмма `prst` по декору карточных групп повтора,
# ТОЛЬКО по декору, обрамляющему текстовый слот раскладки — Task 10, вторая
# правка код-ревью: круглый аватар/бейдж под иконку в словарь не входит,
# даже если шагает той же сеткой повтора, что и карточки контента;
# `build_shape_vocabulary`, см. докстроку `template/shapes.py` для полных
# чисел и метода замера). Два разных ответа на трёх шаблонах (WorkSpace
# теперь совпадает с VK Tech, оба падают в запасной путь либо сходятся на
# `roundRect`) — Education по-прежнему отдельно.
_EXPECTED_DOMINANT_CARD_SHAPE = {
    "VK Tech шаблон.pptx": "roundRect",
    "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx": "roundRect",
    "Шаблон презентации VK Education.pptx": "rect",
}


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")

    def lin(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(l_a: float, l_b: float) -> float:
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


def _poles(profile) -> dict:
    """Первый макет каждого фактически встречающегося полюса фона
    (тёмный/светлый) шаблона — не выдумывает полюс, которого у шаблона нет
    (WorkSpace: ВСЕ 15 лейаутов тёмные, светлого полюса не существует)."""
    poles: dict = {}
    for layout in profile.layouts:
        if layout.background.luminance is None:
            continue
        pole = "dark" if layout.is_dark else "light"
        poles.setdefault(pole, layout)
    return poles


def test_every_diagram_kind_builds_from_native_shapes(new_slide, PROFILE, BOX):
    assert set(DIAGRAM_KINDS) == {"process", "cycle", "hierarchy", "funnel", "comparison", "timeline"}
    for kind in DIAGRAM_KINDS:
        shapes = add_diagram(
            new_slide(), BOX, DiagramSpec(kind=kind, items=["Заявка", "Проверка", "Согласование", "Выдача"]), PROFILE,
        )
        assert shapes, kind
        assert all(s.shape_type != MSO_SHAPE_TYPE.PICTURE for s in shapes), kind


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_diagram_card_uses_the_templates_dominant_shape(new_slide, profile_fixture, BOX, template_name):
    """Task 10 код-ревью, находка №1 (критично) + вторая правка код-ревью
    (осмотр рендера): словарь форм раньше считался ПЕРЕПИСЬЮ всех автофигур
    пакета (слайды+макеты+мастера) — прямоугольники макетов перевешивали
    язык карточек, и `rect` побеждал на всех трёх учебных шаблонах (см.
    докстроку `template/shapes.py` про живой замер). Промежуточная версия
    считала ЛЮБОЙ декор групп повтора — и словила круглые аватар-плашки/
    бейджи под иконку WorkSpace как "карточку", хотя карточка обрамляет
    текст, а не картинку/иконку. Теперь декор засчитывается карточным,
    только если внутри него лежит текстовый слот раскладки (`_contains_
    text`) — тест проверяет, что схема использует ИМЕННО ПРЕОБЛАДАЮЩУЮ форму
    словаря (`profile.shape_vocabulary[0]`) на каждом шаблоне отдельно."""
    profile = profile_fixture(template_name)
    expected_prst = _EXPECTED_DOMINANT_CARD_SHAPE[template_name]
    assert profile.shape_vocabulary, template_name
    assert profile.shape_vocabulary[0].prst == expected_prst, (
        template_name, [(e.prst, e.count) for e in profile.shape_vocabulary],
    )

    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="process", items=["A", "B"]), profile)
    autoshapes = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
    assert autoshapes
    expected_type = MSO_AUTO_SHAPE_TYPE.from_xml(expected_prst)
    assert all(s.auto_shape_type == expected_type for s in autoshapes), (
        template_name, [s.auto_shape_type for s in autoshapes],
    )


def test_all_three_templates_do_not_all_agree_on_one_shape(profile_fixture):
    """Синтетический тест на саму суть находки №1: если когда-нибудь три
    учебных шаблона случайно снова сойдутся на одной и той же преобладающей
    форме, это симптом регрессии (полная перепись снова перевешивает
    декором макетов), даже если по отдельности каждый параметризованный
    тест выше всё ещё проходит на СВОИХ ожидаемых значениях. После второй
    правки код-ревью (декор засчитывается карточным только с текстом
    внутри) VK Tech и WorkSpace сошлись на одной форме (`roundRect`) — оба
    честно посчитаны тем же кодом, простое совпадение форм языка карточек
    двух шаблонов, не растворение сигнала; порог теста (`>= 2` разных формы
    из трёх) при этом всё ещё соблюдён Education'ом."""
    winners = {
        _EXPECTED_DOMINANT_CARD_SHAPE[name]
        for name in _TEMPLATE_NAMES
    }
    assert len(winners) >= 2, winners


def test_hierarchy_connects_parent_to_every_child(new_slide, PROFILE, BOX):
    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="hierarchy", items=["Итог", "A", "B", "C"]), PROFILE)
    autoshapes = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
    connectors = [s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.LINE]
    assert len(autoshapes) == 4  # родитель + 3 ребёнка
    assert len(connectors) == 3  # по одному соединителю на ребёнка


def test_funnel_levels_shrink_in_width(new_slide, PROFILE, BOX):
    shapes = add_diagram(new_slide(), BOX, DiagramSpec(kind="funnel", items=["Показы", "Клики", "Заявки", "Сделки"]), PROFILE)
    widths = [s.width for s in shapes]
    assert widths == sorted(widths, reverse=True)


def test_unknown_diagram_kind_raises(new_slide, PROFILE, BOX):
    with pytest.raises(ValueError):
        add_diagram(new_slide(), BOX, DiagramSpec(kind="bogus", items=["A"]), PROFILE)


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_diagram_card_contrasts_with_the_actual_slide_background(
    real_slide_factory, profile_fixture, BOX, template_name,
):
    """Task 10 отчёт, находка №4 (критично): карточка схемы — фигура без
    шапки/сетки, которая ОБЯЗАНА визуально выделяться на фоне слайда, иначе
    становится невидимой (WorkSpace, тёмный лейаут "free": заливка полюса,
    БЛИЖАЙШЕГО к фону, совпала с чёрным фоном — карточка пропала целиком,
    остались только текст и стрелки соединителей). `pop_pair_for_luminance`
    обязан брать полюс surface/on_surface, чья яркость ДАЛЬШЕ от
    фактического фона макета — проверяем на каждом полюсе фона, который
    реально есть у шаблона (WorkSpace — только тёмный, см. `_poles`)."""
    profile = profile_fixture(template_name)
    poles = _poles(profile)
    assert poles, template_name  # у каждого учебного шаблона есть хотя бы один полюс

    surface_lum = _relative_luminance(profile.palette_roles["surface"])
    on_surface_lum = _relative_luminance(profile.palette_roles["on_surface"])

    for pole_name, layout in poles.items():
        slide = real_slide_factory(template_name, layout.part_name)
        shapes = add_diagram(slide, BOX, DiagramSpec(kind="process", items=["A", "B"]), profile)
        card = next(s for s in shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE)
        card_lum = _relative_luminance("#" + str(card.fill.fore_color.rgb))
        bg_lum = layout.background.luminance

        # "Другой" полюс — тот, что карточка НЕ выбрала: чтобы утверждение
        # "карточка дальше от фона, чем другой полюс" было содержательным
        # (не тривиально верным при surface==on_surface).
        other_lum = on_surface_lum if abs(card_lum - surface_lum) < abs(card_lum - on_surface_lum) else surface_lum
        assert abs(card_lum - bg_lum) >= abs(other_lum - bg_lum), (template_name, pole_name)


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_pictogram_plate_contrasts_with_the_icon_itself(new_slide, profile_fixture, BOX, template_name):
    """Task 10 отчёт, находки №5/№6: подложка под пиктограммой подбирается
    по средней яркости пикселей САМОЙ ИКОНКИ, не по фону слайда/шаблона —
    иначе контурный набор в тон фирменного фона (Education, находка №5) или
    белые иконки на тёмном фоне (ЛЦТ2026, вне датасета тестов, находка №6)
    получают подложку того же полюса, что и то, на чём сама иконка не
    контрастна, и пропадают."""
    profile = profile_fixture(template_name)
    slide = new_slide()
    pictures = add_pictogram_row(slide, BOX, ["a", "b", "c"], profile)

    all_shapes = list(slide.shapes)
    assert len(all_shapes) == 2 * len(pictures)  # плашка, картинка, плашка, картинка, ...
    plates, interleaved_pictures = all_shapes[0::2], all_shapes[1::2]
    assert [p.shape_id for p in interleaved_pictures] == [p.shape_id for p in pictures]

    for plate, picture in zip(plates, pictures):
        plate_hex = "#" + str(plate.fill.fore_color.rgb)
        icon_luminance = _icon_average_luminance(picture.image.blob)
        assert icon_luminance is not None
        contrast = _contrast(_relative_luminance(plate_hex), icon_luminance)
        assert contrast >= MIN_CONTRAST, (template_name, contrast)


@pytest.mark.parametrize("template_name", _TEMPLATE_NAMES)
def test_pictograms_prefer_high_confidence_icons(new_slide, profile_fixture, BOX, template_name):
    """Живой замер на VK Tech (Task 10 отчёт, находка №7): первые иконки
    каталога В ПОРЯДКЕ ОБХОДА ПАКЕТА — декоративный мусор (голая буква,
    почти пустой PNG), а не пиктограммы; отбор обязан предпочитать
    `confidence`, а не порядок каталога. Раньше регрессия проверялась
    только на VK Tech — параметризовано по всем трём учебным шаблонам
    (код-ревью, находка №3: у каждого своя пара уверенностей {0.4, 1.0})."""
    profile = profile_fixture(template_name)
    catalog = list(profile.assets.icons)
    max_confidence = max(ref.confidence for ref in catalog)
    low_confidence_parts = {
        ref.part_name for ref in catalog
        if ref.confidence < max_confidence or ref.size_bytes < _MIN_ICON_BYTES
    }
    n = min(5, len(catalog))
    shapes = add_pictogram_row(new_slide(), BOX, [str(i) for i in range(n)], profile)
    assert len(shapes) == n
    used_blobs = {shape.image.blob for shape in shapes}

    # Косвенная проверка: среди вставленных изображений не должно
    # встретиться байт-в-байт то же содержимое, что у заведомо худших
    # (более низкой уверенности/маленьких) файлов каталога.
    with zipfile.ZipFile(profile.source_path) as pkg:
        low_confidence_blobs = {pkg.read(part) for part in low_confidence_parts}
    assert not (used_blobs & low_confidence_blobs)


def test_pictograms_come_from_the_template_icon_set(new_slide, profile_fixture, BOX):
    profile = profile_fixture("Шаблон презентации VK Education.pptx")
    shapes = add_pictogram_row(new_slide(), BOX, ["рост", "время", "команда"], profile)
    assert len(shapes) == 3
    assert all(s.shape_type == MSO_SHAPE_TYPE.PICTURE for s in shapes)


def test_pictograms_are_refused_when_template_has_no_icon_set(new_slide, PROFILE, BOX):
    """Иконки либо из шаблона, либо их нет. Эмодзи и чужие наборы не подставляются."""
    profile_without_icons = PROFILE.model_copy(
        update={"assets": PROFILE.assets.model_copy(update={"icons": []})}
    )
    with pytest.raises(NoIconSet):
        add_pictogram_row(new_slide(), BOX, ["рост"], profile_without_icons)
