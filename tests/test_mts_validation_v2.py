"""2026-10-16 Hermes Agent: v2 chronology, symmetry, lifecycle priority and inventory costs."""
from dataclasses import replace
from datetime import datetime, timedelta
import json
import math

import pytest
import yaml

from scripts.research.adaptive_validation_v2 import (
    CausalVolume, Inventory, ValidationConfig, confirmation, execute_pending,
    lifecycle_decision, run_v2,
)
from scripts.research.compare_ab_results import main as compare_main
from scripts.research.run_adaptive_ab_backtest import sha256
from scripts.research.run_adaptive_ab_backtest import main as replay_main
from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAP, AdaptiveVWAPConfig, TW


def frame(ts, near=100, far=100):
    return {"ts":ts, **{leg:{"bar":{"ts":ts,"open":price,"close":price,"high":price+1,
            "low":price-10,"volume":100},"vwap":price-100,"adaptive_price":price-50,"alpha":.6}
            for leg,price in (("near",near),("far",far))}}


def test_ema_has_memory_and_rejects_future_or_gaps():
    cfg=AdaptiveVWAPConfig(reference_method="ema",window_secs=1500,ema_span=5)
    bars=[dict(ts=300*i,close=100+i) for i in range(1,6)]
    result=AdaptiveVWAP.compute(1500,1.5,.65,80,0,100,bars,config=cfg)
    expected=101
    for close in (102,103,104,105):
        expected=close/3+expected*2/3
    assert result.twap == pytest.approx(expected)
    assert result.twap != 105
    with_future=bars+[dict(ts=1800,close=9999)]
    assert AdaptiveVWAP.compute(1500,1.5,.65,80,0,100,with_future,config=cfg)==result
    assert AdaptiveVWAP.compute(1500,1.5,.65,80,0,100,bars[1:],config=cfg) is None
    assert AdaptiveVWAP.compute(1800,1.5,.65,80,0,100,bars,config=cfg) is None


@pytest.mark.parametrize("kwargs", [{"reference_method":"bad"},{"ema_span":True},{"ema_span":1},
    {"reference_method":"ema","window_secs":60},{"reference_method":"ema","window_secs":300}])
def test_reference_config_rejects_degenerate_ema(kwargs):
    with pytest.raises(ValueError):
        AdaptiveVWAPConfig(**kwargs)


def test_v2_cli_rejects_legacy_point_capital_instead_of_ignoring_it(tmp_path):
    """2026-10-16 Hermes Agent: v2 money is TWD, not a reinterpretation of v1 points."""
    with pytest.raises(ValueError,match="TWD capital"):
        replay_main(["--near","unused.csv","--far","unused.csv","--start-date","2026-07-06",
            "--end-date","2026-07-10","--output-dir",str(tmp_path/"unused"),"--initial-equity-points","40000"])


def test_volume_uses_prior_bars_and_separates_legs():
    cfg=ValidationConfig(volume_minimum_history=2)
    model=CausalVolume(cfg)
    ts=datetime(2026,7,6,10,tzinfo=TW).timestamp()
    assert model.observe("near",ts,100)["zscore"] is None
    assert model.observe("near",ts+300,200)["zscore"] is None
    r=model.observe("near",ts+600,10000)
    assert r["mean"]==150
    assert r["std"]==pytest.approx(math.sqrt(5000))
    assert r["zscore"]==pytest.approx(9850/math.sqrt(5000))
    assert r["max_calibration_ts"]==ts+300
    assert model.observe("far",ts+600,10000)["history_count"]==0
    assert model.observe("near",ts+4*3600,10000)["history_count"]==0


def test_symmetric_distance_and_adaptive_strength_distinguish_decisions():
    frames=[frame(300*i,near=1000+10*i,far=1000+10*i) for i in range(12)]
    cfg=ValidationConfig()
    assert confirmation(frames,"baseline",cfg)["pass_release"]
    assert confirmation(frames,"adaptive",cfg)["pass_release"]
    frames[-1]["near"]["alpha"]=.2
    assert confirmation(frames,"baseline",cfg)["pass_release"]
    assert confirmation(frames,"adaptive",cfg)["reason"]=="ALPHA_BELOW_THRESHOLD"
    frames[-1]["near"]["vwap"]=frames[-1]["near"]["bar"]["close"]-.1
    assert confirmation(frames,"baseline",cfg)["reason"]=="PRICE_DISTANCE_BELOW_THRESHOLD"


