"""Команда с нуля: карточки «фото + имя + роль». Ни у одного шаблона
датасета нет раскладки под людей, и участники ложились карточками с
фамилиями в тексте.

Фото участника ищется по подписи к файлу контент-пакета
(`plan.photos.photo_caption`) и имени файла: подпись с фамилией участника
отдаёт фото ему. Остальные фото, которые распределение отдало этому
слайду, идут по порядку участникам без фото. Участник без фото получает
кружок с инициалами цветом бренда.

Модуль раскладывает места и кружки; рисует `builder.place_slide`, а фото
вставляет `place_member_photos`: квадратная середина снимка в круглой
рамке, без растяжения."""
from __future__ import annotations
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pptx.util import Emu

from deckforge.compose.blocks import Paragraph, SlotContent
from deckforge.compose.kpi import grid_shape, line_height, lines_needed, plaque_at, plaque_pad, text_slot
from deckforge.ooxml.geometry import Box
from deckforge.ooxml.ns import qn
from deckforge.plan.photos import photo_caption
from deckforge.plan.spec import TeamBlock, TeamMember
from deckforge.template.patterns import DecorShape

EMU_PER_INCH = 914400
MAX_PER_ROW = 4
MAX_MEMBERS = 6
NAME_LINES = 2
ROLE_LINES = 3
_NAME_SCALE = 1.25
# Кружок фото не шире этой доли ячейки и не выше этой доли места.
_CIRCLE_CELL_SHARE = 0.6
_CIRCLE_REGION_SHARE = 0.5
_GAP = 0.012
# Основа фамилии для сравнения с подписью: без окончания падежа
# («Смирнова» и «Смирновой»), но не короче четырёх букв.
_STEM_MIN = 4


@dataclass(frozen=True)
class MemberCard:
    member: TeamMember
    circle: Box
    photo: Path | None


def _stem(word: str) -> str:
    word = word.lower().strip(".,;:()«»\"")
    return word[:max(_STEM_MIN, len(word) - 2)]


def _mentions(member: TeamMember, text: str) -> bool:
    """В подписи или имени файла названа фамилия участника: по основе
    самого длинного слова имени (у «Анна Смирнова» это фамилия)."""
    words = [w for w in member.name.split() if len(w) >= _STEM_MIN]
    if not words:
        return False
    surname = max(words, key=len)
    return _stem(surname) in text.lower()


def member_photos(
    members: list[TeamMember], user_photos: dict[str, Path] | None, slide_photos: list[str] = (),
) -> list[Path | None]:
    """Фото каждому участнику: по подписи, иначе по порядку из фото слайда."""
    photos = dict(user_photos or {})
    haystack = {
        name: f"{photo_caption(path) or ''} {Path(name).stem.replace('-', ' ').replace('_', ' ')}"
        for name, path in photos.items()
    }
    out: list[Path | None] = [None] * len(members)
    used: set[str] = set()
    for i, member in enumerate(members):
        for name in sorted(photos):
            if name not in used and _mentions(member, haystack[name]):
                out[i] = photos[name]
                used.add(name)
                break
    queue = [n for n in slide_photos if n in photos and n not in used]
    for i in range(len(members)):
        if out[i] is None and queue:
            name = queue.pop(0)
            out[i] = photos[name]
            used.add(name)
    return out


def initials(name: str) -> str:
    letters = [w[0] for w in name.split() if w and w[0].isalpha()]
    return "".join(letters[:2]).upper() or "?"


