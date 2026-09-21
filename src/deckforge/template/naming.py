"""Именование ролей палитры: модель предлагает, код проверяет.

Единственный модуль пакета `deckforge.template`, который реально ВЫЗЫВАЕТ
модель (см. докстроку пакета) — для присвоения частотным цветам шаблона
семантических ролей дизайн-системы (`brand`, `surface`, `on_surface`,
`accent`, `muted`, `border`, `danger`, `warning`). `profile.py` тоже
импортирует из `deckforge.provider` — но только тип `LLMProvider` для
сигнатуры параметра `namer`, самого вызова там нет.

Промпт — не строка в коде, а файл `agents/palette-namer/AGENT.md` (ТЗ требует
промпты файлами, не зашитыми в код, и версионирования агентов — версия несётся
фронтматтером файла). Код здесь читает файл, подставляет данные и, что
важнее, ПРОВЕРЯЕТ ответ модели, а не доверяет ему слепо:

- предложенный хекс обязан присутствовать во входной палитре — модели нельзя
  придумывать цвет, которого в шаблоне нет;
- контраст пары `surface`/`on_surface` пересчитывается кодом заново (не по
  слову модели), и если он ниже 4.5:1 (WCAG AA для обычного текста) — обе роли
  роли заменяются детерминированным запасным вариантом;
- без ключа (llm=None) модель вообще не вызывается — все роли берутся тем же
  запасным вариантом, и профиль обязан собраться целиком.

Запасной вариант — не заглушка "на случай сбоя", а полноценный
детерминированный алгоритм (частотный анализ + HSV), которым правила ролей
выполняются без единого обращения к сети.
"""
from __future__ import annotations
import colorsys
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import yaml

from deckforge.provider.base import LLMProvider
from deckforge.template.theme import ThemeInfo
from deckforge.template.usage import Usage
from deckforge.ooxml.color import Color

ROLES: tuple[str, ...] = (
    "brand", "surface", "on_surface", "accent", "muted", "border", "danger", "warning",
)

AGENT_PATH = Path(__file__).resolve().parents[3] / "agents" / "palette-namer" / "AGENT.md"

# WCAG AA для обычного текста (не крупного заголовка) — порог из самого
# промпта AGENT.md ("не ниже 4.5:1"), продублирован здесь как число, которое
# код обязан пересчитать сам, а не доверять слову модели.
MIN_CONTRAST = 4.5

# Начальный бюджет max_tokens именно для этого вызова — не дефолт
# LLMProvider.complete=4096 (Task 8 код-ревью, находка 3: на дефолте
# эскалация бюджета в yandex.py была правилом на каждом вызове, а не
# исключением, и стоила ~12 лишних секунд из ~24 на вызов, см. живой прогон
# `deckforge parse` в task-8-report.md). naming.py работает через абстрактный
# LLMProvider и ни с одним конкретным провайдером не связан — число не
# импортировано из deckforge.provider.yandex (как и MIN_CONTRAST выше не
# импортирован ниоткуда), но обязано оставаться ниже
# MAX_TOKENS_BUDGET_CAP=6144 в yandex.py: выше него эскалации (см.
# _post_with_budget_escalation) уже некуда идти.
#
# Число измерено, не угадано: живой прогон llm.complete(...) на всех четырёх
# шаблонах (три учебных + контрольный ЛЦТ2026), по 2-3 прогона на шаблон —
# qwen3.6 рассуждающая и тратит на reasoning больше, чем на сам ответ, с
# заметным разбросом между прогонами одного и того же шаблона.
# completion_tokens успешных ответов (finish_reason=stop, content не пуст):
# 4074, 4298, 4569, 4894, 4993, 5116, 5150, 5181, 5237, 5779, 6180. 5632
# покрывает 9 из 11 замеров без единой эскалации; оставшиеся два (5779,
# 6180) уходят в единственный оставшийся шаг эскалации до потолка 6144.
#
# Честная оговорка: сам потолок эскалации (6144) не гарантированно
# достаточен для этого вызова — один живой прогон (6180) превысил бы и его.
# Это отдельный риск роли palette_namer, не устраняемый одним лишь
# поднятием стартового бюджета, и не входит в объём этой находки.
_PALETTE_NAMING_INITIAL_MAX_TOKENS = 5632

# Насыщенность/яркость, ниже/выше которых цвет визуально серый, чёрный или
# белый — HSV, не откалибровано по конкретному шаблону: это общее свойство
# восприятия цвета (низкая насыщенность = не воспринимается как "оттенок"),
# а не наблюдение за тремя файлами разведки.
_NEUTRAL_SATURATION = 0.15
_NEUTRAL_MIN_VALUE = 0.10
_NEUTRAL_MAX_VALUE = 0.97

