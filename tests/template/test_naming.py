"""Тесты `naming.py` без сети (Task 8 код-ревью, находка 5): поддельный
провайдер вместо живого вызова. `LLMProvider` (deckforge/provider/base.py)
несёт единственный абстрактный метод `complete` — `FakeLLMProvider` ниже его
реализует детерминированно, без похода в сеть, и попутно запоминает, чем его
вызвали (пригождается для проверки бюджета токенов, находка 3).

Покрывает ветки валидации «модель предлагает, код проверяет» (докстрока
модуля naming.py): ответ валиден; цвет не из входной палитры; контраст ниже
порога; модель прислала только одну сторону пары surface/on_surface; ответ
не разбирается как JSON.

Заодно покрывает структурный признак серьёзности заметок (находка 6):
`PaletteNote.severity` вместо поиска подстрок в тексте в profile.py.
"""
from __future__ import annotations
import json
from collections import Counter

from deckforge.ooxml.color import Color
from deckforge.provider.base import LLMProvider
from deckforge.provider.yandex import MAX_TOKENS_BUDGET_CAP
from deckforge.template.naming import (
    _PALETTE_NAMING_INITIAL_MAX_TOKENS,
    _fallback_roles,
    contrast_ratio,
    name_palette_roles_report,
)
from deckforge.template.naming import _collect_candidates
from deckforge.template.theme import ThemeInfo
from deckforge.template.usage import Usage


class FakeLLMProvider(LLMProvider):
    """Единственный абстрактный метод LLMProvider — `complete`. Отдаёт
    заранее заданный ответ (строку) либо бросает заданное исключение —
    без единого обращения к сети. Запоминает kwargs каждого вызова."""

    def __init__(self, response: str | Exception):
        self.response = response
        self.calls: list[dict] = []

    def complete(self, messages, *, schema=None, max_tokens=4096, temperature=0.3) -> str:
        self.calls.append(
            {"messages": messages, "schema": schema, "max_tokens": max_tokens, "temperature": temperature}
        )
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _theme(**overrides) -> ThemeInfo:
    base = dict(
        scheme={}, clr_map={}, major_font="Inter", minor_font="Inter",
        scheme_name="Custom", font_scheme_degraded=False, text_styles_degraded=False,
        is_stock_office_palette=False,
    )
    base.update(overrides)
    return ThemeInfo(**base)


def _usage(**overrides) -> Usage:
    base = dict(fill=Counter(), text=Counter(), line=Counter(), layout_bg=Counter())
    base.update(overrides)
    return Usage(**base)


# --- Находка 3: начальный бюджет max_tokens этого вызова ---

def test_initial_budget_is_above_default_and_below_escalation_cap():
    """Начальный бюджет вызова именования — не дефолт LLMProvider (4096, на
    котором эскалация была правилом, а не исключением) и не сам потолок
    эскалации (иначе эскалировать было бы уже некуда)."""
    assert 4096 < _PALETTE_NAMING_INITIAL_MAX_TOKENS < MAX_TOKENS_BUDGET_CAP


# --- Ветка 1: валидный ответ ---

def test_valid_model_response_is_accepted_without_replacement():
    usage = _usage(
        layout_bg=Counter({Color(hex="#FFFFFF"): 5}),
        text=Counter({Color(hex="#000000"): 500}),
        fill=Counter({Color(hex="#0057FF"): 10, Color(hex="#FF3300"): 3, Color(hex="#777777"): 2}),
        line=Counter({Color(hex="#CCCCCC"): 4}),
    )
    theme = _theme()
    response = {
        "brand": "#0057FF", "surface": "#FFFFFF", "on_surface": "#000000",
        "accent": "#FF3300", "muted": "#777777", "border": "#CCCCCC",
        "danger": "#FF0000", "warning": "#FFCC00",
    }
    # danger/warning не входят в usage выше нарочно: код проверяет только
    # «цвет есть во входной палитре» — добавим их прямо в тему, чтобы они
    # тоже были валидными кандидатами.
    theme = _theme(scheme={"accent1": "#FF0000", "accent2": "#FFCC00"})
    namer = FakeLLMProvider(json.dumps(response))

    result = name_palette_roles_report(usage, theme, namer)

    assert result.roles == response
    assert len(namer.calls) == 1
    assert namer.calls[0]["max_tokens"] == _PALETTE_NAMING_INITIAL_MAX_TOKENS
    assert all(note.severity == "info" for note in result.notes)
    assert any("без замен" in note.text for note in result.notes)
    assert result.source == "model"


# --- Ветка 2: цвет не из входной палитры ---

