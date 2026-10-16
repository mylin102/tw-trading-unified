# Adaptive VWAP-TWAP integration

2026-10-16 Hermes Agent: Task 1.4.2 implementation, assumptions and verification evidence.

The fusion algorithm, optional candidate fields, paired collection, configuration, offline replay and net-outcome comparison are implemented. Phase 1 computes observations; baseline arbitration and order execution retain their existing authority. No production deployment or real A/B profitability improvement is claimed.

## 2026-10-16 v2 offline validation extension

Hermes Agent: `/tmp/codex-task-improve-validation.md` authorizes the multi-file research change below. The earlier sections describe the initial Phase 1 implementation and its historical verification results.

- `AdaptiveVWAPConfig()` retains the original TWAP defaults for API compatibility. The repository YAML now opts into `reference_method: ema`, `window_secs: 1500`, `ema_span: 5`, `bar_secs: 300`. No order-execution switch was added.
- EMA is a finite-window exponential smoother seeded by the oldest of five complete, contiguous 5-minute closes, with coefficient `2/(5+1)`. Fewer bars or a missing interval yields unavailable. A 60-second window on 5-minute close inputs still degenerates, so dispatch option A was rejected.
- `AdaptiveVWAPSnapshot.twap` remains the legacy serialized field name; with `reference_method: ema` it contains EMA, not a mathematical TWAP. Interpret it with the recorded config; v2 research records use the explicit `ema` key.
- `config/adaptive_validation_config.yaml` is consumed only by the research CLI. Baseline and adaptive share a 0.1% price-to-reference distance gate, two-bar confirmation and canonical Renko/ADL/reference arbitration. Adaptive additionally requires alpha≥0.3 and adaptive-to-VWAP displacement≥0.1%. These are preset model assumptions, not parameters selected from v2 PnL.
- Volume calibration uses strictly prior same-leg, same-clock-segment 5-minute volumes: minimum30, maximum120 samples. Score first, then append current volume. The whole-snapshot statistics are diagnostic only. Production `ZSCORE_PARAMS` stays unchanged because CSV volume units and contract provenance remain unverified.
- The lifecycle replay calls existing `evaluate_lifecycle_actions()` and `counter_trend_leg_from_sides()`. For the fixed LONG NEAR / SHORT FAR inventory, canonical Trend Release retains LONG NEAR and releases FAR; bearish confirmation is used separately for remaining-leg reversal, not to invert the entry-side mapping.
- Both arms share hypothetical spread entries and maintain separate non-overlapping inventories. Entry is allowed only when both arms are flat, cooldown is satisfied, and both have margin for two legs; costs use product configuration plus adverse slippage. Initial research scenario capital is100000 TWD, not an amendment of the runtime PAPER capital limit.
- All fills use the next contiguous paired bar open. Missing bars cancel the pending fill, not the position. Open inventory remains marked and visible at the dataset end; no fabricated terminal exit. Mark-to-liquidation equity includes estimated exit fees/tax/slippage and is distinct from realized episode PnL.
- Hard stop, timeout and Policy J remain above trend gates. `marks_live=False` preserves the actual adapter's closed-bar release-intercept refusal. Policy J's existing decision formula hardcodes10 TWD/point and92 TWD friction; v2 refuses other point values and separately accounts actual configured costs in the fill ledger. This legacy discrepancy is reported, not changed here.
- Tick MID, BBO freshness, tick-confirmed peaks, actual MTS entry records, OI and fraud-quality history are unavailable. The result is explicitly `OFFLINE_MTS_LIFECYCLE_BAR_PROXY_V2`, never complete production replay or a promotion candidate.

The CLI now defaults to v2; use a fresh output directory:

```bash
TASK_PYTHON=/Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3
taskpolicy -c background "$TASK_PYTHON" scripts/research/run_adaptive_ab_backtest.py \
  --model-version v2 --near data/tmf_near_20260712.csv --far data/tmf_far_20260712.csv \
  --start-date 2026-07-06 --end-date 2026-07-10 --output-dir logs/adaptive_v2_reproduction \
  --config config/adaptive_vwap_config.yaml --validation-config config/adaptive_validation_config.yaml
taskpolicy -c background "$TASK_PYTHON" scripts/research/compare_ab_results.py \
  --baseline 'logs/adaptive_v2_reproduction/baseline_*.jsonl' \
  --adaptive 'logs/adaptive_v2_reproduction/adaptive_*.jsonl' \
  --output reports/adaptive_v2_reproduction.md
```

