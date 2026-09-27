"""Задача T1: разбор шаблона не выбрасывает слайды-примеры, которые нужны
сборке. Каждый тест на реальном шаблоне датасета: слайд, который разбор
раньше отбрасывал, теперь даёт раскладку, и раскладка устроена верно."""
from __future__ import annotations

import pytest

VK_TECH = "VK Tech шаблон.pptx"
WORKSPACE = "VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx"
EDUCATION = "Шаблон презентации VK Education.pptx"
LCT = "ЛЦТ2026 Шаблон презентации.pptx"


def _by_slide(profile_fixture, name: str) -> dict:
    return {n: p for p in profile_fixture(name).patterns for n in p.source_slide_index}


def _own(profile_fixture, name: str, slide: int):
    """Паттерн, снятый именно с этого слайда (он первый в источниках)."""
    found = [p for p in profile_fixture(name).patterns if p.source_slide_index[0] == slide]
    assert found, f"{name}: слайд {slide} не попал в профиль"
    return found[0]


def _overlaps(a, b, share: float = 0.3) -> bool:
    inter = a.intersect(b)
    return inter is not None and inter.area / min(a.area, b.area) > share


def test_frame_past_canvas_edge_is_clipped_not_dropped(profile_fixture):
    """VK Education 18: рамки «33%» и «23%» шириной 0.44 начинаются на 0.67
    холста. Их обрезают по полю, слайд остаётся раскладкой показателей."""
    grid = profile_fixture(EDUCATION).grid
    pattern = _own(profile_fixture, EDUCATION, 18)
    assert pattern.kind == "kpi"
    values = [s for s in pattern.slots if s.role == "kpi_value"]
    assert len(values) == 3
    for slot in pattern.slots:
        assert slot.box.right <= 1 - grid.margin_right + 0.01


@pytest.mark.parametrize("name,slide", [(EDUCATION, 6), (EDUCATION, 31), (EDUCATION, 32), (EDUCATION, 34), (LCT, 14)])
def test_photo_to_the_canvas_edge_keeps_the_slide(profile_fixture, name, slide):
    """Фото на полслайда в край холста: слайд остаётся, место под фото
    обрезано по холсту и доходит до его края."""
    pattern = _own(profile_fixture, name, slide)
    images = [s for s in pattern.slots if s.role == "image"]
    assert images
    assert any(s.box.right >= 0.995 for s in images)
    assert all(0.0 <= s.box.left and s.box.right <= 1.0 + 1e-9 for s in images)


@pytest.mark.parametrize("slide", [7, 8, 13, 18, 22, 23, 26, 28, 39, 41, 42, 45, 49, 50])
def test_picture_under_text_is_decor_on_vk_tech(profile_fixture, slide):
    """VK Tech рисует кольца, мокап телефона, ценовые карточки картинками
    под текстом. Такая картинка декор, слайд остаётся, и ни одно место
    под фото не лежит под текстом."""
    pattern = _by_slide(profile_fixture, VK_TECH).get(slide)
    assert pattern is not None, f"слайд {slide} не попал в профиль"
    own = _own(profile_fixture, VK_TECH, pattern.source_slide_index[0])
    images = [s for s in own.slots if s.role == "image"]
    texts = [s for s in own.slots if s.role not in ("image", "icon", "chart", "table")]
    assert not any(_overlaps(i.box, t.box) for i in images for t in texts)
    assert any(d.kind == "picture" for d in own.decor)


@pytest.mark.parametrize("slide", [13, 24, 25])
def test_picture_under_text_is_decor_on_workspace(profile_fixture, slide):
    """VK WorkSpace 13, 24, 25: 3D-картинка под текстом."""
    pattern = _own(profile_fixture, WORKSPACE, slide)
    images = [s for s in pattern.slots if s.role == "image"]
    texts = [s for s in pattern.slots if s.role not in ("image", "icon", "chart", "table")]
    assert not any(_overlaps(i.box, t.box) for i in images for t in texts)


@pytest.mark.parametrize("slide", [5, 6, 9, 10, 11, 23])
def test_card_icons_are_not_photo_places(profile_fixture, slide):
    """VK WorkSpace: значок на карточке мельче места под фото и остаётся
    декором, карточки не пропадают."""
    pattern = _by_slide(profile_fixture, WORKSPACE).get(slide)
    assert pattern is not None, f"слайд {slide} не попал в профиль"
    assert not [s for s in pattern.slots if s.role == "image"]
    assert pattern.repeat is not None


@pytest.mark.parametrize("slide", [17, 18])
def test_number_and_label_in_one_frame_split_into_kpi(profile_fixture, slide):
    """VK WorkSpace 17 и 18: «ххх%» и «данные показателя» в одной рамке.
    Число становится `kpi_value`, подпись `kpi_label`, обе с id той же
    фигуры, подпись под числом."""
    pattern = _own(profile_fixture, WORKSPACE, slide)
    assert pattern.kind == "kpi"
    values = [s for s in pattern.slots if s.role == "kpi_value"]
    labels = [s for s in pattern.slots if s.role == "kpi_label"]
    assert values and len(labels) == len(values)
    for value in values:
        label = next(s for s in labels if s.source_shape_id == value.source_shape_id)
        assert value.size_pt >= label.size_pt
        assert label.box.top >= value.box.bottom - 1e-6
        assert "%" in (value.sample_text or "")


