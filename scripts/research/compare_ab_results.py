"""2026-10-16 Hermes Agent: exact-pair comparison of a declared offline proxy."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import glob
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.deployment_role_gate import assert_research_allowed
from scripts.research.run_adaptive_ab_backtest import MODEL, sha256
from strategies.plugins.futures.active.mts_adaptive_ab import compare_outcomes
from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAPConfig, TW


def load_arm(paths, arm):
    rows = {}
    for path in paths:
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            key = row["pair_id"]
            if key in rows or row["arm"] != arm or row["model"] != MODEL["name"]:
                raise ValueError("duplicate pair or incorrect arm/model")
            if row["real_order_emitted"] or row["assumed_cost_points"] != 0:
                raise ValueError("proxy contract mismatch")
            pnl = row["gross_pnl_points"]
            if row["outcome_evaluable"] != (pnl is not None):
                raise ValueError("missing outcome must remain null")
            if pnl is not None and (isinstance(pnl, bool) or not math.isfinite(pnl)):
                raise ValueError("non-finite outcome")
            rows[key] = row
    if not rows:
        raise ValueError("empty arm")
    return rows


def compare(baseline, adaptive, *, initial_equity, config=None):
    if baseline.keys() != adaptive.keys():
        raise ValueError("unpaired decision records")
    pairs, reasons = [], Counter()
    for key, a in baseline.items():
        b = adaptive[key]
        for field in ("ts", "near_close", "far_close", "vwap", "signals", "outcome_evaluable", "exit_ts"):
            if a[field] != b[field]:
                raise ValueError(f"paired input mismatch: {field}")
        reasons[b["unavailable_reason"]] += 1
        if a["outcome_evaluable"]:
            if a["exit_ts"] != a["ts"] + 300:
                raise ValueError("outcome must close on next contiguous bar")
            pairs.append({"pair_id": key, "ts": a["exit_ts"],
                          "baseline": {"net_pnl": a["gross_pnl_points"]},
                          "adaptive": {"net_pnl": b["gross_pnl_points"]}})
    # 2026-10-16 Hermes Agent: reuse equity math with explicit zero modeled costs.
    result = compare_outcomes(pairs, initial_equity=initial_equity, config=config)
    for arm, data in (("baseline", baseline), ("adaptive", adaptive)):
        rows = list(data.values())
        trades = [r["gross_pnl_points"] for r in rows if r["simulated_trade"]]
        metrics = result[arm] or {}
        # 2026-10-16 Hermes Agent: expose proxy gross PnL and outcome count honestly.
        metrics["gross_pnl_points"] = metrics.pop("net_pnl", None)
        metrics["evaluable_outcomes"] = metrics.pop("trade_count", 0)
        metrics.update({"simulated_trades": len(trades),
            "trade_win_rate_pct": 100 * sum(p > 0 for p in trades) / len(trades) if trades else None,
            "mean_trade_pnl_points": statistics.mean(trades) if trades else None,
            "worst_trade_points": min(trades) if trades else None,
            "worst_trade_pct_initial_equity": min(trades) / initial_equity * 100 if trades else None,
            "confirmation_rate_pct": 100 * sum(r["confirmed"] for r in rows) / len(rows)})
        result[arm] = metrics
    result.update({"promotion": "NO_GO", "study_kind": "ASSUMPTION_PROXY_NOT_MTS_BACKTEST",
        "decision_pairs": len(baseline), "unavailable_reasons": dict(reasons),
        "baseline_wins": sum(p["baseline"]["net_pnl"] > p["adaptive"]["net_pnl"] for p in pairs),
        "adaptive_wins": sum(p["adaptive"]["net_pnl"] > p["baseline"]["net_pnl"] for p in pairs),
        "ties": sum(p["adaptive"]["net_pnl"] == p["baseline"]["net_pnl"] for p in pairs),
        "records_by_date": dict(sorted(Counter(datetime.fromtimestamp(r["ts"], TW).date().isoformat()
                                      for r in baseline.values()).items())),
        "changed_reference_count": sum(a["reference_price"] != adaptive[k]["reference_price"]
            for k, a in baseline.items() if adaptive[k]["confirmed"]),
        "changed_direction_count": sum(a["signal_direction"] != adaptive[k]["signal_direction"]
            for k, a in baseline.items() if adaptive[k]["confirmed"]),
        "initial_equity_points": initial_equity, "model": MODEL})
    calibrated = [r for r in adaptive.values() if r["signals"]["vol_zscore"] is not None]
    result["volume_calibrated_count"] = len(calibrated)
    result["volume_unknown_count"] = len(adaptive) - len(calibrated)
    result["volume_anomaly_rate_pct"] = (100 * sum(r["signals"]["vol_anomaly_level"] in ("HIGH", "EXTREME")
        for r in calibrated) / len(calibrated) if calibrated else None)
    result["oi_intensity_mean_assumed"] = statistics.mean(r["signals"]["oi_intensity"] for r in adaptive.values())
    return result


def render(result, manifest):
    def fmt(value):
        return "N/A" if value is None else f"{value:.4f}"
    lines = ["# A/B 離線代理回放比較 — 2026-10-16", "",
        "結論：**NO_GO，僅完成核准代理模型的回放驗證，不構成 MTS 實際策略績效或部署依據。**", "",
        f"固定 20260712 near/far 快照；2026-07-06～2026-07-10，共 {result['decision_pairs']} 組決策，"
        f"共同可評估結果 {result['paired_count']} 組。所有缺漏結果保留 null。", "",
        "## 指標", "", "| 指標 | Baseline | Adaptive | Adaptive − Baseline |",
        "|---|---:|---:|---:|"]
    for label, key in (("模擬交易數", "simulated_trades"), ("交易勝率 %", "trade_win_rate_pct"),
            ("共同結果勝率 %（含 flat）", "win_rate_pct"), ("平均交易毛利 點", "mean_trade_pnl_points"),
            ("最差單筆 點", "worst_trade_points"), ("最差單筆 / 初始模型權益 %", "worst_trade_pct_initial_equity"),
            ("累計毛利 點（模型成本為零）", "gross_pnl_points"), ("已平倉權益 MDD %", "max_drawdown_pct"),
            ("日報酬年化 Sharpe（252）", "sharpe"), ("參考線可用率 %（確認率）", "confirmation_rate_pct")):
        a, b = result["baseline"].get(key), result["adaptive"].get(key)
        lines.append(f"| {label} | {fmt(a)} | {fmt(b)} | {fmt(b-a) if a is not None and b is not None else 'N/A'} |")
    for label, key in (("Vol HIGH/EXTREME 偵測率 %（已校準時段）", "volume_anomaly_rate_pct"),
                       ("OI 強度均值（假設常數）", "oi_intensity_mean_assumed")):
        lines.append(f"| {label} | {fmt(result[key])} | {fmt(result[key])} | 0 |")
    lines += ["", "## 成對分析與覆蓋", "",
        f"Baseline 勝 {result['baseline_wins']}；Adaptive 勝 {result['adaptive_wins']}；平手 {result['ties']}。",
        f"有效參考線改變 {result['changed_reference_count']} 次；方向改變 {result['changed_direction_count']} 次。",
        "固定 window_secs=300、bar_secs=300，因此 TWAP=當前 close，且 alpha>0；"
        "adaptive=alpha×VWAP+(1-alpha)×close，故 close 與參考線的方向一致。此模型下績效相同屬結構性結果，不能推論其他進出場規則的改善。", "",
        "| 台北日曆日期 | 成對決策數 |", "|---|---:|"]
    lines += [f"| {day} | {count} |" for day, count in result["records_by_date"].items()]
    lines += ["", f"結果分類：`{json.dumps(result['unavailable_reasons'], ensure_ascii=False)}`。",
        f"Vol 已校準 {result['volume_calibrated_count']} 組；未知 {result['volume_unknown_count']} 組，不以正常值補齊。", "",
        "## 固定模型與限制", "",
        "- 當前 close 高於／低於參考線做多／做空；等值 flat；下一根連續 5 分 K 的 open 進場、同根 close 出場，固定一單位。",
        "- 只使用決策時已收盤資料計算訊號；未來 K 線僅供成交與結果計算。需要下一根 near/far 均完整才計分，兩組共用樣本。",
        "- OI intensity=1.5、direction=0.65 是假設；quality 是 VolAdjuster 高斯成交量代理，並非歷史 OI 或詐欺品質觀測。",
        "- 零手續費／滑價；所有 PnL 是指數點，非台幣。初始模型權益 40000 點只供百分比與 Sharpe 正規化，不代表實際資金。",
        "- VWAP 使用快照內當期 15:00 重置後可用的完整 NEAR K 線，未保證涵蓋完整交易時段。",
        "- 來源僅涵蓋 7/6 08:46～7/10 05:00，7/10 日盤缺漏；五個日曆日期不等於五個完整交易日。來源無合約代碼與交易所驗真證明。",
        "- MDD 只計已平倉權益，不含持倉內風險。Sharpe 僅五個觀察日期，樣本過短且非獨立，不能視為長期風險估計。",
        "- 此次未調參；未做交易成本、跨時期與樣本外驗證；維持 NO_GO。", "",
        "## 可重現證據", "", f"來源基準 commit：`{manifest['source_commit']}`。",
        "執行模型、參數、原始資料檢查及 SHA256：`logs/ab_test_20261016/model_manifest.json`。",
        "逐筆決策追蹤：`logs/ab_test_20261016/replay_trace.jsonl`；原觀察模式結果保留於 `observed/`。",
        "比較前已檢查 manifest 所有輸出雜湊、來源雜湊與 exact pair ID／時間戳；重複或未配對記錄會直接失敗。", "",
        "原始快照檢查：", "", "```json", json.dumps(manifest["source_audit"], indent=2, ensure_ascii=False), "```", "",
        "來源及執行程式 SHA256：", "", "```json", json.dumps(manifest["source_files"], indent=2), "```", "",
        "## 重現指令", "", "先進入本研究 worktree，以全新 output-dir 重跑，避免混入既有結果：", "", "```bash",
        "taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3 scripts/research/run_adaptive_ab_backtest.py --near data/tmf_near_20260712.csv --far data/tmf_far_20260712.csv --start-date 2026-07-06 --end-date 2026-07-10 --output-dir logs/ab_test_reproduction --initial-equity-points 40000 --config config/adaptive_vwap_config.yaml",
        "taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3 scripts/research/compare_ab_results.py --baseline 'logs/ab_test_reproduction/baseline_*.jsonl' --adaptive 'logs/ab_test_reproduction/adaptive_*.jsonl' --output reports/ab_comparison_reproduction.md",
        "```", ""]
    return "\n".join(lines)


def main(argv=None):
    assert_research_allowed(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, nargs="+")
    parser.add_argument("--adaptive", required=True, nargs="+")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    def expand(patterns):
        paths = [Path(p) for pattern in patterns for p in sorted(glob.glob(pattern))]
        if not paths or len(set(paths)) != len(paths):
            raise ValueError("missing or duplicate input files")
        return paths
    baseline_paths, adaptive_paths = expand(args.baseline), expand(args.adaptive)
    root = baseline_paths[0].parent
    if any(p.parent != root for p in baseline_paths + adaptive_paths):
        raise ValueError("comparison must use one run directory")
    manifest = json.loads((root / "model_manifest.json").read_text())
    if manifest["model"] != MODEL:
        raise ValueError("model manifest mismatch")
    cfg = AdaptiveVWAPConfig.from_mapping(manifest["config"])
    if cfg.window_secs != 300 or cfg.bar_secs != 300:
        raise ValueError("this fixed-model report requires 300-second TWAP/bars")
    for name, digest in manifest["outputs"].items():
        if sha256(root / name) != digest:
            raise ValueError(f"output checksum mismatch: {name}")
    for name, digest in manifest["source_files"].items():
        if sha256(name) != digest:
            raise ValueError(f"source checksum mismatch: {name}")
    baseline, adaptive = load_arm(baseline_paths, "baseline"), load_arm(adaptive_paths, "adaptive")
    if {"baseline": len(baseline), "adaptive": len(adaptive)} != manifest["record_counts"]:
        raise ValueError("manifest record count mismatch")
    result = compare(baseline, adaptive, initial_equity=manifest["initial_equity_points"], config=cfg)
    result["manifest_sha256"] = sha256(root / "model_manifest.json")
    result["comparator_sha256"] = sha256(__file__)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(result, manifest))
    args.output.with_suffix(".json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({k: result[k] for k in ("decision_pairs", "paired_count", "promotion")}))


if __name__ == "__main__":
    import os
    # 2026-10-16 Hermes Agent: keep offline comparison on E-Core policy.
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