For v1 compatibility, explicitly pass `--model-version v1` and a YAML with `reference_method: twap`, `window_secs: 300`, `bar_secs: 300`; the v1 runner refuses EMA instead of silently changing its frozen model. Original source/config is pinned at commit `3ec71658`.

Reports: [v2 comparison](../../reports/ab_comparison_20261016_v2.md), [volume diagnosis](../../reports/volume_calibration_diagnosis.md). Current authoritative artifacts are under `logs/ab_test_20261016_v2_verified/`; earlier v2 directories are intermediate evidence, not the final result.

The5% gate uses all paired records as denominator and excludes availability-only probe differences. It requires both common-reference same-state candidate differences and actual portfolio action/leg differences to reach5%, plus at least one different paired exit. No threshold or capital adjustment is permitted merely to turn this snapshot's result into a pass. Strategy promotion always remains NO_GO for this assumption-based, reused sample.

## Specification decisions

The supplied `/tmp/ADAPTIVE_VWAP_SPEC.md` contains contradictions. Clarification was requested; the following assumptions were stated before implementation and remain explicit:

| Topic | Implemented interpretation |
| --- | --- |
| OI direction | Follow the stated intent: direction 0.5 is weakest; 0 and 1 strongest. Score is `2 * abs(direction - 0.5) * 100`. The printed reference formula instead rewards 0.5 and was not used. |
| TWAP timestamps | Existing bars carry close timestamps. A close at the decision time represents a completed interval, not a zero-duration point. |
| Volume quality | Use Task 1.3.2 `vol_quality_score`, as specified in the input section, rather than Z-score `quality_rating` shown in one integration example. |
| Confidence | The spec has no formula. Use `quality_score * covered_window_fraction`, a conservative input-quality/temporal-coverage proxy, not a calibrated probability. |
| Evaluation week | The user's `/tmp/codex-ab-data-response.md` defines five trading dates. Require paired outcomes on at least five distinct observed Asia/Taipei weekday dates; callers must supply actual trading observations and official session/holiday provenance. Weekend clock dates do not satisfy the gate. |
| Promotion threshold | Use the stricter explicit criteria: win-rate gain at least **2 percentage points**, and relative MDD reduction at least **5%**. The later +1% note is not the promotion gate. |

The alpha mapping follows the reference sigmoid exactly: `sigmoid((chips - 50) / 15)`. With defaults its endpoints are approximately 0.03445 and 0.96555, rather than exactly zero and one. The reference anomaly formula, including its discontinuities at -1 and 2, is retained and tested.

## Components

| File | Responsibility |
| --- | --- |
| `strategies/plugins/futures/active/mts_adaptive_vwap.py` | Pure validated configuration, chips score, stable sigmoid, TWAP, interpolation and snapshot serialization |
| `strategies/plugins/futures/active/mts_hvwap_candidate.py` | Four optional fields and opt-in adaptive computation from matching completed-bar signals |
| `strategies/plugins/futures/active/mts_adaptive_ab.py` | Two-arm evaluation, paired JSONL collector, net-outcome metrics and comparison CLI |
| `strategies/plugins/futures/active/tmf_spread.py` | Load configuration once, preserve baseline verdict, record both arms at existing completed-bar emission cadence |
| `scripts/research/replay_adaptive_vwap.py` | Read overlapping historical CSVs, construct complete bars, replay both observations and write reports |
| `config/adaptive_vwap_config.yaml` | Calibration, weights, shadow flag and comparison thresholds |

`adaptive_price`, `chips_strength`, `alpha` and `adaptive_twap` are all `Optional[float] = None`. Baseline calls remain `use_adaptive=False`. An explicit `use_adaptive=True` populates observations when OI, Task 1.3.2 quality, calibrated volume Z-score and NEAR session VWAP are available at the same completed-bar boundary. Missing inputs or uncalibrated volume segments leave these fields null. Adaptive computation does not replace VWAP inputs to the release voters in Phase 1.

```python
from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAP

snapshot = AdaptiveVWAP.compute(
    ts=1792029600.0,
    oi_intensity=1.5,
    oi_direction=0.65,
    vol_quality=75.0,
    vol_zscore=0.8,
    vwap=45920.0,
    bars=[{"ts": 1792029600.0, "close": 45905.0}],
)
result = snapshot.to_dict() if snapshot is not None else None
```

This example uses synthetic inputs to illustrate the API; it is not a market-performance result.

## TWAP contract

