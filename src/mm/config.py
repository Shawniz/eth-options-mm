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


class SuspectConfig(_Strict):
    iv_max: float = Field(gt=0)
    mark_rel_dev: float = Field(gt=0)
    mark_min_ticks: float = Field(ge=0)


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
    suspect: SuspectConfig


class TickStep(_Strict):
    above_price_eth: float = Field(ge=0)
    tick_eth: float = Field(gt=0)


class OptionSpecs(_Strict):
    contract_size_eth: float = Field(gt=0)
    min_order_contracts: float = Field(gt=0)
    tick_steps: list[TickStep] = Field(min_length=1)
    maker_fee_eth_per_contract: float = Field(ge=0)
    taker_fee_eth_per_contract: float = Field(ge=0)
    fee_cap_fraction_of_premium: float = Field(gt=0, le=1)
    delivery_fee_eth_per_contract: float = Field(ge=0)
    delivery_fee_cap_fraction: float = Field(gt=0, le=1)
    delivery_fee_exempt_daily: bool

    @model_validator(mode="after")
    def _steps_sorted(self) -> OptionSpecs:
        levels = [s.above_price_eth for s in self.tick_steps]
        if levels[0] != 0.0 or levels != sorted(set(levels)):
            raise ValueError("tick_steps must start at 0 and be strictly increasing")
        return self

    def tick_for(self, price_eth: float) -> float:
        tick = self.tick_steps[0].tick_eth
        for s in self.tick_steps:
            if price_eth > s.above_price_eth:
                tick = s.tick_eth
        return tick


class PerpSpecs(_Strict):
    instrument: str
    contract_size_usd: float = Field(gt=0)
    min_order_usd: float = Field(gt=0)
    tick_usd: float = Field(gt=0)
    maker_fee_rate: float
    taker_fee_rate: float = Field(ge=0)


class Specs(_Strict):
    option: OptionSpecs
    perp: PerpSpecs


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
    specs: Specs
    sim: SimConfig
    fill: FillConfig
    surface: SurfaceConfig


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
