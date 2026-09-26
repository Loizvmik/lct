"""Стиль генерации как целевая функция планировщика (`config/styles.yaml`).

Раньше стиль (вариант `dense`/`airy`/`visual`) решался после текста:
`plan.variants.apply_variant` переставлял раскладки под уже написанное
содержание, и вкусу варианта часто было не из чего выбирать, потому что
длинный текст плотного варианта не влезал в нарядные раскладки. Теперь
стиль входит в стоимость назначения паттерна, а текст пишется после, под
выбранную композицию. Числа живут в конфиге: их подбирают по живым
прогонам, а не правкой кода."""
from __future__ import annotations
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

STYLES_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "styles.yaml"

STYLE_NAMES = ("dense", "airy", "visual")

# Запасные значения на случай нечитаемого конфига: генерация не должна
# падать из-за файла настроек, как и везде в проекте.
_DEFAULT_WEIGHTS = {
    "overflow": 1000.0, "consecutive_repeat": 500.0, "repeated_pattern": 30.0,
    "style_mismatch": 20.0, "density_mismatch": 10.0, "pattern_quality": 5.0,
    "decor": 10.0, "orphan_image": 15.0, "cover_miss": 50.0, "short_headline": 60.0, "sample_photo_void": 200.0,
}
_DEFAULT_STYLES = {
    "dense": {
        "target_density": 0.80, "prefer": ["table", "cards", "two_col", "kpi", "bullets"],
        "words_per_item": 16, "min_words_per_item": 8, "decor_weight": 0.5, "dividers": False,
    },
    "airy": {
        "target_density": 0.50, "prefer": ["section", "quote", "kpi_caption", "sparse_cards", "kpi"],
        "words_per_item": 9, "min_words_per_item": 4, "decor_weight": 0.5, "dividers": True,
    },
    "visual": {
        "target_density": 0.55, "prefer": ["image", "photo_text", "chart", "diagram", "cards"],
        "words_per_item": 10, "min_words_per_item": 4, "decor_weight": 2.0, "dividers": False,
    },
}


# Ограничения разнообразия уровня колоды (задача V2): не штраф, а запрет,
# который действует, только пока у слайда есть совместимая альтернатива.
# Запасные значения на случай конфига без раздела `diversity`.
_DEFAULT_DIVERSITY = {
    "same_pattern_max_total": 3,
    "same_pattern_max_consecutive": 1,
    "same_kind_max_consecutive": {"cards": 2, "bullets": 2},
}


@dataclass(frozen=True)
class Diversity:
    same_pattern_max_total: int = 3
    same_pattern_max_consecutive: int = 1
    same_kind_max_consecutive: tuple[tuple[str, int], ...] = (("cards", 2), ("bullets", 2))

    def kind_limit(self, kind: str) -> int | None:
        return dict(self.same_kind_max_consecutive).get(kind)


def _diversity(raw: dict | None) -> Diversity:
    data = {**_DEFAULT_DIVERSITY, **(raw or {})}
    kinds = data.get("same_kind_max_consecutive") or {}
    return Diversity(
        same_pattern_max_total=int(data["same_pattern_max_total"]),
        same_pattern_max_consecutive=int(data["same_pattern_max_consecutive"]),
        same_kind_max_consecutive=tuple(sorted((str(k), int(v)) for k, v in kinds.items())),
    )


@dataclass(frozen=True)
class StylePolicy:
    name: str
    target_density: float
    prefer: tuple[str, ...]
    words_per_item: int
    min_words_per_item: int
    decor_weight: float
    dividers: bool
    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    diversity: Diversity = field(default_factory=Diversity)

    def weight(self, name: str) -> float:
        return float(self.weights.get(name, _DEFAULT_WEIGHTS.get(name, 0.0)))


def style_name(style) -> str:
    """Имя стиля из строки или `plan.variants.Variant` (у него `.value`):
    пакет не импортирует `Variant`, чтобы не зависеть от устаревшего пути."""
    name = getattr(style, "value", style)
    if name not in STYLE_NAMES:
        raise ValueError(f"неизвестный стиль {style!r}, ожидается один из {STYLE_NAMES}")
    return name


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict, dict]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001: без конфига работают запасные числа
        return dict(_DEFAULT_WEIGHTS), dict(_DEFAULT_STYLES)
    weights = {**_DEFAULT_WEIGHTS, **(data.get("weights") or {})}
    styles = {**_DEFAULT_STYLES, **(data.get("styles") or {})}
    return weights, styles


@lru_cache(maxsize=4)
def _load_diversity(path: str) -> Diversity:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001: без конфига работают запасные числа
        return _diversity(None)
    return _diversity(data.get("diversity"))


def load_style(style, path: Path | None = None) -> StylePolicy:
    name = style_name(style)
    weights, styles = _load(str(path or STYLES_YAML_PATH))
    raw = {**_DEFAULT_STYLES[name], **(styles.get(name) or {})}
    return StylePolicy(
        name=name,
        target_density=float(raw["target_density"]),
        prefer=tuple(raw["prefer"]),
        words_per_item=int(raw["words_per_item"]),
        min_words_per_item=int(raw["min_words_per_item"]),
        decor_weight=float(raw["decor_weight"]),
        dividers=bool(raw["dividers"]),
        weights={k: float(v) for k, v in weights.items()},
        diversity=_load_diversity(str(path or STYLES_YAML_PATH)),
    )