# Минимальная насыщенность, при которой цвет вообще может считаться "явно
# красным"/"явно тёплым (жёлто-оранжевым)" — та же логика, что и
# _NEUTRAL_SATURATION, но строже: danger/warning не должны загораться от
# бледно-розового/кремового фона.
_HUED_MIN_SATURATION = 0.35

# Минимальное угловое расстояние по кругу оттенков (градусы), при котором
# второй фирменный цвет (accent) визуально отличим от первого (brand) — не
# просто "тот же цвет чуть темнее". 20° — стандартный шаг, при котором два
# оттенка ещё уверенно различаются глазом на мониторе (меньше — это оттенки
# одного цвета, не два разных).
_MIN_HUE_SEPARATION = 20.0

# Сколько кандидатов surface/on_surface показываем модели в матрице
# контраста — не всю палитру целиком (на шаблоне с богатой иконотекой
# кандидатов десятки, а нужны только реалистичные фон/текст), а самые частые.
_MAX_CONTRAST_SURFACES = 4
_MAX_CONTRAST_TEXTS = 6


@dataclass(frozen=True)
class PaletteCandidate:
    """Один цвет входной палитры — из темы файла и/или фактического
    употребления на слайдах, с разбивкой по контексту (см. докстроку
    модуля usage.py про то, откуда берутся fill/text/line/layout_bg)."""

    hex: str
    from_theme: bool = False
    fill_count: int = 0
    text_chars: int = 0
    line_count: int = 0
    layout_bg_count: int = 0

    @property
    def weight(self) -> int:
        return self.fill_count + self.text_chars + self.line_count + self.layout_bg_count


NoteSeverity = Literal["info", "warning"]


@dataclass(frozen=True)
class PaletteNote:
    """Одна заметка о происхождении/деградации роли — текст для человека
    плюс структурный признак серьёзности (Task 8 код-ревью, находка 6):
    раньше `profile.py` искал предупреждения подстрокой в тексте заметки
    ("не удалось", "заменён запасным" и т.д.) — поменяется формулировка,
    предупреждение тихо исчезает. `severity` не зависит от текста."""

    text: str
    severity: NoteSeverity = "info"


@dataclass(frozen=True)
class PaletteNamingResult:
    """Результат именования палитры вместе с пояснением источника каждой
    роли — материал для `TemplateProfile.provenance`/`.warnings` (Task 8,
    "отчёт откуда что взято" распространяется и на роли палитры, не только
    на типографику/сетку)."""

    roles: dict[str, str]
    notes: list[PaletteNote]


def name_palette_roles(usage: Usage, theme: ThemeInfo, llm: LLMProvider | None) -> dict[str, str]:
    """Контракт интерфейса брифа (Step 1) дословно — только итоговая
    карта роль → hex. `TemplateProfile.from_file` использует более
    подробный `name_palette_roles_report` ради `.provenance`/`.warnings`."""
    return name_palette_roles_report(usage, theme, llm).roles


def name_palette_roles_report(
    usage: Usage, theme: ThemeInfo, llm: LLMProvider | None,
) -> PaletteNamingResult:
    candidates = _collect_candidates(usage, theme)
    fallback = _fallback_roles(candidates, theme)

    if llm is None:
        return PaletteNamingResult(
            roles=fallback,
            notes=[PaletteNote(
                "ключ модели не задан — все роли назначены детерминированным запасным вариантом",
                severity="info",
            )],
        )
    if not candidates:
        return PaletteNamingResult(
            roles={},
            notes=[PaletteNote(
                "во входной палитре нет ни одного цвета — назначать нечего", severity="warning",
            )],
        )

    result, notes = _ask_model(candidates, theme, llm, fallback)
    return PaletteNamingResult(roles=result, notes=notes)


# --------------------------------------------------------------------------
# Сбор входной палитры
# --------------------------------------------------------------------------

