"""Сборка слайда клоном слайда-примера (`compose.clone`, `builder.place_slide_by_clone`).

Шаблон: VK Education из `dataset/templates/` (тот же файл, что лежит в
корне репозитория, байт в байт): на нём клон проверялся глазами, и у него
есть всё, что клон обязан сохранить: картинки-иконки, значки с номерами,
пустые плейсхолдеры титула. Модель не вызывается: профиль строится
разбором без сети (`cache_dir=None`, как в `conftest.py`).
"""
from __future__ import annotations
from dataclasses import replace
import zipfile
from collections import Counter
from pathlib import Path

import pytest
from lxml import etree
from pptx import Presentation

from deckforge.audit.config import AuditConfig
from deckforge.compose import builder
from deckforge.compose.blocks import Paragraph
from deckforge.compose.clone import bind_text, clone_example_slide, sample_slides_by_number, shape_text
from deckforge.ooxml.geometry import Box, Canvas
from deckforge.ooxml.ns import qn
from deckforge.ooxml.walk import walk_shapes
from deckforge.plan.spec import BulletBlock, Card, CardBlock, DeckSpec, SlideSpec, Visual
from deckforge.plan.variants import Variant

TEMPLATE = Path("dataset/templates/Шаблон презентации VK Education.pptx")


@pytest.fixture(scope="module")
def profile(profile_fixture):
    return profile_fixture(TEMPLATE.name)


@pytest.fixture
def deck():
    """Колода шаблона без примеров и сами примеры по номеру: ровно то, с
    чем работает `build_deck`."""
    prs = Presentation(str(TEMPLATE))
    sources = sample_slides_by_number(prs)
    builder._clear_sample_slides(prs)
    return prs, sources


def _pattern(profile, pattern_id: str):
    return next(builder._pattern_from_model(m) for m in profile.patterns if m.pattern_id == pattern_id)


def _canvas(profile) -> Canvas:
    return Canvas(width_emu=profile.canvas_width_emu, height_emu=profile.canvas_height_emu)


def _texts(slide) -> list[str]:
    return [shape_text(el).strip() for el in slide._element.iter(qn("p:sp")) if shape_text(el).strip()]


def _cards_spec(n: int) -> SlideSpec:
    return SlideSpec(
        index=0, kind="two_col", headline="Что нужно для раскатки",
        blocks=[CardBlock(items=[Card(body=f"Пункт номер {i + 1} плана раскатки") for i in range(n)])],
    )


def test_clone_keeps_shapes_and_image_links(profile, deck):
    prs, sources = deck
    source = sources[26]  # три иконки-картинки над тремя тезисами
    layout = builder._find_layout(prs, _pattern(profile, "slide26").layout_id)

    slide = clone_example_slide(prs, source, layout)

    canvas = _canvas(profile)
    assert len(list(walk_shapes(slide._element, canvas))) == len(list(walk_shapes(source._element, canvas)))
    blips = list(slide._element.iter(qn("a:blip")))
    assert blips, "у примера есть картинки: у клона они обязаны остаться"
    source_blobs = {rel.target_part.blob for rel in source.part.rels.values() if rel.reltype.endswith("/image")}
    for blip in blips:
        rel = slide.part.rels[blip.get(qn("r:embed"))]
        assert rel.reltype.endswith("/image")
        assert rel.target_part.blob in source_blobs
    assert not any(r.reltype.endswith("/notesSlide") for r in slide.part.rels.values())


def test_bind_text_keeps_size_and_color_of_first_run(deck):
    _prs, sources = deck
    element = run_pr = None
    for slide in sources.values():
        for sp in slide._element.iter(qn("p:sp")):
            run = sp.find(".//" + qn("a:r"))
            r_pr = run.find(qn("a:rPr")) if run is not None else None
            if r_pr is not None and r_pr.get("sz") and r_pr.find(qn("a:solidFill")) is not None:
                element, run_pr = sp, r_pr
                break
        if element is not None:
            break
    assert element is not None, "в шаблоне нет run с явным кеглем и цветом: тест не о чем"
    size, color = run_pr.get("sz"), _color(run_pr)
    body_pr_before = _xml(element.find(qn("p:txBody")).find(qn("a:bodyPr")))

    bind_text(element, [Paragraph("Первый пункт", bullet=True), Paragraph("Второй пункт", bullet=True)], bullet_char="•")

    paragraphs = element.find(qn("p:txBody")).findall(qn("a:p"))
    assert [_paragraph_text(p) for p in paragraphs] == ["Первый пункт", "Второй пункт"]
    for p in paragraphs:
        runs = p.findall(qn("a:r"))
        assert len(runs) == 1
        assert runs[0].find(qn("a:rPr")).get("sz") == size
        assert _color(runs[0].find(qn("a:rPr"))) == color
    assert _xml(element.find(qn("p:txBody")).find(qn("a:bodyPr"))) == body_pr_before


def test_unfilled_repeat_units_are_removed(profile, deck):
    prs, sources = deck
    pattern = _pattern(profile, "slide21")  # четыре кружка с номерами над четырьмя описаниями
    spec = _cards_spec(2)

    outcome = builder.place_slide_by_clone(prs, spec, pattern, profile, AuditConfig.load(), sources[21])

    assert outcome.reason is None
    texts = _texts(prs.slides[-1])
    badges = sorted(t for t in texts if t.isdigit())
    assert badges == ["1", "2"], texts
    assert sum("плана раскатки" in t for t in texts) == 2


