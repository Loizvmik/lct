"""Стиль генерации одной презентации: `dense`, `airy` или `visual`.

Раньше модуль описывал «три варианта вёрстки одного содержания»: текст
писался один раз, а `apply_variant` после него переставлял раскладки под
каждый вариант. С задачи P раскладки на всю колоду назначает планировщик
(`pattern.plan_patterns`) до текста, по стилю, а с задачи Q стиль стал
параметром ОДНОГО задания генерации (одна презентация, свой бюджет в 300 с),
а не одной из трёх веток внутри задания. Поэтому здесь осталось только
перечисление стилей.

Что стиль значит для колоды (целевая плотность, предпочитаемые виды
раскладок, вес декора, разделители разделов у `airy`), лежит в
`config/styles.yaml` и читается `pattern.style.load_style`: числа подбирают
по живым прогонам, правкой конфига, а не кода. Разделители `airy` вставляет
`pattern.intent.intents_from_outline`."""
from __future__ import annotations
from enum import Enum


class GenerationStyle(Enum):
    """Стиль одной презентации. Имена совпадают с ключами `config/styles.yaml`
    и со значением поля `style` в API."""

    dense = "dense"
    airy = "airy"
    visual = "visual"

    @classmethod
    def parse(cls, value: "str | GenerationStyle") -> "GenerationStyle":
        """Стиль из строки запроса или флага CLI. Неизвестное имя даёт
        понятную ошибку со списком допустимых, а не голый `ValueError` enum."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            allowed = ", ".join(s.value for s in cls)
            raise ValueError(f"неизвестный стиль {value!r}, ожидается один из: {allowed}") from None


# Старое имя. `compose.builder` и скрипты импортируют `Variant`, а сборку в
# этой задаче не трогаем: имя остаётся псевдонимом того же перечисления.
Variant = GenerationStyle
