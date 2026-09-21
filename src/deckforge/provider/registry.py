"""Проверка модели на соответствие ТЗ до первого запроса, а не после счёта."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import yaml
from pydantic import BaseModel

ALLOWED_LICENSES = {"Apache-2.0", "MIT"}
MAX_PARAMS_B = 35
REGISTRY_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"


class ModelNotAllowed(RuntimeError):
    pass


class ModelCard(BaseModel):
    id: str
    provider: str
    hf: str
    license: str
    params_b: float
    active_params_b: float | None = None
    vision: bool = False
    roles: list[str] = []


@lru_cache(maxsize=1)
def _registry(path: str = str(REGISTRY_PATH)) -> tuple[dict[str, ModelCard], dict[str, str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    allowed = {m["id"]: ModelCard(**m) for m in data.get("models", [])}
    denied = {d["id"]: d["reason"] for d in data.get("denied", [])}
    return allowed, denied


def assert_allowed(model_id: str) -> ModelCard:
    allowed, denied = _registry()
    if model_id in denied:
        raise ModelNotAllowed(f"{model_id}: {denied[model_id]}")
    card = allowed.get(model_id)
    if card is None:
        raise ModelNotAllowed(
            f"{model_id} не описана в config/models.yaml. "
            "ТЗ требует перечислить лицензию и размер каждой используемой модели."
        )
    if card.license not in ALLOWED_LICENSES:
        raise ModelNotAllowed(f"{model_id}: лицензия {card.license}, ТЗ требует Apache 2.0 или MIT")
    if card.params_b > MAX_PARAMS_B:
        raise ModelNotAllowed(f"{model_id}: {card.params_b}B превышает потолок 35B из ТЗ")
    return card
