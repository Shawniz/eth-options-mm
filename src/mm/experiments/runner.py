"""Phase 8 experiment runner (D042).

    uv run python -m mm.run --config config/base.yaml --experiments config/experiments.yaml

1. Train period: the baseline and every value of each `select` experiment. The best value
   of each (highest mean daily P&L on train) forms the best setting.
2. Test period: the baseline, every experiment value, the best setting, and the best
   setting under each robustness override.
3. Results table (table.py). Each run is deterministic and cached by a hash of its
   resolved config, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import yaml

from mm.config import Config, load_config


def apply_override(cfg: Config, override: dict) -> Config:
    """Deep-merge an override into a config and re-validate (typos fail loudly)."""

    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
        return out

    return Config.model_validate(merge(cfg.model_dump(mode="python"), override))


def _get(cfg: Config, path: list[str]):
    x = cfg
    for p in path:
        x = getattr(x, p)
    return x


def _nest(path: list[str], value) -> dict:
    d = value
    for p in reversed(path):
        d = {p: d}
    return d


def cfg_hash(cfg: Config, start: str, end: str) -> str:
    blob = json.dumps({"cfg": cfg.model_dump(mode="json"), "start": start, "end": end},
                      sort_keys=True, default=str)  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def run_variant(cfg: Config, start: str, end: str, out: Path) -> dict:
    """Simulate, attribute and summarise one variant over [start, end). Cached."""
    from mm.experiments.metrics import daily_components
    from mm.pnl.attribution import attribute
    from mm.pnl.markouts import markouts
    from mm.pnl.value import MarketData
    from mm.sim.engine import run
    from mm.sim.load import load_inputs
    from mm.sim.report import volume_share

    h = cfg_hash(cfg, start, end)
    sfile = out / "summary.json"
    if sfile.exists() and json.loads(sfile.read_text()).get("hash") == h:
        return json.loads(sfile.read_text())
    out.mkdir(parents=True, exist_ok=True)
    inp = load_inputs(cfg, pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"))
    res = run(inp, cfg)
    md = MarketData(inp.book, inp.curve, inp.perp)
    att = attribute(res.fills, res.hedges, res.ledger, res.daily, md, cfg.sim.initial_eth,
                    inp.start_ms, inp.end_ms)  # fmt: skip
    mk = markouts(res.fills, md, inp.end_ms, cfg.hedging.delta)
    daily = daily_components(att.rows)
    daily.to_parquet(out / "daily_pnl.parquet")
    res.fills.to_parquet(out / "fills.parquet", index=False)
    res.hedges.to_parquet(out / "hedges.parquet", index=False)
    mk.to_parquet(out / "markouts.parquet", index=False)
    snaps = att.snapshots
    q = res.quotes
    lim = cfg.risk.vega_limit_usd
    vega_mk = (mk["qty"] * mk["vega_usd"]).sum() if len(mk) else 0.0
    summ = {
        "hash": h, "start": start, "end": end,
        "total": float(snaps["engine_w"].iloc[-1] - att.w_start) if len(snaps) else 0.0,
        "residual": float((snaps["engine_w"].iloc[-1] - att.w_start) - daily["total"].sum())
        if len(snaps) else 0.0,
        "fills": len(res.fills),
        "contracts": float(res.fills["qty"].sum()) if len(res.fills) else 0.0,
        "share_of_contracts": float(volume_share(inp, res).iloc[-1]["share of contracts"]),
        "hedges": len(res.hedges),
        "mean_abs_vega_frac": float((q["net_vega_usd"].abs() / lim).mean()) if len(q) else 0.0,
        "mean_abs_gamma_frac": float((q["net_gamma1_usd"].abs() / cfg.risk.gamma_limit_usd)
                                     .mean()) if len(q) else 0.0,
        "quotes_cut_frac": float(((q["bid_size"] < q["size"]) | (q["ask_size"] < q["size"]))
                                 .mean()) if len(q) else 0.0,
        "markout_usd": {lab: float(mk[f"hedged_{lab}"].sum()) for lab in
                        ("0", "1min", "5min", "1h")} if len(mk) else {},
        "markout_vp": {lab: float(mk[f"hedged_{lab}"].sum() / vega_mk) for lab in
                       ("0", "1min", "5min", "1h")} if vega_mk else {},
    }  # fmt: skip
    sfile.write_text(json.dumps(summ, indent=2))
    return summ


def _job(base_path: str, override: dict, start: str, end: str, out: str) -> tuple[str, dict]:
    cfg = apply_override(load_config(base_path), override)
    return out, run_variant(cfg, start, end, Path(out))


def plan(base: Config, exp: dict) -> dict[str, dict]:
    """Variant name -> override, for the experiments (baseline included once)."""
    out = {"baseline": {}}
    for name, e in exp["experiments"].items():
        base_v = _get(base, e["param"])
        if base_v not in e["values"]:
            raise ValueError(f"experiment {name}: base value {base_v} not in {e['values']}")
        for v in e["values"]:
            if v != base_v:
                out[f"{name}={v:g}"] = _nest(e["param"], v)
    return out


def _run_all(base_path, jobs: dict[str, tuple[dict, str, str, Path]], workers: int) -> dict:
    res = {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_job, base_path, o, s, e, str(p)): n for n, (o, s, e, p) in jobs.items()}
        for f in as_completed(futs):
            n = futs[f]
            res[n] = f.result()[1]
            print(f"  done {n}: total ${res[n]['total']:,.0f}", flush=True)
    return res


def run_experiments(base_path: str, exp_path: str, workers: int | None = None) -> dict:
    from mm.experiments.metrics import choose_best

    base = load_config(base_path)
    exp = yaml.safe_load(Path(exp_path).read_text())
    st = exp["settings"]
    workers = workers or st["workers"]
    root = Path(st["out_dir"])
    sp = base.study_period
    train = (str(sp.start), str(sp.train_end))
    test = (str(sp.train_end), str(sp.end))
    variants = plan(base, exp)
    selectable = {n for n in variants if n == "baseline"
                  or exp["experiments"][n.split("=")[0]]["select"]}  # fmt: skip

    print(f"Train {train[0]} -> {train[1]}: {len(selectable)} runs", flush=True)
    tr = _run_all(base_path, {n: (variants[n], *train, root / "train" / n)
                              for n in sorted(selectable)}, workers)  # fmt: skip

    def mean_daily(n, period):
        return float(pd.read_parquet(root / period / n / "daily_pnl.parquet")["total"].mean())

    best_override: dict = {}
    best = {}
    for name, e in exp["experiments"].items():
        if not e["select"]:
            continue
        base_v = _get(base, e["param"])
        cands = {f"{name}={v:g}" if v != base_v else "baseline": v for v in e["values"]}
        scores = {c: mean_daily(c, "train") for c in cands}
        pick = choose_best(scores)
        best[name] = {"value": cands[pick], "train_mean_daily": scores}
        if cands[pick] != base_v:
            best_override = _deep(best_override, _nest(e["param"], cands[pick]))
    apply_override(base, best_override)  # validate the combined best setting
    print(f"Best on train: { {k: v['value'] for k, v in best.items()} }", flush=True)

    jobs = {n: (o, *test, root / "test" / n) for n, o in variants.items()}
    if best_override:
        jobs["best"] = (best_override, *test, root / "test" / "best")
    for rn, ro in exp["robustness"].items():
        jobs[f"best+{rn}"] = (_deep(best_override, ro), *test, root / "test" / f"best+{rn}")
    print(f"Test {test[0]} -> {test[1]}: {len(jobs)} runs", flush=True)
    te = _run_all(base_path, jobs, workers)
    meta = {"train": train, "test": test, "best": best, "best_override": best_override,
            "variants": {n: o for n, (o, *_rest) in jobs.items()}}  # fmt: skip
    (root / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    return {"train": tr, "test": te, "meta": meta}


def _deep(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out
