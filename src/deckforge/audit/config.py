"""Пороги детерминированного аудита — `config/audit.yaml`, не код.

Тот же принцип, что `deckforge.settings.Settings` для `config/app.yaml`:
единственная точка настройки, pydantic-модель проверяет форму файла на
загрузке, а не в момент первого обращения к полю посреди проверки."""
from __future__ import annotations
from pathlib import Path

import yaml
from pydantic import BaseModel

AUDIT_YAML_PATH = Path(__file__).resolve().parents[3] / "config" / "audit.yaml"


class LayoutThresholds(BaseModel):
    overlap_ratio: float
    overlap_min_area_in2: float
    margin_tolerance: float
    grid_tolerance: float
    aspect_tolerance: float
    text_fit_tolerance_in: float
    grid_axis_min_support_share: float


class TemplateThresholds(BaseModel):
    min_contrast_small: float
    min_contrast_large: float
    large_text_pt: float
    large_bold_pt: float
    max_font_families: int
    size_tolerance_pt: float
    logo_position_tolerance: float


class DensityThresholds(BaseModel):
    max_bullets: int
    max_words_per_bullet: int
    max_table_rows: int
    max_table_cols: int
    max_chart_series: int
    fill_ratio_min: float
    fill_ratio_max: float
    pattern_delta_max: float


class IntegrityThresholds(BaseModel):
    placeholder_patterns: list[str]
    duplicate_similarity: float
    single_picture_coverage: float
    min_duplicate_check_text_len: int


class RepairThresholds(BaseModel):
    overflow_ratio: float
    overlap_ratio: float
    density_delta: float


class AuditConfig(BaseModel):
    layout: LayoutThresholds
    template: TemplateThresholds
    density: DensityThresholds
    integrity: IntegrityThresholds
    repair: RepairThresholds

    @classmethod
    def load(cls, path: Path = AUDIT_YAML_PATH) -> "AuditConfig":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(**data)