def _collect_candidates(usage: Usage, theme: ThemeInfo) -> dict[str, PaletteCandidate]:
    candidates: dict[str, PaletteCandidate] = {}

    def _bump(hex_raw: str, **kwargs: int) -> None:
        hex_val = hex_raw.upper()
        current = candidates.get(hex_val, PaletteCandidate(hex=hex_val))
        updates = {field: getattr(current, field) + value for field, value in kwargs.items()}
        candidates[hex_val] = replace(current, **updates)

    for hex_val in theme.scheme.values():
        if not hex_val:
            continue
        key = hex_val.upper()
        current = candidates.get(key, PaletteCandidate(hex=key))
        candidates[key] = replace(current, from_theme=True)

    for color, count in usage.fill.items():
        if isinstance(color, Color):
            _bump(color.hex, fill_count=count)
    for color, chars in usage.text.items():
        if isinstance(color, Color):
            _bump(color.hex, text_chars=chars)
    for color, count in usage.line.items():
        if isinstance(color, Color):
            _bump(color.hex, line_count=count)
    for color, count in usage.layout_bg.items():
        if isinstance(color, Color):
            _bump(color.hex, layout_bg_count=count)

    return candidates


# --------------------------------------------------------------------------
# Детерминированный запасной вариант
# --------------------------------------------------------------------------

def _hex_to_rgb01(hex_val: str) -> tuple[float, float, float]:
    h = hex_val.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _hsv(hex_val: str) -> tuple[float, float, float]:
    r, g, b = _hex_to_rgb01(hex_val)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return h * 360, s, v


def _is_neutral(hex_val: str) -> bool:
    _, s, v = _hsv(hex_val)
    return s < _NEUTRAL_SATURATION or v < _NEUTRAL_MIN_VALUE or v > _NEUTRAL_MAX_VALUE


def _hue_distance(a: str, b: str) -> float:
    ha, _, _ = _hsv(a)
    hb, _, _ = _hsv(b)
    d = abs(ha - hb) % 360
    return min(d, 360 - d)


def _is_reddish(hex_val: str) -> bool:
    h, s, _ = _hsv(hex_val)
    return s >= _HUED_MIN_SATURATION and (h >= 345 or h <= 15)


def _is_warm(hex_val: str) -> bool:
    h, s, _ = _hsv(hex_val)
    return s >= _HUED_MIN_SATURATION and 25 <= h <= 65


def _relative_luminance(hex_val: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _hex_to_rgb01(hex_val))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """Контраст WCAG между двумя цветами — то самое число, которым код (не
    модель) решает, годится ли пара surface/on_surface."""
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _pick_surface(candidates: dict[str, PaletteCandidate], theme: ThemeInfo) -> str | None:
    bg = [c for c in candidates.values() if c.layout_bg_count > 0]
    if bg:
        return max(bg, key=lambda c: c.layout_bg_count).hex
    for slot in ("lt1", "dk1"):
        hex_val = (theme.scheme.get(slot) or "").upper()
        if hex_val in candidates:
            return hex_val
    return None


def _pick_on_surface(candidates: dict[str, PaletteCandidate], surface: str | None) -> str | None:
    if surface is None:
        return None
    pool = [c for c in candidates.values() if c.text_chars > 0 and c.hex != surface]
    if not pool:
        pool = [c for c in candidates.values() if c.hex != surface]
    if not pool:
        return None
    return max(pool, key=lambda c: (contrast_ratio(surface, c.hex), c.text_chars)).hex


def _pick_brand(candidates: dict[str, PaletteCandidate], used: set[str], theme: ThemeInfo) -> str | None:
    accent1 = (theme.scheme.get("accent1") or "").upper()
    if accent1 in candidates and accent1 not in used and not _is_neutral(accent1):
        return accent1
    pool = [c for c in candidates.values() if c.hex not in used and not _is_neutral(c.hex) and c.fill_count > 0]
    if pool:
        return max(pool, key=lambda c: c.fill_count).hex
    pool = [c for c in candidates.values() if c.hex not in used and not _is_neutral(c.hex)]
    if pool:
        return max(pool, key=lambda c: c.weight).hex
    return None


def _pick_accent(
    candidates: dict[str, PaletteCandidate], used: set[str], theme: ThemeInfo, brand: str | None,
) -> str | None:
    def _distinct_enough(hex_val: str) -> bool:
        return brand is None or _hue_distance(hex_val, brand) >= _MIN_HUE_SEPARATION

    accent2 = (theme.scheme.get("accent2") or "").upper()
    if accent2 in candidates and accent2 not in used and not _is_neutral(accent2) and _distinct_enough(accent2):
        return accent2
    pool = [
        c for c in candidates.values()
        if c.hex not in used and not _is_neutral(c.hex) and _distinct_enough(c.hex)
    ]
    if pool:
        return max(pool, key=lambda c: c.fill_count + c.line_count).hex
    # Ни один кандидат не отличается по оттенку от brand на нужный угол —
    # второго фирменного цвета в палитре объективно нет, берём просто
    # следующий по частоте не-нейтральный цвет, не выдумывая различие.
    pool = [c for c in candidates.values() if c.hex not in used and not _is_neutral(c.hex)]
    if pool:
        return max(pool, key=lambda c: c.weight).hex
    return None


