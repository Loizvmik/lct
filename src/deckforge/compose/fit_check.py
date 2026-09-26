"""Инструмент №1 агентного цикла `plan.writer` (Task 19): "влезает ли
предложенный текст в слот выбранной раскладки, прежде чем модель отдаст его
как финальный".

Живёт в `compose/`, не в `plan/` — намеренно, ровно по архитектурной границе
задачи ("`plan/` не импортирует `python-pptx` и не знает координат",
`plan/spec.py`, докстрока модуля). Здесь, в `compose/`, координаты — законная
рабочая единица (`PatternSlot.box`, `TemplateProfile.canvas_*_emu`); наружу,
в `plan.writer`, уходят только ПЛОСКИЕ числа (влезает/не влезает, на сколько
дюймов не хватило) — `plan/` зовёт `measure_fit(...)` как чёрный ящик (текст,
роль, профиль, вид раскладки) и ни разу не трогает `PatternSlot.box`/`Canvas`
само по себе.

Считает ОБЯЗАТЕЛЬНО через `compose.textfit.measure` (докстрока `textfit.py`:
"единственный способ измерить текст в проекте") — не через `estimate_slot_
chars` (площадная эвристика `template/patterns.py`, её же докстрока называет
её временной заглушкой) и не через отдельный расчёт. Два независимых
расчёта одного и того же — ровно то, от чего предостерегает докстрока
`textfit.py`: они бы разошлись молча, и агентный цикр ругался бы на то, чего
финальная сборка (`compose.builder.fits`) не видит, или наоборот."""
from __future__ import annotations

from deckforge.compose.textfit import measure

EMU_PER_INCH = 914400.0

# Роли заголовка — тот же интерлиньяж, что и `compose.builder._HEADING_
# ROLES`/`_line_spacing_for` (независимая копия по той же причине, что и
# везде в проекте: `compose.builder` не предназначен для импорта наружу как
# библиотека — первый настоящий сборщик .pptx, не стабильный API, см. его
# докстроку).
_HEADING_ROLES = frozenset({"headline"})


def _richest_slot(profile, kind: str, role: str):
    """Слот роли `role` среди раскладок `kind` этого шаблона с наибольшим
    `max_chars` — тот же принцип максимума, что `plan.writer._kind_capacity`
    уже применяет к агрегированной `Capacity` (см. её докстроку): во время
    письма ещё не известен КОНКРЕТНЫЙ `pattern_id` (тот выбирается позже —
    `pick_patterns`/`variants._pick_pattern_id`), поэтому замер идёт по
    самой вместительной раскладке этого вида, не по случайной первой
    попавшейся — заведомо не более строгий, чем то, что реально выберет
    подбор паттерна."""
    candidates = [
        (slot, pattern)
        for pattern in getattr(profile, "patterns", []) or []
        if pattern.kind == kind
        for slot in pattern.slots
        if slot.role == role and slot.max_chars > 0
    ]
    if not candidates:
        return None
    slot, _pattern = max(candidates, key=lambda pair: pair[0].max_chars)
    return slot


def slot_fit_geometry(profile, kind: str) -> dict[str, dict]:
    """Геометрия (плоские числа в дюймах) слотов раскладки `kind` — по
    одному, самому вместительному слоту на роль (см. `_richest_slot`).

    Возвращает словарь примитивов (`width_in`/`height_in`/`size_pt`/
    `font_family`/`line_spacing`), не `Box`/`PatternSlot` — даже если этот
    словарь однажды попадёт в `plan/` целиком, а не только через `measure_
    fit`, граница "план не знает координат" не пострадает: в нём нет ни
    одного объекта, несущего смысл координаты, только числа."""
    if profile is None:
        return {}
    roles = {slot.role for pattern in profile.patterns if pattern.kind == kind for slot in pattern.slots}
    geometry: dict[str, dict] = {}
    for role in roles:
        slot = _richest_slot(profile, kind, role)
        if slot is None:
            continue
        geo = _slot_geometry(slot, profile)
        if geo is not None:
            geometry[role] = geo
    return geometry


def _slot_geometry(slot, profile) -> dict | None:
    """Плоские числа одного слота: ширина и высота в дюймах, кегль,
    гарнитура, интерлиньяж. `None` у слота нулевого размера."""
    width_in = slot.box.width * getattr(profile, "canvas_width_emu", 0) / EMU_PER_INCH
    height_in = slot.box.height * getattr(profile, "canvas_height_emu", 0) / EMU_PER_INCH
    if width_in <= 0 or height_in <= 0:
        return None
    type_scale = profile.type_scale
    family = type_scale.families[0] if type_scale.families else "Arial"
    line_spacing = (
        type_scale.heading_line_spacing if slot.role in _HEADING_ROLES else type_scale.body_line_spacing
    )
    return {
        "width_in": width_in, "height_in": height_in,
        "size_pt": slot.size_pt, "font_family": family, "line_spacing": line_spacing,
    }


def _layout_slot(profile, layout_id: str, role: str):
    """Самый ёмкий слот роли `role` в одной конкретной раскладке."""
    pattern = next((p for p in getattr(profile, "patterns", []) or [] if p.pattern_id == layout_id), None)
    if pattern is None:
        return None
    slots = [s for s in pattern.slots if s.role == role and s.max_chars > 0]
    return max(slots, key=lambda s: s.max_chars) if slots else None