def test_missing_headline_shape_falls_back_to_building_from_scratch(profile, deck):
    prs, sources = deck
    pattern = _pattern(profile, "slide21")
    # Коробка заголовка уведена туда, где в примере пусто: клону некуда
    # положить заголовок, и слайд обязан собраться старым путём.
    slots = [
        replace(s, box=Box(0.05, 0.9, 0.3, 0.05)) if s.role == "headline" else s for s in pattern.slots
    ]
    broken = replace(pattern, slots=slots)
    spec = _cards_spec(2)

    outcome = builder.place_slide_by_clone(prs, spec, broken, profile, AuditConfig.load(), sources[21])
    assert outcome.reason is not None and "headline" in outcome.reason
    assert len(prs.slides) == 0

    chosen, notes = builder._place_best_candidate(
        prs, spec, [broken], profile, _canvas(profile), AuditConfig.load(), source_slides=sources,
    )
    assert chosen is broken
    assert len(prs.slides) == 1
    assert any("не принят" in n for n in notes)
    name = prs.slides[0]._element.find(qn("p:cSld")).get("name") or ""
    assert not name.startswith(builder.CLONE_MARK_PREFIX)


def test_sample_text_does_not_survive_in_unfilled_slots(profile, deck):
    prs, sources = deck
    pattern = _pattern(profile, "slide26")  # два тезиса под иконками, третий тезис и сноска
    spec = _cards_spec(2)

    outcome = builder.place_slide_by_clone(prs, spec, pattern, profile, AuditConfig.load(), sources[26])

    assert outcome.reason is None
    texts = " ".join(_texts(prs.slides[-1]))
    for slot in pattern.slots:
        for line in (slot.sample_text or "").split("\n"):
            if len(line.strip()) > 3:
                assert line.strip() not in texts, f"остался текст примера: {line!r}"


def test_build_deck_marks_cloned_slides(profile):
    spec = DeckSpec(title="Клон", language="ru", slides=[_cards_spec(2)])
    spec.slides[0].pattern_id = "slide21"
    cloned = Presentation(str(builder.build_deck(spec, profile, TEMPLATE, Variant.dense, clone_examples=True)))
    names = [s._element.find(qn("p:cSld")).get("name") or "" for s in cloned.slides]
    assert names and names[0].startswith(builder.CLONE_MARK_PREFIX)

    spec_old = DeckSpec(title="Клон", language="ru", slides=[_cards_spec(2)])
    spec_old.slides[0].pattern_id = "slide21"
    rebuilt = Presentation(str(builder.build_deck(spec_old, profile, TEMPLATE, Variant.dense, clone_examples=False)))
    assert not any(
        (s._element.find(qn("p:cSld")).get("name") or "").startswith(builder.CLONE_MARK_PREFIX)
        for s in rebuilt.slides
    )


def test_user_photo_replaces_example_picture_without_duplicate_parts(profile):
    """Фото пользователя встаёт в фигуру-картинку примера (рамка телефона,
    круглая маска), а архив не получает двух частей с одним именем: новая
    картинка берёт имя, которое python-pptx считает свободным после
    очистки примеров, а следующий клон снова связывается со старой
    картинкой примера под тем же именем (см. `fix_duplicate_partnames`)."""
    photo = Path("fixtures/content-packs/edu-platform/photos/students-project-review.jpg")
    slides = [
        SlideSpec(
            index=i, kind=_pattern(profile, pid).kind, headline="Студенты защищают проекты", pattern_id=pid,
            blocks=[BulletBlock(items=["Разбор проекта с ментором", "Демо на планшете"])],
            visual=Visual(kind="photo", photo_name="p.jpg"),
        )
        for i, pid in enumerate(["slide30", "slide3", "slide33"])
    ]
    spec = DeckSpec(title="Фото", language="ru", slides=slides)

    out = builder.build_deck(
        spec, profile, TEMPLATE, Variant.visual, user_photos={"p.jpg": photo}, clone_examples=True,
    )

    names = zipfile.ZipFile(out).namelist()
    assert [n for n, k in Counter(names).items() if k > 1] == []
    assert builder.count_embedded_photos(out, {"p.jpg": photo}) == 1
    assert any("собран клоном" in f for s in spec.slides for f in s.findings)


def _paragraph_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(qn("a:t")))


def _color(r_pr) -> str:
    fill = r_pr.find(qn("a:solidFill"))
    return _xml(fill) if fill is not None else ""


def _xml(el) -> bytes:
    return etree.tostring(el) if el is not None else b""


def test_bind_text_marks_a_bold_paragraph_and_keeps_the_rest_regular(deck):
    """Заголовок карточки, склеенный с телом (`blocks._assign_cards`),
    приходит абзацем с `bold=True`: у его run появляется `b="1"`, у
    остальных начертание примера не меняется."""
    _prs, sources = deck
    element = next(
        sp for slide in sources.values() for sp in slide._element.iter(qn("p:sp"))
        if sp.find(".//" + qn("a:r")) is not None
    )
    bind_text(element, [Paragraph("Бюджет", bold=True), Paragraph("2,4 млн ₽")])

    runs = element.findall(".//" + qn("a:r"))
    assert [r.find(qn("a:t")).text for r in runs] == ["Бюджет", "2,4 млн ₽"]
    assert runs[0].find(qn("a:rPr")).get("b") == "1"
    assert runs[1].find(qn("a:rPr")) is None or runs[1].find(qn("a:rPr")).get("b") != "1"