def _pick_muted(candidates: dict[str, PaletteCandidate], used: set[str]) -> str | None:
    pool = [c for c in candidates.values() if c.hex not in used and _is_neutral(c.hex) and c.weight > 0]
    if pool:
        return max(pool, key=lambda c: (c.text_chars, c.fill_count)).hex
    pool = [c for c in candidates.values() if c.hex not in used and c.text_chars > 0]
    if pool:
        return max(pool, key=lambda c: c.text_chars).hex
    return None


def _pick_border(candidates: dict[str, PaletteCandidate], used: set[str]) -> str | None:
    pool = [c for c in candidates.values() if c.hex not in used and c.line_count > 0]
    if pool:
        return max(pool, key=lambda c: c.line_count).hex
    pool = [c for c in candidates.values() if c.hex not in used and _is_neutral(c.hex) and c.weight > 0]
    if pool:
        return max(pool, key=lambda c: c.weight).hex
    return None


def _pick_hued(candidates: dict[str, PaletteCandidate], used: set[str], predicate) -> str | None:
    pool = [c for c in candidates.values() if c.hex not in used and predicate(c.hex)]
    if not pool:
        return None
    return max(pool, key=lambda c: c.weight).hex


def _fallback_roles(candidates: dict[str, PaletteCandidate], theme: ThemeInfo) -> dict[str, str]:
    """Полноценный детерминированный алгоритм, не заглушка на случай сбоя:
    частотный анализ контекста (fill/text/line/layout_bg) плюс HSV для
    нейтральности/оттенка. `danger`/`warning` остаются не назначенными
    (роль отсутствует в словаре), если в палитре нет по-настоящему
    красного/тёплого цвета — ровно то же правило, что дано модели в
    AGENT.md, соблюдённое кодом без единого обращения к сети."""
    if not candidates:
        return {}
    roles: dict[str, str] = {}
    used: set[str] = set()

    surface = _pick_surface(candidates, theme)
    if surface:
        roles["surface"] = surface
        used.add(surface)

    on_surface = _pick_on_surface(candidates, surface)
    if on_surface:
        roles["on_surface"] = on_surface
        used.add(on_surface)

    brand = _pick_brand(candidates, used, theme)
    if brand:
        roles["brand"] = brand
        used.add(brand)

    accent = _pick_accent(candidates, used, theme, brand)
    if accent:
        roles["accent"] = accent
        used.add(accent)

    muted = _pick_muted(candidates, used)
    if muted:
        roles["muted"] = muted
        used.add(muted)

    border = _pick_border(candidates, used)
    if border:
        roles["border"] = border
        used.add(border)

    danger = _pick_hued(candidates, used, _is_reddish)
    if danger:
        roles["danger"] = danger
        used.add(danger)

    warning = _pick_hued(candidates, used, _is_warm)
    if warning:
        roles["warning"] = warning
        used.add(warning)

    return roles


# --------------------------------------------------------------------------
# Вызов модели и проверка ответа
# --------------------------------------------------------------------------

def _load_agent_prompt(path: Path = AGENT_PATH) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path}: ожидался YAML-фронтматтер, ограниченный `---`")
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


def _build_input_payload(candidates: dict[str, PaletteCandidate], theme: ThemeInfo) -> dict:
    ordered = sorted(candidates.values(), key=lambda c: -c.weight)
    palette = []
    for c in ordered:
        contexts: dict[str, int] = {}
        if c.fill_count:
            contexts["fill_shapes"] = c.fill_count
        if c.text_chars:
            contexts["text_chars"] = c.text_chars
        if c.line_count:
            contexts["line_shapes"] = c.line_count
        if c.layout_bg_count:
            contexts["layout_backgrounds"] = c.layout_bg_count
        palette.append({"hex": c.hex, "in_theme": c.from_theme, "contexts": contexts})

    surfaces = [c.hex for c in ordered if c.layout_bg_count > 0][:_MAX_CONTRAST_SURFACES]
    if not surfaces:
        surfaces = [
            (theme.scheme.get(slot) or "").upper()
            for slot in ("lt1", "dk1")
            if (theme.scheme.get(slot) or "").upper() in candidates
        ]
    texts = [c.hex for c in ordered if c.text_chars > 0][:_MAX_CONTRAST_TEXTS]

    contrast_pairs = [
        {"surface": s, "on_surface": t, "contrast": round(contrast_ratio(s, t), 2)}
        for s in surfaces for t in texts if s != t
    ]
    return {"palette": palette, "contrast_pairs": contrast_pairs}


