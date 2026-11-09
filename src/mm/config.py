"""Typed configuration loaded from config/*.yaml.

Unknown keys are rejected so a typo in a YAML file fails loudly instead of
silently falling back to a default.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

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


class ForwardsConfig(_Strict):
    future_basis_max_age_s: float = Field(gt=0)
    non_future_source: Literal["interp", "parity"]
    parity_pair_window_s: float = Field(gt=0)
    parity_atm_band: float = Field(gt=0)
    parity_n_pairs: int = Field(ge=1)
    parity_min_pairs: int = Field(ge=1)
    parity_max_age_s: float = Field(gt=0)
    basis_bounds: tuple[float, float]

    @model_validator(mode="after")
    def _check(self) -> ForwardsConfig:
        if self.parity_min_pairs > self.parity_n_pairs:
            raise ValueError("parity_min_pairs must be <= parity_n_pairs")
        lo, hi = self.basis_bounds
        if not 0 < lo < 1 < hi:
            raise ValueError("basis_bounds must satisfy 0 < lo < 1 < hi")
        return self


class ValidationConfig(_Strict):
    min_days_to_expiry: float = Field(ge=0)
    vol_target_min_days: float = Field(ge=0)
    target_median_volpts: float = Field(gt=0)
    target_p95_volpts: float = Field(gt=0)
    price_target_frac_within_half_tick: float = Field(gt=0, le=1)
    forward_gap_sample: int = Field(gt=0)
    seed: int


class PricingConfig(_Strict):
    forwards: ForwardsConfig
    validation: ValidationConfig


class SimConfig(_Strict):
    latency_ms: int = Field(ge=0)


class FillConfig(_Strict):
    at_price_fill_fraction: float = Field(ge=0.0, le=1.0)
    max_fill_fraction: float = Field(gt=0.0, le=1.0)


class SurfaceValidation(_Strict):
    target_rmse_median_volpts: float = Field(gt=0)
    target_rmse_p95_volpts: float = Field(gt=0)
    normal_day_dvol_quantile: float = Field(gt=0, lt=1)
    target_arb_free_frac: float = Field(gt=0, le=1)
    target_dvol_change_corr: float = Field(gt=-1, le=1)


class SurfaceConfig(_Strict):
    refit_interval_s: int = Field(ge=60, le=300)
    trailing_window_s: int = Field(gt=0)
    min_fit_hours: float = Field(ge=0)
    min_mark_price_eth: float = Field(ge=0)
    max_mark_iv: float = Field(gt=0)
    min_strikes_full: int = Field(ge=3)
    max_carry_s: float = Field(ge=0)
    cold_start_tries: int = Field(ge=1)
    calendar_penalty: float = Field(gt=0)
    calendar_grid_std: float = Field(gt=0)
    calendar_grid_n: int = Field(ge=11)
    k_grid: tuple[float, float, int]
    butterfly_tol: float = Field(ge=0)
    calendar_tol: float = Field(ge=0)
    stats_interval_s: int = Field(gt=0)
    const_maturity_days: float = Field(gt=0)
    n_workers: int = Field(ge=1)
    chunk_days: float = Field(gt=0)
    validation: SurfaceValidation


class Config(_Strict):
    study_period: StudyPeriod
    data: DataConfig
    specs: Specs
    pricing: PricingConfig
    sim: SimConfig
    fill: FillConfig
    surface: SurfaceConfig


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
