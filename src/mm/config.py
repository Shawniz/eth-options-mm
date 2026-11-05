"""Typed configuration loaded from config/*.yaml.

Unknown keys are rejected so a typo in a YAML file fails loudly instead of
silently falling back to a default.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataConfig(_Strict):
    raw_dir: Path
    clean_dir: Path
    currency: str = "ETH"
    history_base_url: str
    main_base_url: str
    exclude_block_trades: bool = True


class SimConfig(_Strict):
    latency_ms: int = Field(ge=0)


class FillConfig(_Strict):
    at_price_fill_fraction: float = Field(ge=0.0, le=1.0)
    max_fill_fraction: float = Field(gt=0.0, le=1.0)


class SurfaceConfig(_Strict):
    refit_interval_s: int = Field(ge=60, le=300)
    trailing_window_s: int = Field(gt=0)


class Config(_Strict):
    data: DataConfig
    sim: SimConfig
    fill: FillConfig
    surface: SurfaceConfig


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