def team_layout(
    block: TeamBlock, region: Box, profile, grid, photos: list[Path | None], *, brand_hex: str | None,
    initials_hex: str, plaque: DecorShape | None = None, family: str = "Arial",
) -> tuple[list[SlotContent], list[DecorShape], list[MemberCard]]:
    """Места имён и ролей, кружки с инициалами и места фото в `region`."""
    members = [m for m in block.items if m.name.strip()][:MAX_MEMBERS]
    if not members:
        return [], [], []
    rows, cols = grid_shape(len(members), MAX_PER_ROW)
    gutter = grid.gutter
    pad = plaque_pad(grid, plaque)
    cell_w = (region.width - gutter * (cols - 1)) / cols
    inner_w = max(cell_w - 2 * pad, 0.01)
    norm = profile.canvas_norm
    # Роль кеглем текста (сборка с нуля и так не ставит текст мельче body),
    # имя на ступень крупнее, но не крупнее h2.
    role_pt = profile.type_scale_pt("body", 0.0) or 14.0
    name_pt = min(profile.type_scale_pt("h2", 0.0) or role_pt * _NAME_SCALE, role_pt * _NAME_SCALE)
    width_in = profile.canvas_width_emu / EMU_PER_INCH
    height_in = profile.canvas_height_emu / EMU_PER_INCH
    inner_in = inner_w * width_in
    name_lines = min(NAME_LINES, lines_needed([m.name for m in members], family, name_pt, inner_in))
    role_lines = min(ROLE_LINES, lines_needed([m.role for m in members], family, role_pt, inner_in))
    name_h = line_height(profile, name_pt, name_lines)
    role_h = line_height(profile, role_pt, role_lines) if any(m.role.strip() for m in members) else 0.0
    text_h = name_h + role_h
    # Диаметр в дюймах: кружок обязан быть кругом на холсте любых пропорций.
    row_budget = (region.height - (rows - 1) * gutter) / rows
    circle_h_max = max(0.0, min(row_budget - 2 * pad - _GAP - text_h, region.height * _CIRCLE_REGION_SHARE))
    diameter_in = max(0.0, min(inner_w * _CIRCLE_CELL_SHARE * width_in, circle_h_max * height_in))
    circle_w, circle_h = diameter_in / width_in, diameter_in / height_in
    cell_h = 2 * pad + circle_h + _GAP + text_h
    total_h = rows * cell_h + (rows - 1) * gutter
    top0 = region.top + max(0.0, (region.height - total_h) / 2)
    contents: list[SlotContent] = []
    decor: list[DecorShape] = []
    cards: list[MemberCard] = []
    for i, member in enumerate(members):
        r, c = divmod(i, cols)
        left = region.left + c * (cell_w + gutter)
        top = top0 + r * (cell_h + gutter)
        shape = plaque_at(plaque, Box(left, top, cell_w, cell_h))
        if shape is not None:
            decor.append(shape)
        circle = Box(left + (cell_w - circle_w) / 2, top + pad, circle_w, circle_h)
        photo = photos[i] if i < len(photos) else None
        if circle_h > 0 and photo is None and brand_hex:
            decor.append(DecorShape(
                kind="shape", box=circle, rotation=0.0, flip_h=False, flip_v=False, fill_hex=brand_hex,
                has_fill=True, fill_kind="solid", prst="ellipse", badge_text=initials(member.name),
                badge_size_pt=round(diameter_in * 72 * 0.36, 1), badge_color_hex=initials_hex,
            ))
        cards.append(MemberCard(member=member, circle=circle, photo=photo))
        name_box = Box(left + pad, circle.bottom + _GAP, inner_w, name_h)
        contents.append(SlotContent(text_slot("card_title", name_box, name_pt * norm, align="ctr"), "card_title",
                                    [Paragraph(member.name.strip())]))
        if member.role.strip():
            role_box = Box(left + pad, name_box.bottom, inner_w, role_h)
            contents.append(SlotContent(text_slot("card_body", role_box, role_pt * norm, align="ctr"), "card_body",
                                        [Paragraph(member.role.strip())]))
    return contents, decor, cards


def _square_bytes(path: Path) -> bytes | None:
    """Квадратная середина снимка PNG-байтами: круглая рамка без
    растяжения (аудит L07 сверяет пропорции рамки с пикселями снимка)."""
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            side = min(img.size)
            left, top = (img.width - side) // 2, (img.height - side) // 2
            square = img.crop((left, top, left + side, top + side))
            out = io.BytesIO()
            square.save(out, format="PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001: битое фото не роняет сборку, участник остаётся без фото
        return None


def place_member_photos(slide, cards: list[MemberCard], canvas_width_emu: int, canvas_height_emu: int) -> list[str]:
    """Фото участников в круглых рамках. Возвращает имена участников, чьё
    фото не прочиталось: вызывающий пишет находку."""
    failed = []
    for card in cards:
        if card.photo is None or card.circle.width <= 0:
            continue
        data = _square_bytes(card.photo)
        if data is None:
            failed.append(card.member.name)
            continue
        b = card.circle
        pic = slide.shapes.add_picture(
            io.BytesIO(data), Emu(round(b.left * canvas_width_emu)), Emu(round(b.top * canvas_height_emu)),
            Emu(max(1, round(b.width * canvas_width_emu))), Emu(max(1, round(b.height * canvas_height_emu))),
        )
        geom = pic._element.spPr.find(qn("a:prstGeom"))  # noqa: SLF001: круглая рамка снимка
        if geom is not None:
            geom.set("prst", "ellipse")
    return failed