_ROLE_SCHEMA = {
    "type": "object",
    "properties": {role: {"type": "string"} for role in ROLES},
    "additionalProperties": False,
}


def _ask_model(
    candidates: dict[str, PaletteCandidate],
    theme: ThemeInfo,
    llm: LLMProvider,
    fallback: dict[str, str],
) -> tuple[dict[str, str], list[PaletteNote]]:
    notes: list[PaletteNote] = []
    _meta, prompt_body = _load_agent_prompt()
    payload = _build_input_payload(candidates, theme)
    messages = [
        {"role": "system", "content": prompt_body},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    try:
        raw = llm.complete(
            messages, schema=_ROLE_SCHEMA, max_tokens=_PALETTE_NAMING_INITIAL_MAX_TOKENS,
        )
        proposed = json.loads(raw)
        if not isinstance(proposed, dict):
            raise ValueError(f"ожидался объект JSON, получено {type(proposed).__name__}")
    except Exception as exc:  # сеть/модель — не должны ронять сборку профиля
        notes.append(PaletteNote(
            f"именование ролей палитры моделью не удалось ({exc}) — все роли назначены "
            "детерминированным запасным вариантом",
            severity="warning",
        ))
        return dict(fallback), notes

    result: dict[str, str] = {}
    valid_hexes = set(candidates)
    for role in ROLES:
        if role not in proposed:
            if role in fallback:
                result[role] = fallback[role]
                notes.append(PaletteNote(
                    f"{role}: модель не высказалась — взят запасной вариант {fallback[role]}",
                    severity="warning",
                ))
            continue
        value = proposed.get(role)
        if not isinstance(value, str) or not value:
            # Модель осознанно оставила роль пустой (валидно для danger/warning
            # без красного/жёлтого в палитре, брифом AGENT.md) — уважаем это,
            # даже если у запасного варианта была своя догадка. Это не
            # деградация источника, а корректный ответ модели.
            notes.append(PaletteNote(f"{role}: модель оставила роль пустой", severity="info"))
            continue
        hex_val = value.upper()
        if hex_val not in valid_hexes:
            notes.append(PaletteNote(
                f"{role}: модель предложила цвет {hex_val}, которого нет во входной палитре — "
                "заменён запасным вариантом" + (f" {fallback[role]}" if role in fallback else " (нет)"),
                severity="warning",
            ))
            if role in fallback:
                result[role] = fallback[role]
            continue
        result[role] = hex_val

    surface, on_surface = result.get("surface"), result.get("on_surface")
    if surface and on_surface:
        contrast = contrast_ratio(surface, on_surface)
        if contrast < MIN_CONTRAST:
            notes.append(PaletteNote(
                f"surface/on_surface: контраст {contrast:.2f}:1 ниже порога {MIN_CONTRAST}:1 — "
                "обе роли заменены запасным вариантом",
                severity="warning",
            ))
            if "surface" in fallback:
                result["surface"] = fallback["surface"]
            else:
                result.pop("surface", None)
            if "on_surface" in fallback:
                result["on_surface"] = fallback["on_surface"]
            else:
                result.pop("on_surface", None)
    elif surface or on_surface:
        # Модель назначила только одну сторону пары — вторую восполняем
        # запасным вариантом и перепроверяем контраст комбинации.
        if "surface" in fallback and "surface" not in result:
            result["surface"] = fallback["surface"]
        if "on_surface" in fallback and "on_surface" not in result:
            result["on_surface"] = fallback["on_surface"]
        if "surface" in result and "on_surface" in result:
            if contrast_ratio(result["surface"], result["on_surface"]) < MIN_CONTRAST:
                notes.append(PaletteNote(
                    "surface/on_surface: комбинация модель+фолбэк не прошла контраст — "
                    "взята пара целиком из фолбэка",
                    severity="warning",
                ))
                if "surface" in fallback:
                    result["surface"] = fallback["surface"]
                if "on_surface" in fallback:
                    result["on_surface"] = fallback["on_surface"]

    if not notes:
        notes.append(PaletteNote(
            f"роли {', '.join(sorted(result))} назначены моделью и приняты кодом без замен",
            severity="info",
        ))
    return result, notes