def test_color_not_in_input_palette_is_replaced_with_fallback():
    # #0044AA, не #0057FF: чистый синий (#0057FF) имеет HSV V=1.0 (максимум
    # канала = 255) и _is_neutral() в naming.py его бракует как «нейтральный»
    # (V > _NEUTRAL_MAX_VALUE=0.97) — не подходит на роль brand в фолбэке.
    # #0044AA (V≈0.667) — то же самое по смыслу, но не задевает эту границу.
    usage = _usage(
        layout_bg=Counter({Color(hex="#FFFFFF"): 5}),
        text=Counter({Color(hex="#000000"): 500}),
        fill=Counter({Color(hex="#0044AA"): 10}),
    )
    theme = _theme()
    candidates = _collect_candidates(usage, theme)
    fallback = _fallback_roles(candidates, theme)
    assert "brand" in fallback  # предпосылка теста

    response = dict(fallback)
    response["brand"] = "#ABCDEF"  # этого цвета нет во входной палитре
    namer = FakeLLMProvider(json.dumps(response))

    result = name_palette_roles_report(usage, theme, namer)

    assert result.roles["brand"] == fallback["brand"]
    warning_notes = [n for n in result.notes if n.severity == "warning"]
    assert any("нет во входной палитре" in n.text and "brand" in n.text for n in warning_notes)
    for role, hexv in fallback.items():
        if role == "brand":
            continue
        assert result.roles[role] == hexv
    # Модель ответила (пусть код и заменил один цвет фолбэком) — это всё
    # ещё источник "model" для кеша (Task 8, находка 2), не "fallback":
    # "fallback" зарезервирован за случаями, когда модель не была вызвана
    # вовсе или её ответ целиком отброшен (см. тесты ниже).
    assert result.source == "model"


# --- Ветка 3: контраст пары ниже порога ---

def test_low_contrast_surface_pair_is_replaced_with_fallback():
    usage = _usage(
        layout_bg=Counter({Color(hex="#FFFFFF"): 5}),
        text=Counter({Color(hex="#000000"): 500}),
        fill=Counter({Color(hex="#DDDDDD"): 1, Color(hex="#EEEEEE"): 1}),
    )
    theme = _theme()
    assert contrast_ratio("#DDDDDD", "#EEEEEE") < 4.5  # предпосылка теста

    response = {"surface": "#DDDDDD", "on_surface": "#EEEEEE"}
    namer = FakeLLMProvider(json.dumps(response))

    result = name_palette_roles_report(usage, theme, namer)

    assert result.roles["surface"] == "#FFFFFF"
    assert result.roles["on_surface"] == "#000000"
    warning_notes = [n for n in result.notes if n.severity == "warning"]
    assert any("контраст" in n.text and "ниже порога" in n.text for n in warning_notes)


# --- Ветка 4: модель прислала только одну сторону пары ---

def test_model_supplies_only_one_side_of_the_pair():
    usage = _usage(
        layout_bg=Counter({Color(hex="#FFFFFF"): 5}),
        text=Counter({Color(hex="#000000"): 500}),
    )
    theme = _theme()
    candidates = _collect_candidates(usage, theme)
    fallback = _fallback_roles(candidates, theme)
    assert "on_surface" in fallback  # предпосылка теста

    response = {"surface": "#FFFFFF", "on_surface": ""}
    namer = FakeLLMProvider(json.dumps(response))

    result = name_palette_roles_report(usage, theme, namer)

    assert result.roles["surface"] == "#FFFFFF"
    assert result.roles["on_surface"] == fallback["on_surface"]
    assert contrast_ratio(result.roles["surface"], result.roles["on_surface"]) >= 4.5
    assert any(
        "on_surface" in n.text and "пустой" in n.text and n.severity == "info"
        for n in result.notes
    )


# --- Ветка 5: ответ не разбирается как JSON ---

def test_unparseable_json_response_falls_back_entirely():
    usage = _usage(
        layout_bg=Counter({Color(hex="#FFFFFF"): 5}),
        text=Counter({Color(hex="#000000"): 500}),
        fill=Counter({Color(hex="#0057FF"): 10}),
    )
    theme = _theme()
    candidates = _collect_candidates(usage, theme)
    fallback = _fallback_roles(candidates, theme)
    namer = FakeLLMProvider("это не JSON и никогда им не станет")

    result = name_palette_roles_report(usage, theme, namer)

    assert result.roles == fallback
    assert len(result.notes) == 1
    assert result.notes[0].severity == "warning"
    assert "не удалось" in result.notes[0].text
    assert result.source == "fallback"


# --- Структурная серьёзность: остальные ветки ---

def test_no_key_configured_is_informational_not_warning():
    usage = _usage(fill=Counter({Color(hex="#0057FF"): 1}))
    theme = _theme()
    result = name_palette_roles_report(usage, theme, None)
    assert len(result.notes) == 1
    assert result.notes[0].severity == "info"
    assert result.source == "fallback"


def test_no_candidates_at_all_is_a_warning():
    usage = _usage()
    theme = _theme()
    namer = FakeLLMProvider(json.dumps({}))
    result = name_palette_roles_report(usage, theme, namer)
    assert result.roles == {}
    assert len(result.notes) == 1
    assert result.notes[0].severity == "warning"
    assert namer.calls == []  # без кандидатов модель вообще не дёргаем
    assert result.source == "empty"
