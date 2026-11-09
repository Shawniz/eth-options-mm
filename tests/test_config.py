from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mm.config import load_config
from mm.run import parse_args

BASE = Path(__file__).resolve().parents[1] / "config" / "base.yaml"


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _base() -> dict:
    return yaml.safe_load(BASE.read_text())


def test_base_config_loads():
    cfg = load_config(BASE)
    assert cfg.fill.max_fill_fraction == 0.5
    assert cfg.data.exclude_block_trades is True


def test_unknown_key_rejected(tmp_path):
    d = _base()
    d["fill"]["max_fil_fraction"] = 0.5  # typo
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, d))


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_fill_fraction_bounds(tmp_path, value):
    d = _base()
    d["fill"]["at_price_fill_fraction"] = value
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, d))


def test_refit_interval_bounds(tmp_path):
    d = _base()
    d["surface"]["refit_interval_s"] = 30
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, d))


def test_cli_parses_utc_dates():
    args = parse_args(["--config", str(BASE), "--start", "2025-01-01", "--end", "2025-01-02"])
    assert args.start.tzinfo is not None
    assert args.start.utcoffset().total_seconds() == 0


def test_cli_rejects_reversed_dates():
    with pytest.raises(SystemExit):
        parse_args(["--config", str(BASE), "--start", "2025-01-02", "--end", "2025-01-01"])


def test_train_end_is_65_percent_of_days():
    cfg = load_config(BASE)
    sp = cfg.study_period
    frac = (sp.train_end - sp.start).days / (sp.end - sp.start).days
    assert frac == pytest.approx(sp.train_frac, abs=0.01)