def measure_fit(text: str, role: str, profile, kind: str, *, layout_id: str | None = None) -> dict:
    """Инструмент агента: влезает ли `text` в слот роли `role` раскладки
    `kind` этого шаблона.

    Роль, которой нет среди слотов этого `kind` (например, модель спросила
    про `quote`, а раскладка его не несёт), — не ошибка инструмента: `fits=
    True` с пометкой `note`, реальную применимость роли к раскладке всё
    равно решает выбор `kind`/`pick_patterns`, а не этот замер. Честная
    деградация (нет геометрии -> "считаем, что влезло"), а не исключение —
    тот же принцип, что и у любого другого инструмента модели в проекте.

    `fill` — доля высоты слота, которую занял текст (0–1). «Влезает» отвечает
    только на половину вопроса: текст в 20 знаков в рамке на 250 влезает, но
    оставляет её на три четверти пустой, и слайд читается как черновик.
    Считается той же высотой, что и переполнение, чтобы два числа не
    расходились.

    `layout_id` сужает замер до одной раскладки: писатель уже выбрал её, и
    мерить по самой ёмкой раскладке вида значило бы занижать заполнение."""
    slot = _layout_slot(profile, layout_id, role) if layout_id and profile is not None else None
    if slot is not None:
        geo = _slot_geometry(slot, profile)
    else:
        geo = slot_fit_geometry(profile, kind).get(role)
    if geo is None:
        return {
            "fits": True, "overflow_in": 0.0, "lines": 0, "fill": 0.0,
            "note": f"нет слота роли {role!r} в раскладке {kind!r} этого шаблона — "
                    "замер недоступен, ориентируйся на capacity/max_chars_per_item",
        }
    metrics = measure(
        text or "", geo["font_family"], geo["size_pt"], geo["width_in"], line_spacing=geo["line_spacing"],
    )
    overflow_in = max(0.0, metrics.height_in - geo["height_in"])
    fill = min(1.0, metrics.height_in / geo["height_in"]) if (text or "").strip() else 0.0
    return {
        "fits": overflow_in <= 0.0,
        "overflow_in": round(overflow_in, 3),
        "lines": metrics.lines,
        "box_height_in": round(geo["height_in"], 3),
        "fill": round(fill, 2),
    }


# Доля вместимости места, в которую целится писатель (`slide_tools.
# list_layouts`, `agents/slide-writer/AGENT.md`). Не 1,0: вместимость —
# оценка по площади, и текст точно под неё переносится лишней строкой.
TARGET_SHARE = 0.8

# Роли, которые несут основное содержание слайда. Заголовок, подписи,
# показатели не в счёт: их длину задаёт смысл, а не размер рамки.
_MAIN_TEXT_ROLES = {"cards": ("card_body",), "text": ("body", "bullet"), "quote": ("quote", "body")}


def target_of(max_value: int | None) -> int | None:
    """Целевой объём места по его пределу (знаки или слова)."""
    return round(max_value * TARGET_SHARE) if max_value else None


def _main_text(slide_spec) -> tuple[str, str] | None:
    """`(группа ролей, текст)` основного содержания. Карточки меряются по
    самой длинной: если даже она заполнила место меньше чем наполовину,
    остальные тем более."""
    from deckforge.plan.spec import BulletBlock, CardBlock, QuoteBlock, TextBlock

    cards = [c.body for b in slide_spec.blocks if isinstance(b, CardBlock) for c in b.items]
    if cards:
        return "cards", max(cards, key=len)
    quotes = [b.text for b in slide_spec.blocks if isinstance(b, QuoteBlock)]
    if quotes:
        return "quote", quotes[0]
    lines: list[str] = []
    for block in slide_spec.blocks:
        if isinstance(block, TextBlock):
            lines.append(block.text)
        elif isinstance(block, BulletBlock):
            lines.extend(block.items)
    return ("text", "\n".join(lines)) if lines else None


def main_slot_fill(slide_spec, profile, *, layout_id: str | None = None) -> dict | None:
    """Заполнение крупнейшего текстового места раскладки основным
    содержанием слайда: по нему писатель решает, дописывать ли текст.

    Раскладка — выбранная писателем (`layout_id`), без неё все раскладки
    вида слайда. Крупнейшее место — по вместимости в знаках с учётом схемы
    (`slot_char_capacity`): рамка, в которую модель разбора велела писать
    три слова, не должна требовать абзаца.

    `target_fill` — цель в тех же единицах, что `fill` (доля высоты):
    `TARGET_SHARE` вместимости места, умноженная на долю рамки, которую эта
    вместимость занимает. `None` — у слайда нет основного текста или места
    под него."""
    from deckforge.template.patterns import slot_char_capacity

    if profile is None:
        return None
    main = _main_text(slide_spec)
    if main is None:
        return None
    group, text = main
    roles = _MAIN_TEXT_ROLES[group]
    patterns = [p for p in profile.patterns if p.pattern_id == layout_id] if layout_id else []
    if not patterns:
        patterns = [p for p in profile.patterns if p.kind == slide_spec.kind]
    slots = [
        (slot, pattern) for pattern in patterns for slot in pattern.slots
        if slot.role in roles and slot.max_chars > 0 and not (slot.ordinal or slot.fixed)
    ]
    if not slots:
        return None
    slot, pattern = max(slots, key=lambda pair: slot_char_capacity(pair[0]))
    capacity = slot_char_capacity(slot)
    result = measure_fit(text, slot.role, profile, pattern.kind, layout_id=pattern.pattern_id)
    if "note" in result:
        return None
    return {
        "role": slot.role,
        "layout_id": pattern.pattern_id,
        "chars": len(text),
        "max_chars": capacity,
        "target_chars": target_of(capacity),
        "fill": result["fill"],
        "target_fill": round(TARGET_SHARE * capacity / slot.max_chars, 2),
    }