def test_confirmation_rejects_missing_history_and_shift():
    frames=[frame(300*i,near=1000+10*i,far=1000+10*i) for i in range(12)]
    frames[-1]["near"]["adaptive_price"]=frames[-1]["near"]["vwap"]
    assert confirmation(frames,"adaptive",ValidationConfig())["reason"]=="REFERENCE_SHIFT_BELOW_THRESHOLD"
    assert confirmation(frames[-1:],"baseline",ValidationConfig())["reason"]=="CONFIRMATION_GAP"


def test_spread_uses_canonical_entry_side_mapping():
    """2026-10-16 Hermes Agent: bearish bars cannot invert the configured retained leg."""
    frames=[frame(300*i,near=1000-10*i,far=1000-10*i) for i in range(12)]
    for f in frames:
        for leg in ("near","far"):
            f[leg]["vwap"]=f[leg]["bar"]["close"]+100
            f[leg]["adaptive_price"]=f[leg]["bar"]["close"]+50
    assert confirmation(frames,"baseline",ValidationConfig())["reason"]=="ENTRY_SIDE_DIRECTION_MISMATCH"


PRODUCT=dict(point_value=10,margin_per_lot=46000,tick_size=1,broker_fee_per_side=22,exchange_fee_per_side=0,tax_rate=.00002)
PARAMS=dict(release_stop_points=88,trail_distance_points=66,enable_combined_upl_trail=True,
            combined_upl_activation_net_pnl_twd=200,combined_upl_giveback_twd=50)


def entered():
    state=Inventory(100000)
    state.pending=dict(action="ENTRY",decision_ts=0,episode_id="E1",brick_size=10)
    execute_pending(state,frame(300),PRODUCT,1)
    return state


def test_two_leg_fills_fees_tax_slippage_and_close_once():
    state=entered()
    assert state.episode["open_legs"]==dict(near=101,far=99)
    state.pending=dict(action="COMBINED_EXIT",decision_ts=300,leg=None)
    execute_pending(state,frame(600,near=105,far=100),PRODUCT,1)
    ep=state.episodes[0]
    assert ep["gross_twd"]==10
    assert ep["cost_twd"]==pytest.approx(88+.0002*(101+99+104+101))
    assert state.cash==pytest.approx(100000+ep["net_pnl_twd"])
    assert state.episode is None
    assert execute_pending(state,frame(900),PRODUCT,1)==[]


def test_gap_does_not_fill_old_signal_or_erase_inventory():
    state=entered()
    state.pending=dict(action="COMBINED_EXIT",decision_ts=300,leg=None)
    events=execute_pending(state,frame(900),PRODUCT,1)
    assert events[0]["event"]=="UNFILLED_GAP"
    assert state.episode["open_legs"]==dict(near=101,far=99)
    assert not state.episodes


def test_policy_j_and_hard_stop_preempt_trend_gates():
    state=entered()
    state.peak_net=1000
    blocked=dict(pass_release=False,release_leg=None,direction="CHOP")
    action=lifecycle_decision(state,frame(600,near=140,far=110),blocked,ValidationConfig(),PARAMS)
    assert action["action"]=="COMBINED_EXIT"
    action=lifecycle_decision(state,frame(600,near=100,far=400),blocked,ValidationConfig(),PARAMS)
    assert action["action"]=="STOPLOSS"


def test_trend_release_then_single_leg_trail():
    state=entered()
    trend=dict(pass_release=True,release_leg="FAR",direction="BULLISH")
    action=lifecycle_decision(state,frame(600,near=120,far=120),trend,ValidationConfig(),PARAMS)
    assert action["action"]=="TREND_RELEASE" and action["leg"]=="FAR"
    state.pending=action
    execute_pending(state,frame(900,near=120,far=120),PRODUCT,1)
    assert list(state.episode["open_legs"])==["near"]
    action=lifecycle_decision(state,frame(1200,near=50,far=120),
        dict(pass_release=False,release_leg=None,direction="CHOP"),ValidationConfig(),PARAMS)
    assert action["action"]=="TRAIL"


