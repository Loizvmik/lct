"""Выбор цвета текста по контрасту к заданному фону — общий для `charts.py`,
`tables.py` и `diagrams.py` (Task 10): все три штампуют заливки (шапка
таблицы, ряды графика, карточки схем) и должны положить поверх них текст,
который реально читается, а не выбранный на вкус чёрный/белый.

Отдельный крошечный модуль, а не три копии функции (в отличие, например, от
`_all_shape_bearing_parts`, продублированной в паре мест по всему проекту
как малозначащая деталь обхода пакета) — здесь три модуля были бы обязаны
разойтись в порогa/приоритетах контраста по мере правок, а разъехавшийся
контраст молча портит читаемость текста, то самое, ради чего требование
существует.
"""
from __future__ import annotations

from deckforge.template.naming import MIN_CONTRAST, contrast_ratio
from deckforge.template.profile import TemplateProfile

__all__ = [
    "MIN_CONTRAST", "best_contrast_text_color", "best_contrast_text_color_for_luminance",
    "slide_background_luminance", "surface_pair_for_luminance", "pop_pair_for_luminance",
]


def best_contrast_text_color(bg_hex: str, palette_roles: dict[str, str]) -> str:
    """Цвет ИЗ ПАЛИТРЫ ШАБЛОНА (не произвольный чёрный/белый), дающий
    наибольший контраст WCAG к `bg_hex`. Возвращает сам `bg_hex`, только
    если в палитре вовсе нет ни одного цвета (не должно происходить —
    `palette_roles` всегда несёт хотя бы запасной набор ролей, см.
    `template/naming.py::_fallback_roles`)."""
    candidates = list(dict.fromkeys(v for v in palette_roles.values() if v))
    if not candidates:
        return bg_hex
    return max(candidates, key=lambda c: contrast_ratio(bg_hex, c))


# ---------------------------------------------------------------------------
# Контраст к ФОНУ СЛАЙДА (не к явной заливке своей фигуры)
# ---------------------------------------------------------------------------
#
# Task 10 отчёт (визуальное ревью, находка №1): `add_chart` красил текст
# графика (шрифт/подписи осей) фиксированной ролью `on_surface` — читаемо,
# только если ФАКТИЧЕСКИЙ фон макета, на который лёг слайд, парный ей
# `surface` (эти две роли калибруются друг под друга при именовании палитры,
# см. naming.py). У графика нет собственной непрозрачной заливки под
# текстом (в отличие от ячейки таблицы/карточки схемы, где текст лежит на
# ЯВНОЙ заливке самой фигуры, см. `best_contrast_text_color` выше) — он
# рисуется прямо на фоне слайда. Раскладка VK Tech "free" (0 плейсхолдеров,
# выбрана демо-скриптом Task 10 именно для чистого фона под графики) на деле
# несёт свой собственный декоративный фон, не совпадающий с общей парой
# surface/on_surface, — заголовок и подписи осей вышли почти невидимыми
# (белым по белому). Тот же принцип, что уже чинил builder.
# `_local_background_luminance`/`_best_contrast_color` для текстовых слотов:
# фон МАКЕТА (`profile.layouts[].background`, разобран надёжно — Task 6/8) —
# источник истины, а не фиксированная роль.


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast_ratio_from_luminance(l_a: float, l_b: float) -> float:
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


def best_contrast_text_color_for_luminance(
    bg_luminance: float, palette_roles: dict[str, str], *, preferred_role: str = "on_surface",
) -> str:
    """Цвет ИЗ ПАЛИТРЫ, дающий контраст WCAG не ниже `MIN_CONTRAST` к
    относительной яркости фона `bg_luminance`. `preferred_role` используется
    как есть, если он и так контрастен (кегль/роль сохраняют смысл, текст
    не меняет цвет там, где и так всё читаемо); иначе — лучший по контрасту
    цвет из всей палитры (та же логика, что `builder._best_contrast_color`,
    только без плашки декора под слотом — у графика/схемы её нет)."""
    candidates = list(dict.fromkeys(v for v in palette_roles.values() if v))
    if not candidates:
        return palette_roles.get(preferred_role, "#000000")

    def contrast(hex_c: str) -> float:
        return _contrast_ratio_from_luminance(bg_luminance, _relative_luminance(hex_c))

    preferred = palette_roles.get(preferred_role)
    if preferred and contrast(preferred) >= MIN_CONTRAST:
        return preferred
    return max(candidates, key=contrast)