Bars are sorted without mutating caller data. Duplicate timestamps use the last valid sample. Only close timestamps in `(decision_ts - window_secs, decision_ts]` are eligible; future bars never contribute.

Each bar covers `(start_ts, close_ts]` when `start_ts` is provided, otherwise `(close_ts - duration_secs, close_ts]`, with `bar_secs=300` as the default duration. Intervals are clipped to the requested window and previous covered interval, so overlaps are not counted twice and gaps are not filled. Prices must be finite and positive. An unavailable price or zero covered duration returns `None`; no zero-price fallback is invented. Volume does not enter TWAP.

Naive datetimes/ISO strings mean Asia/Taipei, independently of the host timezone. Numeric epochs in seconds/milliseconds and stringified numeric candidate timestamps are supported.

## Configuration and collection

The strategy loads `config/adaptive_vwap_config.yaml` once during `init()`. A strategy context can override `adaptive_vwap_config_path`. Invalid or absent configuration disables the shadow collector with a warning; it does not prevent baseline initialization. Restart/deployment authorization rules still apply; the YAML does not trigger a restart or enable a trading route.

The default `shadow_enabled: true` enables paired observation when this code is eventually approved and deployed. Setting it to false suppresses the additional shadow arm. There is no switch that enables adaptive trading.

One `HVWAP_CANDIDATE` event remains the baseline record. At the same existing five-minute cadence, `HVWAP_ADAPTIVE_AB` contains both complete verdicts with a pair ID, configuration, data-valid flag, and explicit `SHADOW_ONLY` execution mode. The baseline verdict stored in `_hvwap_last_verdict` is not replaced by the adaptive result. Both arms share the immutable baseline arbitration calculation; only the adaptive observation is added, avoiding duplicate voter/scoring work. An independently requested `use_adaptive=True` evaluation produces the same enriched verdict.

The paired collector additionally writes:

```text
<directory containing the mode-scoped _MTS_EVENT_LOG>/ab_test/paired_YYYYMMDD.jsonl
```

Both arms are in one locked JSONL record, rather than two independently writable files that can drift apart. Directories are created automatically. This follows the supplement's suggested `logs/ab_test/` location while retaining the runtime's PAPER/LIVE path isolation.

Each arm records candidate release eligibility and a hypothetical price mark, plus `exit_executed: false` and `exit_price: null`. Phase 1 has no adaptive execution/fill model; actual exits and net outcomes must be joined from an authorized execution/replay model later. Candidate eligibility must not be presented as an executed exit. Collector failures log a warning and preserve the already-computed baseline.

## Offline replay

The supplied historical files cover June/July, not the supplement's suggested October 7–11 window. File names can differ from contained dates and overlap each other. Replay filters **content timestamps**, merges duplicates, explicitly shifts source start timestamps when requested, and discards incomplete five-minute buckets. FAR data remains its own leg's data.

The tool requires explicit source timestamp semantics. For the existing minute-bar CSVs, the engineering smoke run used `--timestamp-kind start --source-bar-secs 60`. This contract must be verified for any other dataset.

Run from the repository root, with an empty output directory:

```bash
TASK_PYTHON=/Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3
taskpolicy -c background "$TASK_PYTHON" scripts/research/replay_adaptive_vwap.py \
  --near-glob 'data/tmf_near_202606*.csv' \
  --far-glob 'data/tmf_far_202606*.csv' \
  --start 2026-06-08 --end 2026-06-12 \
  --timestamp-kind start --source-bar-secs 60 \
  --initial-equity 40000 \
  --output-dir /tmp/adaptive-replay-new-run
```

The background policy applies to child processes too. The script also includes the project's macOS entry-point policy; a sandbox may warn about that redundant priority change, while the outer background launch remains in effect.

Replay uses a documented hypothetical LONG NEAR / SHORT FAR orientation and SMA of up to 14 completed 15-minute true ranges for ATR. It does not reproduce an entire production trading lifecycle or invent trade profits. These local legacy CSVs are engineering inputs, not a verified Mini READY dataset or production-promotion evidence.

Optional `--events <JSONL>` supplies real OI/quality events, using the existing estimator unchanged. Optional `--outcomes <JSONL>` supplies paired net outcomes. Every outcome ID and timestamp must match an observed replay pair. Without these inputs the tool still records both arms and writes a report, but lists missing signals/outcomes and returns NO_GO.

Replay outputs:

```text
<output-dir>/logs/ab_test/paired_YYYYMMDD.jsonl
<output-dir>/reports/ab_comparison_<start>_<end>.json
<output-dir>/reports/ab_comparison_<start>_<end>.md
```