def write_snapshot(path, future_shift=0):
    start=datetime(2026,7,6,9,0)
    lines=["ts,Open,High,Low,Close,Volume,Amount"]
    for minute in range(150):
        price=1000+minute+(future_shift if minute>=120 else 0)
        volume=20+minute%7
        lines.append(f"{start+timedelta(minutes=minute)},{price},{price+2},{price-3},{price+1},{volume},{price*volume}")
    path.write_text("\n".join(lines)+"\n")
    return path


def synthetic_run(tmp_path, future_shift=0):
    tmp_path.mkdir(exist_ok=True)
    near=write_snapshot(tmp_path/"near.csv",future_shift)
    far=write_snapshot(tmp_path/"far.csv",future_shift)
    config=tmp_path/"validation.yaml"
    config.write_text(yaml.safe_dump(replace(ValidationConfig(),volume_minimum_history=2).__dict__))
    out=tmp_path/"out"
    run_v2(near,far,start_date="2026-07-06",end_date="2026-07-06",output_dir=out,validation_path=config)
    return out


def test_v2_end_to_end_report_and_source_integrity(tmp_path):
    out=synthetic_run(tmp_path)
    args=["--baseline",str(out/"baseline_*.jsonl"),"--adaptive",str(out/"adaptive_*.jsonl"),
          "--output",str(tmp_path/"v2.md")]
    compare_main(args)
    result=json.loads((tmp_path/"v2.json").read_text())
    assert result["paired_decisions"]==30
    assert result["promotion"]=="NO_GO"
    assert (tmp_path/"volume_calibration_diagnosis.md").exists()
    source=tmp_path/"near.csv"
    source.write_text(source.read_text()+"\n")
    with pytest.raises(ValueError,match="source checksum"):
        compare_main(args)


def test_future_mutation_cannot_change_v2_prior_decisions(tmp_path):
    a=synthetic_run(tmp_path/"a")
    b=synthetic_run(tmp_path/"b",future_shift=10000)
    first=[json.loads(s) for s in next(a.glob("adaptive_*.jsonl")).read_text().splitlines()]
    second=[json.loads(s) for s in next(b.glob("adaptive_*.jsonl")).read_text().splitlines()]
    cutoff=datetime(2026,7,6,11,tzinfo=TW).timestamp()
    assert [r for r in first if r["ts"]<=cutoff]==[r for r in second if r["ts"]<=cutoff]


@pytest.mark.parametrize("mutation", ["missing_pair", "duplicate_pair", "future_calibration"])
def test_v2_rejects_inconsistent_pairs_even_with_updated_output_hash(tmp_path, mutation):
    """2026-10-16 Hermes Agent: integrity checks must go beyond checksum equality."""
    out=synthetic_run(tmp_path)
    path=next(out.glob("adaptive_*.jsonl"))
    rows=[json.loads(s) for s in path.read_text().splitlines()]
    if mutation=="missing_pair":
        rows.pop()
    elif mutation=="duplicate_pair":
        rows.append(rows[0])
    else:
        rows[0]["features"]["near"]["volume_model"]["max_calibration_ts"]=rows[0]["ts"]
        baseline=next(out.glob("baseline_*.jsonl"))
        baseline_rows=[json.loads(s) for s in baseline.read_text().splitlines()]
        baseline_rows[0]["features"]=rows[0]["features"]
        baseline.write_text("".join(json.dumps(r)+"\n" for r in baseline_rows))
    path.write_text("".join(json.dumps(r)+"\n" for r in rows))
    manifest_path=out/"model_manifest.json"
    manifest=json.loads(manifest_path.read_text())
    for name in manifest["outputs"]:
        manifest["outputs"][name]=sha256(out/name)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="unpaired|duplicate|lookahead"):
        compare_main(["--baseline",str(out/"baseline_*.jsonl"),"--adaptive",str(out/"adaptive_*.jsonl"),
                      "--output",str(tmp_path/"report.md")])
