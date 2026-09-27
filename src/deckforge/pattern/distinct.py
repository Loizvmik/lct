"""Различимость вариантов пакета (задача D3): насколько три стиля одного
содержания разные для глаза.

ТЗ: «три варианта вёрстки одного содержания на одном шаблоне, визуально
различимых»; организаторы оценивают сами различия. Мера здесь по обликам
раскладок (`scoring.look_key`), а не по `pattern_id`: два примера с одной
геометрией глаз не различит. Сравнение по пункту структуры, а не по номеру
слайда: разделители airy и слияния dense сдвигают номера, и совпадение
раскладок на одном содержании иначе терялось бы. Героические слайды
(обложка, финал, разделители) не считаются: их раскладку задаёт шаблон."""
from __future__ import annotations
import hashlib
from itertools import combinations

from deckforge.pattern.scoring import look_key
from deckforge.pattern.style import batch_max_overlap


def short_look(look: str) -> str:
    """Короткий ярлык облика для отчёта: полный ключ длинный и нечитаем."""
    return hashlib.sha1(look.encode("utf-8")).hexdigest()[:6]


def slide_looks(assignments, profile) -> list[dict]:
    """Облик каждого слайда колоды: пункты структуры, раскладка, ярлык
    облика, героический ли слайд. Снимок для сводки пакета."""
    patterns = {p.pattern_id: p for p in getattr(profile, "patterns", None) or ()}
    out = []
    for a in assignments:
        p = patterns.get(a.pattern_id)
        out.append({
            "position": a.position,
            "outline": list(a.intent.outline_indices),
            "pattern_id": a.pattern_id,
            "kind": a.kind,
            "look": short_look(look_key(p)) if p is not None else None,
            "hero": bool(a.intent.is_hero),
        })
    return out


def _by_item(looks: list[dict]) -> dict[int, str]:
    out: dict[int, str] = {}
    for s in looks:
        if s["hero"] or s["look"] is None:
            continue
        for i in s["outline"]:
            out[i] = s["look"]
    return out


def distinctness(looks_by_style: dict[str, list[dict]], *, limit: float | None = None) -> dict:
    """Сводка различимости: для каждой пары стилей доля общих пунктов
    содержания с одним обликом раскладки, у каждого стиля число разных
    обликов, наибольшая доля и уложилась ли она в цель (`batch.max_overlap`
    в `config/styles.yaml`)."""
    limit = batch_max_overlap() if limit is None else limit
    items = {style: _by_item(looks) for style, looks in looks_by_style.items()}
    sets = {style: set(by.values()) for style, by in items.items()}
    pairs = {}
    for a, b in combinations(list(looks_by_style), 2):
        common = sorted(set(items[a]) & set(items[b]))
        same = [i for i in common if items[a][i] == items[b][i]]
        union = sets[a] | sets[b]
        pairs[f"{a}/{b}"] = {
            "positions": len(common), "same": len(same),
            "overlap": round(len(same) / len(common), 3) if common else 0.0,
            # Доля общих обликов в наборах двух колод, без учёта позиции:
            # по позициям стили могут расходиться перестановкой одних и
            # тех же раскладок, и честная сводка это показывает.
            "shared_looks": round(len(sets[a] & sets[b]) / len(union), 3) if union else 0.0,
        }
    worst = max((p["overlap"] for p in pairs.values()), default=0.0)
    return {
        "pairs": pairs,
        "unique_looks": {
            style: len({s["look"] for s in looks if s["look"] is not None and not s["hero"]})
            for style, looks in looks_by_style.items()
        },
        "slides": {style: len(looks) for style, looks in looks_by_style.items()},
        "max_overlap": worst,
        "limit": limit,
        "ok": worst <= limit,
    }