def test_big_numbers_with_labels_on_vk_tech_are_kpi(profile_fixture):
    """VK Tech 42: «7» и «10» с подписью «Описание» под каждым."""
    pattern = _own(profile_fixture, VK_TECH, 42)
    assert pattern.kind == "kpi"
    assert sum(s.role == "kpi_value" for s in pattern.slots) == 2
    assert sum(s.role == "kpi_label" for s in pattern.slots) == 2


def test_quote_without_headline_is_mined(profile_fixture):
    """VK Education 19: высказывание и подпись автора без заголовка."""
    pattern = _own(profile_fixture, EDUCATION, 19)
    assert pattern.kind == "quote"
    roles = sorted(s.role for s in pattern.slots)
    assert roles == ["caption", "quote"]
    quote = next(s for s in pattern.slots if s.role == "quote")
    author = next(s for s in pattern.slots if s.role == "caption")
    assert quote.box.top < author.box.top


def test_team_grid_of_photos_is_mined(profile_fixture):
    """VK Education 20: восемь портретов с именем и должностью, без
    заголовка. Каждое фото место под фото в единице повтора."""
    pattern = _own(profile_fixture, EDUCATION, 20)
    assert pattern.kind == "team"
    assert pattern.repeat is not None and pattern.repeat.count == 8
    assert "image" in pattern.repeat.slot_roles
    assert sum(s.role == "image" for s in pattern.slots) == 8


@pytest.mark.parametrize("name,slide", [(EDUCATION, 42), (WORKSPACE, 26), (WORKSPACE, 27), (VK_TECH, 47), (VK_TECH, 48), (LCT, 25)])
def test_timeline_is_its_own_kind(profile_fixture, name, slide):
    """Единицы вдоль линии, цепочки стрелок или с датами: таймлайн, а не
    карточки. Повтор остаётся, по нему раскладка разворачивается."""
    pattern = _own(profile_fixture, name, slide)
    assert pattern.kind == "timeline"
    assert pattern.repeat is not None and pattern.repeat.count >= 3


@pytest.mark.parametrize("name,slide", [(VK_TECH, 31), (VK_TECH, 32), (EDUCATION, 15)])
def test_code_slide_gives_a_code_slot(profile_fixture, name, slide):
    """Слайд с моноширинным кодом: раскладка `code`, одно место `code` с
    гарнитурой примера, заголовок как обычно."""
    from deckforge.template.typography import is_mono_family

    pattern = _own(profile_fixture, name, slide)
    assert pattern.kind == "code"
    code = [s for s in pattern.slots if s.role == "code"]
    assert len(code) == 1 and code[0].mono and is_mono_family(code[0].font_family)
    assert any(s.role == "headline" for s in pattern.slots)


@pytest.mark.parametrize("slide", [7, 19, 28])
def test_empty_picture_placeholder_is_a_photo_place(profile_fixture, slide):
    """ЛЦТ2026: пустой плейсхолдер рисунка это место под фото, а не абзац.
    На слайде 28 это экран телефона, нарисованного на лейауте."""
    pattern = _own(profile_fixture, LCT, slide)
    assert any(s.role == "image" for s in pattern.slots)
    assert not any(s.role == "body" and s.box.area > 0.2 and not s.sample_text for s in pattern.slots)


@pytest.mark.parametrize("slide", [8, 22, 23, 28])
def test_nested_caption_frames_are_trimmed_not_dropped(profile_fixture, slide):
    """ЛЦТ2026: рамка пояснения заходит в рамку заголовка пункта. Внешняя
    обрезана по верху внутренней, слайд остаётся, наложения нет."""
    pattern = _own(profile_fixture, LCT, slide)
    texts = [s for s in pattern.slots if s.role not in ("image", "icon", "chart", "table")]
    assert not any(_overlaps(a.box, b.box) for i, a in enumerate(texts) for b in texts[i + 1:])


def test_device_mockup_frame_is_decor_and_screen_is_the_photo_place(profile_fixture):
    """ЛЦТ2026, слайд 26: плейсхолдер экрана внутри картинки-телефона.
    Экран место под скриншот, телефон декор."""
    pattern = _own(profile_fixture, LCT, 26)
    image = next(s for s in pattern.slots if s.role == "image")
    frames = [d for d in pattern.decor if d.kind == "picture" and d.box.area > image.box.area]
    assert frames
    frame = frames[0].box
    assert frame.left <= image.box.left and image.box.right <= frame.right + 1e-6


def test_closing_slide_is_not_merged_with_empty_copies(profile_fixture):
    """VK Education 52 «Спасибо за внимание!» и пустые 53-55 той же
    раскладки: финал остаётся отдельным паттерном со своим текстом."""
    pattern = _own(profile_fixture, EDUCATION, 52)
    head = next(s for s in pattern.slots if s.role == "headline")
    assert "Спасибо" in (head.sample_text or "")