The real historical smoke run produced **251 paired observations across all five requested dates**. No OI/quality event dataset or paired net outcomes was available, so **0 adaptive observations had complete inputs**; metrics are null and the result is `INSUFFICIENT_DATA / NO_GO`.

Artifacts are under `/tmp/mts-adaptive-replay-20260608-12/`, with command output in `/tmp/mts-adaptive-replay.log`. Synthetic event/outcome fixtures separately test valid joins, five-date threshold decisions, and refusal to promote evaluated pairs whose adaptive inputs are unavailable. Their favorable results are test cases, not observed market improvement.

## Win rate, drawdown and Sharpe comparison

The comparison CLI accepts explicit, cost-inclusive net PnL. It never turns a VWAP/TWAP price difference into a trading profit:

```json
{"pair_id":"copy-the-observed-pair-id","ts":1792029600.0,"baseline":{"net_pnl":-120.0},"adaptive":{"net_pnl":-80.0}}
```

```bash
taskpolicy -c background "$TASK_PYTHON" \
  -m strategies.plugins.futures.active.mts_adaptive_ab \
  --input /path/to/paired_net_outcomes.jsonl \
  --initial-equity 40000 \
  --config config/adaptive_vwap_config.yaml \
  --output /tmp/adaptive-comparison.json
```

Win rate is positive-net-PnL trades divided by all paired trades, including zero-PnL trades in the denominator. MDD is the negative percentage drawdown of chronological closed-trade equity, including the initial equity peak; simultaneous closes share one equity observation. Daily Sharpe uses observed Asia/Taipei daily net returns, sample standard deviation, and `sqrt(252)` annualization. It excludes unrealized intraday risk and unobserved dates; it is not production marked-to-market MDD. Insufficient/zero-variance returns yield null Sharpe, and a zero baseline drawdown yields null relative MDD improvement.

The gate requires five observed weekday outcome dates, at least +2 percentage points of win-rate gain, and at least 5% relative reduction in absolute MDD. It does not infer an exchange holiday calendar or remap weekend night-session observations into a new trading date; those require authoritative dataset provenance. A passing numerical result is `GO_CANDIDATE`, never an automatic deployment authorization. Replay also refuses GO if evaluated outcome pairs lack valid adaptive observations. Off-segment observations remain visible as gaps without invalidating unrelated valid paired outcomes.

## Verification and limitations

- Baseline before this change: 56 relevant integration/release tests passed.
- Current focused validation: **144 passed**; `/tmp/mts-adaptive-validation.log`.
- Core/fusion and A/B modules: approximately **99%** combined line/branch coverage; exact JSON is `/tmp/mts-adaptive-coverage.json`.
- Pure `AdaptiveVWAP.compute`, 1,000 decisions with 320 bars each: mean **0.412ms**, p99 **0.891ms**, max **2.727ms** under the background launch policy; `/tmp/mts-adaptive-performance.json`.
- Complete paired candidate calculation with supplied OI snapshot/events, 100 decisions and 320 bars per leg: mean **4.607ms**, p99 **6.298ms**, max **6.408ms**; `/tmp/mts-adaptive-paired-performance.json`. Both hot-path measurements meet 10ms. Sharing immutable arbitration removed a duplicated calculation whose pre-optimization max was 12.035ms.
- This performance claim excludes upstream OI file scanning, first import, disk collection and scheduling. The prior estimator's roughly 110-second 100k-event benchmark failure remains unresolved; end-to-end runtime latency is not certified below 10ms.
- The broad `tests/ --maxfail=1` attempt stopped on the previously reproduced `test_unified_config_session_override_merging` failure: 30 passed, 1 failed. The entire suite was not completed or declared green.
- Prior OI/Vol/Quality supplied modules and their known test/schema limitations remain unchanged. The local event schema does not universally contain required order IDs/sides. New inputs are not fabricated to hide those gaps.
- Real paired outcomes, verified dataset provenance, a defined adaptive execution model, and the existing production promotion gates are still required before asserting strategy improvement or moving to Phase 2.

Host provenance: `air4`, `offline_research`, instance `mts-oi-vol-integration-20261015`, hostname `MacBook-Air-2.local`, repository `/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015`, branch `feat/mts-oi-vol-integration-20261015`, baseline `1b9e904fa478977f305c2021f141f683061e234b`. Permitted mode: PAPER; no broker access, PM2 operations, cron changes, Mini synchronization, push or deployment performed.
