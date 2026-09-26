"""Отпечаток модельной части кэша профиля (задача V4): ключ полного профиля
несёт id моделей и хэш промптов разбора, ключ детерминированной части нет."""
from __future__ import annotations
import shutil
from pathlib import Path

from deckforge.template import profile as profile_module
from deckforge.template.profile import (
    MODEL_CACHE_AGENTS, TemplateProfile, deterministic_cache_key, model_cache_key,
)

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "dataset" / "templates" / "VK Tech шаблон.pptx"


def _agents_copy(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    for name in MODEL_CACHE_AGENTS:
        (root / name).mkdir(parents=True)
        shutil.copy(REPO / "agents" / name / "AGENT.md", root / name / "AGENT.md")
    return root


def test_prompt_edit_changes_model_key_but_not_deterministic_key(tmp_path):
    data = TEMPLATE.read_bytes()
    agents = _agents_copy(tmp_path)
    ids = ["palette_namer=m1", "pattern_kind=m1", "pattern_schema=m1"]
    before_model = model_cache_key(data, model_ids=ids, agents_dir=agents)
    before_det = deterministic_cache_key(data)

    prompt = agents / "pattern-schema" / "AGENT.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nещё одно правило\n", encoding="utf-8")

    assert model_cache_key(data, model_ids=ids, agents_dir=agents) != before_model
    assert deterministic_cache_key(data) == before_det
    assert before_model.startswith(f"{before_det.split('-v')[0]}-v{profile_module.PROFILE_SCHEMA_VERSION}-m")
    assert len(before_model.rsplit("-m", 1)[1]) == 8


def test_model_id_changes_the_model_key():
    data = b"pptx"
    a = model_cache_key(data, model_ids=["pattern_kind=qwen-a"])
    b = model_cache_key(data, model_ids=["pattern_kind=qwen-b"])
    assert a != b
    assert a == model_cache_key(data, model_ids=["pattern_kind=qwen-a"]), "ключ воспроизводим"


def test_prompt_edit_reparses_model_part_and_keeps_geometry_cache(tmp_path, monkeypatch):
    """Смена промпта: полный профиль в кэше больше не находится (новый файл
    под новым ключом), а детерминированный кэш по-прежнему отдаёт геометрию
    без перемайнинга."""
    cache_dir = tmp_path / "cache"
    agents = _agents_copy(tmp_path)
    monkeypatch.setattr(profile_module, "AGENTS_DIR", agents)
    first = TemplateProfile.from_file(TEMPLATE, cache_dir=cache_dir)
    assert (cache_dir / f"{first.fingerprint}.json").exists()

    prompt = agents / "palette-namer" / "AGENT.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nправка\n", encoding="utf-8")

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("геометрия перемайнена из-за правки промпта")

    for name in ("mine_patterns", "build_grid", "build_layout_catalog", "build_asset_catalog", "build_shape_vocabulary"):
        monkeypatch.setattr(f"deckforge.template.profile.{name}", _must_not_be_called)

    second = TemplateProfile.from_file(TEMPLATE, cache_dir=cache_dir)
    assert second.fingerprint != first.fingerprint
    assert second.patterns == first.patterns
    assert (cache_dir / f"{second.fingerprint}.json").exists()
