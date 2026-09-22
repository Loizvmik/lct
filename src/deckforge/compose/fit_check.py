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
    canvas_width_in = getattr(profile, "canvas_width_emu", 0) / EMU_PER_INCH
    canvas_height_in = getattr(profile, "canvas_height_emu", 0) / EMU_PER_INCH
    type_scale = profile.type_scale
    family = type_scale.families[0] if type_scale.families else "Arial"

    roles = {slot.role for pattern in profile.patterns if pattern.kind == kind for slot in pattern.slots}
    geometry: dict[str, dict] = {}
    for role in roles:
        slot = _richest_slot(profile, kind, role)
        if slot is None:
            continue
        width_in = slot.box.width * canvas_width_in
        height_in = slot.box.height * canvas_height_in
        if width_in <= 0 or height_in <= 0:
            continue
        line_spacing = (
            type_scale.heading_line_spacing if role in _HEADING_ROLES else type_scale.body_line_spacing
        )
        geometry[role] = {
            "width_in": width_in, "height_in": height_in,
            "size_pt": slot.size_pt, "font_family": family, "line_spacing": line_spacing,
        }
    return geometry


def measure_fit(text: str, role: str, profile, kind: str) -> dict:
    """Инструмент агента: влезает ли `text` в слот роли `role` раскладки
    `kind` этого шаблона.

    Роль, которой нет среди слотов этого `kind` (например, модель спросила
    про `quote`, а раскладка его не несёт), — не ошибка инструмента: `fits=
    True` с пометкой `note`, реальную применимость роли к раскладке всё
    равно решает выбор `kind`/`pick_patterns`, а не этот замер. Честная
    деградация (нет геометрии -> "считаем, что влезло"), а не исключение —
    тот же принцип, что и у любого другого инструмента модели в проекте."""
    geometry = slot_fit_geometry(profile, kind)
    geo = geometry.get(role)
    if geo is None:
        return {
            "fits": True, "overflow_in": 0.0, "lines": 0,
            "note": f"нет слота роли {role!r} в раскладке {kind!r} этого шаблона — "
                    "замер недоступен, ориентируйся на capacity/max_chars_per_item",
        }
    metrics = measure(
        text or "", geo["font_family"], geo["size_pt"], geo["width_in"], line_spacing=geo["line_spacing"],
    )
    overflow_in = max(0.0, metrics.height_in - geo["height_in"])
    return {
        "fits": overflow_in <= 0.0,
        "overflow_in": round(overflow_in, 3),
        "lines": metrics.lines,
        "box_height_in": round(geo["height_in"], 3),
    }
