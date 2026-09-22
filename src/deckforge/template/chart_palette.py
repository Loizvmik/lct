"""Палитра рядов графика — цветные токены шаблона по убыванию веса, при
нехватке достроенные оттенками `brand`.

Без явной раскраски `python-pptx` отдаёт каждый ряд/точку цвету темы файла
(«accent1..6» из `a:clrScheme`) — на Google-экспортированных шаблонах эта
схема часто заглушка (см. `theme.ThemeInfo.is_stock_office_palette`), и
график выходит в стоковых цветах Office, а не в цветах шаблона (бриф Task
10 дословно). Эта функция строит список hex-цветов, которые `compose/
charts.py` обязан назначить явно каждому ряду/точке.
"""
from __future__ import annotations
import colorsys

from deckforge.ooxml.color import darken, lighten
from deckforge.template.usage import Usage

# Сколько цветов рядов нужно максимум одному графику. Запас: у Capacity
# намайненных паттернов (Task 7) `max_series` на трёх учебных шаблонах не
# превышает 4-5, а график презентационного слайда с большим числом рядов
# и так нечитаем на глаз — расширять дальше нет практического смысла.
MAX_CHART_SERIES = 5

# Роли палитры, которые НЕ годятся цветом ряда данных — это тон
# поверхности/текста/поля, а не акцентный цвет, различающий ряды. Именно
# эти роли (наравне с "brand"/"accent"/"danger"/"warning") чаще всего
# оказываются самыми частыми заливками шаблона (фон карточек, текст) —
# без фильтра график красился бы в оттенки серого/белого/чёрного.
_NEUTRAL_ROLES = ("surface", "on_surface", "background", "muted", "border")

# Живой замер на VK Tech без доп. фильтра: два первых цвета по весу —
# #C4C4C4 (серый) и #FEFFFF (почти белый) — ни один из них не совпадает
# БУКВАЛЬНО ни с одной из hex-заливок _NEUTRAL_ROLES (это декоративные
# полутона шаблона, а не сами роли), поэтому фильтр по совпадению строки
# выше их не ловит, хотя визуально это ровно то же самое "нейтральный
# фон/обводка", просто с другим hex. Дополнительный фильтр по HLS: серым
# (низкая насыщенность) и вымытым до почти белого/почти чёрного (крайняя
# светлота) цветам не место среди цветов, которые должны РАЗЛИЧАТЬ ряды на
# графике — они неотличимы от фона/обводки на глаз. Пороги не откалиброваны
# отдельно под каждый шаблон, взяты как типографски разумные "заметно
# серый"/"заметно вымыт" границы.
_MIN_SATURATION = 0.15
_MIN_LIGHTNESS = 0.15
_MAX_LIGHTNESS = 0.85


def _is_usable_series_color(hex_color: str) -> bool:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    _hue, lightness, saturation = colorsys.rgb_to_hls(r, g, b)
    return saturation >= _MIN_SATURATION and _MIN_LIGHTNESS <= lightness <= _MAX_LIGHTNESS

# Насколько нужно затемнить/осветлить `brand`, когда цветных токенов
# шаблона не хватило на MAX_CHART_SERIES рядов — числа из брифа задачи
# дословно ("оттенками brand — затемнение и осветление на 45% и 70%"),
# порядок (сначала тёмный, потом светлый на каждом шаге) даёт наибольший
# зрительный контраст между первыми двумя достроенными цветами.
_TINT_STEPS: tuple[tuple[str, float], ...] = (
    ("dark", 0.45), ("light", 0.45), ("dark", 0.70), ("light", 0.70),
)


def build_chart_series(
    usage: Usage, palette_roles: dict[str, str], *, max_series: int = MAX_CHART_SERIES,
) -> list[str]:
    """Цвета рядов/точек графика — цветные заливки шаблона (`Usage.fill`)
    по убыванию частоты употребления, без нейтральных ролей палитры,
    достроенные оттенками `brand`, если цветных токенов не хватило."""
    neutral_hexes = {
        v.upper() for role, v in palette_roles.items() if role in _NEUTRAL_ROLES and v
    }
    weight: dict[str, int] = {}
    for color, count in usage.fill.items():
        hex_ = getattr(color, "hex", None)
        if not hex_:
            continue
        hex_upper = hex_.upper()
        if hex_upper in neutral_hexes or not _is_usable_series_color(hex_upper):
            continue
        weight[hex_upper] = weight.get(hex_upper, 0) + count

    ordered = [hex_ for hex_, _ in sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))]
    series = list(dict.fromkeys(ordered))[:max_series]

    brand = palette_roles.get("brand")
    if brand:
        seen = {s.upper() for s in series}
        for direction, amount in _TINT_STEPS:
            if len(series) >= max_series:
                break
            tint = darken(brand, amount) if direction == "dark" else lighten(brand, amount)
            if tint.upper() not in seen:
                series.append(tint)
                seen.add(tint.upper())

    return series
