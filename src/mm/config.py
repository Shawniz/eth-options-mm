"""Typed configuration loaded from config/*.yaml.

Unknown keys are rejected so a typo in a YAML file fails loudly instead of
silently falling back to a default.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StudyPeriod(_Strict):
    start: dt.date  # inclusive
    end: dt.date  # exclusive

    @model_validator(mode="after")
    def _ordered(self) -> StudyPeriod:
        if self.end <= self.start:
            raise ValueError("study_period.end must be after start")
        return self


class HttpConfig(_Strict):
    max_requests_per_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    backoff_base_s: float = Field(ge=0)
    backoff_max_s: float = Field(ge=0)
    timeout_s: float = Field(gt=0)


class DataConfig(_Strict):
    raw_dir: Path
    clean_dir: Path
    currency: str = "ETH"
    history_base_url: str
    main_base_url: str
    page_size: int = Field(gt=0, le=1000)
    dvol_resolution_s: int = Field(gt=0)
    exclude_block_trades: bool = True
    exclude_combo_trades: bool = True
    http: HttpConfig


class SimConfig(_Strict):
    latency_ms: int = Field(ge=0)


class FillConfig(_Strict):
    at_price_fill_fraction: float = Field(ge=0.0, le=1.0)
    max_fill_fraction: float = Field(gt=0.0, le=1.0)


class SurfaceConfig(_Strict):
    refit_interval_s: int = Field(ge=60, le=300)
    trailing_window_s: int = Field(gt=0)


class Config(_Strict):
    study_period: StudyPeriod
    data: DataConfig
    sim: SimConfig
    fill: FillConfig
    surface: SurfaceConfig


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
