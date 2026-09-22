"""Инструмент №2 агентного цикла `plan.writer` (Task 19): "встречается ли
это число/короткая фраза в исходных материалах этого слайда".

Модели промптом запрещено выдумывать цифры (`agents/slide-writer/AGENT.md`,
"Цифры и факты бери ТОЛЬКО из sources") — до этой задачи запрет проверялся
только ПОТОМ, визуальным аудитом по картинке, и только если его вообще
запустят (брифом задачи). Этот инструмент даёт модели способ свериться
самой, в момент письма, а не надеяться на дисциплину промпта.

Чистая работа со строками — ни коммуникации с моделью, ни координат, ни
`python-pptx`: живёт в `plan/`, а не в `compose/` (в отличие от `fit_check.
py`, у которого ровно обратная причина жить в `compose/`), граница задачи
("план не знает координат") здесь не задета вовсе — она про геометрию слота,
не про поиск подстроки в тексте источников."""
from __future__ import annotations

# Контекст вокруг найденного совпадения — с запасом, чтобы модель увидела не
# только само число, но и к чему оно относится (единицу измерения, выборку,
# оговорку о смещённости) и не спутала "цифра встречается" с "цифра значит
# то же самое". Круглое число порядка "полторы строки текста" в обе стороны.
_CONTEXT_MARGIN_CHARS = 60

# Сколько совпадений возвращать модели целиком — одного обычно достаточно
# для проверки, но число может встречаться в источнике несколько раз в
# разных контекстах (например, и в тексте, и в таблице) — от 2 до 5
# контекстов дают модели достаточно, чтобы заметить расхождение, не заваливая
# ответ тем же числом, повторённым по всему документу.
_MAX_CONTEXTS = 5

# Запятая как десятичный разделитель (русский текст источников, "98,5%") —
# при сравнении со строкой запроса ("98.5" или "98,5", как модель написала)
# обе формы должны совпасть. Неразрывный пробел/тонкий пробел — типографский
# разделитель тысяч в русских числах ("1 200"), убираем, чтобы "1200" и
# "1 200" сравнивались как одно и то же число.
_NUMERIC_NORMALIZE = str.maketrans({",": ".", " ": "", " ": ""})


def _normalize(text: str) -> str:
    return text.translate(_NUMERIC_NORMALIZE).lower()


def check_number_in_sources(query: str, source_text: str) -> dict:
    """Ищет `query` (число вроде "98,5%"/"6,2 ч" или короткую фразу) в
    `source_text` буквально и с нормализацией разделителя (см.
    `_NUMERIC_NORMALIZE`). Возвращает `found`/`occurrences`/`context`
    (первое совпадение с запасом вокруг, см. `_CONTEXT_MARGIN_CHARS`) и
    `all_contexts` (до `_MAX_CONTEXTS`, если совпадений несколько).

    Пустой `query` — не ошибка инструмента (модель могла ошибиться в
    аргументе), а честный "не найдено": пустая строка находится в любом
    тексте буквально, и `found=True` на пустом запросе было бы бесполезной
    ложной уверенностью."""
    query = (query or "").strip()
    haystack = source_text or ""
    if not query or not haystack:
        return {"found": False, "occurrences": 0, "context": None, "all_contexts": []}

    variants = {query}
    normalized_query = _normalize(query)
    variants.add(normalized_query)
    if normalized_query.endswith("%"):
        variants.add(normalized_query[:-1])

    normalized_haystack = _normalize(haystack)
    contexts: list[str] = []
    seen_positions: set[int] = set()
    for variant in variants:
        needle = _normalize(variant)
        if not needle:
            continue
        start = 0
        while True:
            idx = normalized_haystack.find(needle, start)
            if idx == -1:
                break
            if idx not in seen_positions:
                seen_positions.add(idx)
                lo = max(0, idx - _CONTEXT_MARGIN_CHARS)
                hi = min(len(haystack), idx + len(needle) + _CONTEXT_MARGIN_CHARS)
                contexts.append(haystack[lo:hi].strip())
            start = idx + max(len(needle), 1)

    return {
        "found": bool(contexts),
        "occurrences": len(contexts),
        "context": contexts[0] if contexts else None,
        "all_contexts": contexts[:_MAX_CONTEXTS],
    }