def surface_pair_for_luminance(bg_luminance: float, palette_roles: dict[str, str]) -> tuple[str, str]:
    """(заливка, цвет текста) для непрозрачной плашки (тело таблицы,
    карточка схемы), которая должна визуально принадлежать тому же
    "полюсу" светлый/тёмный, что и фон слайда под ней, а не быть случайно
    противоположной. `surface`/`on_surface` — единственная пара ролей,
    откалиброванная друг под друга по контрасту (≥ MIN_CONTRAST) при
    именовании палитры (naming.py); из них берётся та, чья ЯРКОСТЬ ближе к
    `bg_luminance`, как заливка, другая — как текст.

    Task 10 отчёт (визуальное ревью, находка №3): у VK Tech `surface` —
    чёрный (#000000), потому что бОльшая часть слайдов шаблона тёмная — но
    ДЕМО-раскладка Task 10 выбрана намеренно светлой ("free", 0
    плейсхолдеров). Без этой функции таблица/карточки схемы клали чёрную
    плашку на бледный фон слайда — не нечитаемо (контраст текста внутри
    плашки в порядке), но визуально чужеродно, будто вырезано из другого
    файла. Слепое `palette_roles["surface"]` предполагает один и тот же
    "полюс" фона на каждом лейауте шаблона — неверное допущение уже на
    примере VK Tech (часть лейаутов тёмная, часть светлая)."""
    surface = palette_roles.get("surface")
    on_surface = palette_roles.get("on_surface")
    if not surface or not on_surface:
        return surface or "#FFFFFF", on_surface or "#000000"
    surface_lum = _relative_luminance(surface)
    on_surface_lum = _relative_luminance(on_surface)
    if abs(surface_lum - bg_luminance) <= abs(on_surface_lum - bg_luminance):
        return surface, on_surface
    return on_surface, surface


def pop_pair_for_luminance(bg_luminance: float, palette_roles: dict[str, str]) -> tuple[str, str]:
    """(заливка, цвет текста) для НЕПРОЗРАЧНОЙ ФИГУРЫ, которая обязана
    визуально ВЫДЕЛЯТЬСЯ на фоне слайда (карточка схемы) — в отличие от
    `surface_pair_for_luminance` (тело таблицы, у которого есть шапка и
    сетка, и слиться с фоном для него не катастрофа), у карточки схемы нет
    ничего, что очертит её границу, кроме собственной заливки: если заливка
    того же полюса, что фон, карточка становится НЕВИДИМОЙ, а подпись
    повисает в воздухе без контейнера.

    Task 10 отчёт (визуальное ревью, находка №4): WorkSpace, `surface`
    (#000000) взят заливкой карточки на факти­чески ЧЁРНОМ фоне лейаута
    "free" — карточка растворилась в фоне целиком, видны были только
    текст и стрелки соединителей. Здесь берётся полюс surface/on_surface,
    чья яркость ДАЛЬШЕ от `bg_luminance` — заведомо контрастирует с фоном,
    другой полюс (гарантированно контрастен первому, пара откалибрована
    в naming.py) — текст."""
    surface = palette_roles.get("surface")
    on_surface = palette_roles.get("on_surface")
    if not surface or not on_surface:
        return surface or "#FFFFFF", on_surface or "#000000"
    surface_lum = _relative_luminance(surface)
    on_surface_lum = _relative_luminance(on_surface)
    if abs(surface_lum - bg_luminance) >= abs(on_surface_lum - bg_luminance):
        return surface, on_surface
    return on_surface, surface


def slide_background_luminance(slide, profile: TemplateProfile) -> float:
    """Относительная яркость фона МАКЕТА, на который реально положен
    `slide` — ищет лейаут `slide.slide_layout` в `profile.layouts` по имени
    парта пакета (тот же принцип, что `builder._local_background_luminance`:
    фон макета — источник истины, а не догадка по теме/паттерну). 1.0
    (светлый) — честный запасной вариант, когда лейаут не нашёлся в профиле
    (не должно происходить для слайда, собранного на файле того же
    шаблона, но так безопаснее, чем упасть или угадать тёмный)."""
    try:
        partname = str(slide.slide_layout.part.partname).lstrip("/")
    except Exception:
        return 1.0
    for layout in profile.layouts:
        if layout.part_name == partname:
            if layout.background.luminance is not None:
                return layout.background.luminance
            return 0.0 if layout.is_dark else 1.0
    return 1.0
