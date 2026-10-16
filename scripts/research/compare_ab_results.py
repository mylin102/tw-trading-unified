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


def compare_v2(baseline_paths, adaptive_paths, root, manifest):
    """2026-10-16 Hermes Agent: separate same-state probes, portfolio actions and paired exits."""
    from scripts.research.adaptive_validation_v2 import MODEL_NAME
    def load(paths, arm):
        rows = {}
        for path in paths:
            for line in path.read_text().splitlines():
                r = json.loads(line)
                if r["pair_id"] in rows or r["arm"] != arm or r["mode"] != MODEL_NAME or r["real_order_emitted"]:
                    raise ValueError("duplicate or invalid v2 pair")
                if not math.isfinite(r["liquidation_equity_twd"]):
                    raise ValueError("invalid v2 equity")
                rows[r["pair_id"]] = r
        return rows
    a, b = load(baseline_paths, "baseline"), load(adaptive_paths, "adaptive")
    if not a or a.keys() != b.keys() or len(a) != manifest["records_per_arm"]:
        raise ValueError("unpaired or truncated v2 records")
    for key, r in a.items():
        if any(r[k] != b[key][k] for k in ("ts", "features", "current_spread")):
            raise ValueError("v2 paired input mismatch")
        for leg in ("near", "far"):
            cal_ts = r["features"][leg]["volume_model"]["max_calibration_ts"]
            if cal_ts is not None and cal_ts >= r["ts"]:
                raise ValueError("calibration lookahead")
    ordered = sorted(a, key=lambda k:a[k]["ts"])
    if len({a[k]["ts"] for k in ordered}) != len(ordered):
        raise ValueError("duplicate decision timestamp")
    initial = manifest["validation_config"]["initial_equity_twd"]
    last = dict(baseline=initial, adaptive=initial)
    increments = []
    for key in ordered:
        pair = dict(pair_id=key, ts=a[key]["ts"])
        for arm, data in (("baseline",a), ("adaptive",b)):
            equity = data[key]["liquidation_equity_twd"]
            pair[arm] = {"net_pnl":equity-last[arm]}
            last[arm] = equity
        increments.append(pair)
    risk = compare_outcomes(increments, initial_equity=initial)
    portfolio = json.loads((root / "portfolio.json").read_text())
    def probe_key(r):
        return r["probe"]["pass_release"], r["probe"]["release_leg"]
    def action_key(r):
        action = r["action"] or {}
        return action.get("action", "HOLD"), action.get("leg")
    # 2026-10-16 Hermes Agent: missing EMA/calibration cannot inflate the 5% reference-effect gate.
    common_reference = {k for k in a if all(a[k]["features"][leg]["adaptive_price"] is not None
                                           for leg in ("near","far"))}
    probe_diff = sum(probe_key(a[k]) != probe_key(b[k]) for k in common_reference)
    availability_diff = sum(probe_key(a[k]) != probe_key(b[k]) for k in a if k not in common_reference)
    action_diff = sum(action_key(a[k]) != action_key(b[k]) for k in a)
    result = dict(schema_version=2, promotion="NO_GO", model=manifest["model"], paired_decisions=len(a),
        initial_equity_twd=initial, same_state_probe_difference_count=probe_diff,
        common_reference_pairs=len(common_reference), availability_only_probe_difference_count=availability_diff,
        same_state_probe_difference_pct=100*probe_diff/len(a), portfolio_action_difference_count=action_diff,
        portfolio_action_difference_pct=100*action_diff/len(a), baseline={}, adaptive={},
        common_entry_status=dict(Counter(r["common_entry_status"] for r in a.values())),
        both_spread_reference_ready_pairs=sum(k in common_reference and a[k]["phase"]==b[k]["phase"]=="SPREAD" for k in a))
    episodes = {}
    for arm, data in (("baseline",a), ("adaptive",b)):
        closed = portfolio[arm]["closed_episodes"]
        episodes[arm] = {e["episode_id"]:e for e in closed}
        if len(episodes[arm]) != len(closed):
            raise ValueError("duplicate closed episode")
        pnl = [e["net_pnl_twd"] for e in closed]
        result[arm] = dict(closed_spreads=len(closed),
            win_rate_pct=100*sum(p>0 for p in pnl)/len(pnl) if pnl else None,
            mean_closed_net_twd=statistics.mean(pnl) if pnl else None,
            closed_net_twd=sum(pnl), final_cash_twd=portfolio[arm]["final_cash_twd"],
            final_liquidation_equity_twd=last[arm], net_liquidation_change_twd=last[arm]-initial,
            max_drawdown_pct=risk[arm]["max_drawdown_pct"], daily_sharpe=risk[arm]["sharpe"],
            open_episode=portfolio[arm]["open_episode"] is not None,
            pending_unfilled=portfolio[arm]["unfilled_pending"] is not None,
            probe_pass_count=sum(r["probe"]["pass_release"] for r in data.values()),
            probe_block_reasons=dict(Counter(r["probe"]["reason"] for r in data.values())),
            actions=dict(Counter((r["action"] or {}).get("action", "HOLD") for r in data.values())),
            exit_reasons=dict(Counter(e["reason"] for e in portfolio[arm]["fills"] if e["event"]=="EXIT_FILL")),
            data_gaps=sum(e["data_gap_count"] for e in closed)+(portfolio[arm]["open_episode"] or {}).get("data_gap_count",0))
    paired = episodes["baseline"].keys() & episodes["adaptive"].keys()
    changed_exits = baseline_wins = adaptive_wins = ties = 0
    for key in paired:
        ea, eb = episodes["baseline"][key], episodes["adaptive"][key]
        if ea["entry_ts"] != eb["entry_ts"]:
            raise ValueError("paired episodes must share entry time")
        exit_signature = lambda ep: sorted((e["leg"],e["ts"],e["price"]) for e in ep["exits"])
        changed_exits += exit_signature(ea) != exit_signature(eb)
        delta = eb["net_pnl_twd"]-ea["net_pnl_twd"]
        baseline_wins += delta < -1e-8
        adaptive_wins += delta > 1e-8
        ties += abs(delta) <= 1e-8
    result.update(paired_closed_spreads=len(paired), changed_exit_spreads=changed_exits,
        paired_baseline_wins=baseline_wins, paired_adaptive_wins=adaptive_wins, paired_ties=ties,
        capital_normalized_improvement_bps=(last["adaptive"]-last["baseline"])/initial*10000,
        records_by_date=dict(sorted(Counter(datetime.fromtimestamp(r["ts"], TW).date().isoformat() for r in a.values()).items())))
    result["volume"] = {}
    for leg in ("near", "far"):
        models = [r["features"][leg]["volume_model"] for r in a.values()]
        valid = [m for m in models if m["zscore"] is not None]
        result["volume"][leg] = dict(valid=len(valid), unknown=len(models)-len(valid),
            median_z=statistics.median(m["zscore"] for m in valid) if valid else None,
            extreme_pct=100*sum(m["anomaly"]=="EXTREME" for m in valid)/len(valid) if valid else None,
            high_or_extreme_pct=100*sum(m["anomaly"] in ("HIGH","EXTREME") for m in valid)/len(valid) if valid else None)
    threshold = manifest["validation_config"]["minimum_decision_difference_pct"]
    result["acceptance"] = dict(minimum_difference_pct=threshold,
        same_state_probe_at_least_5pct=result["same_state_probe_difference_pct"]>=threshold,
        portfolio_action_at_least_5pct=result["portfolio_action_difference_pct"]>=threshold,
        different_paired_exits=changed_exits>0,
        decision_rule="Both same-state and portfolio action rates must meet threshold; reasons alone are not differences")
    result["framework_acceptance"] = "PASS" if all(result["acceptance"][k] for k in
        ("same_state_probe_at_least_5pct","portfolio_action_at_least_5pct","different_paired_exits")) else "NOT_MET"
    return result


def render_v2(r, manifest):
    """2026-10-16 Hermes Agent: do not present observability as profitable improvement."""
    fmt = lambda v:"N/A" if v is None else f"{v:.4f}"
    lines = ["# Adaptive A/B v2 雙腿生命週期回放 — 2026-10-16", "",
        f"框架差異驗收：**{r['framework_acceptance']}**；策略提升／部署判定：**NO_GO**。", "",
        "固定 20260712 near/far 快照；5 個部分覆蓋日曆日期。以下是核准離線 bar proxy，非完整 tick／實盤重建。", "",
        "## 決策差異", "",
        f"共 {r['paired_decisions']} 組成對決策，分母保留所有暖機／缺漏樣本。",
        f"雙腿參考線皆有效 {r['common_reference_pairs']} 組；另有 {r['availability_only_probe_difference_count']} 次可用性造成的候選差異，未納入5%門檻。",
        f"相同假設持倉下 Trend Release 候選差異：{r['same_state_probe_difference_count']} / {r['paired_decisions']} = {r['same_state_probe_difference_pct']:.4f}%。",
        f"實際模擬持倉路徑 action／leg 差異：{r['portfolio_action_difference_count']} / {r['paired_decisions']} = {r['portfolio_action_difference_pct']:.4f}%。",
        f"共同已平倉 spread：{r['paired_closed_spreads']}；出場時間／價格改變：{r['changed_exit_spreads']}。",
        f"成對淨利比較：baseline 勝 {r['paired_baseline_wins']}、adaptive 勝 {r['paired_adaptive_wins']}、平手 {r['paired_ties']}。", "",
        f"兩組同為 SPREAD 且參考線有效的實際決策點：{r['both_spread_reference_ready_pairs']}；共同進場狀態：`{json.dumps(r['common_entry_status'])}`。", "",
        "相同狀態 probe 隔離參考線／門檻效果；portfolio action 包含持倉路徑分歧。僅 block reason 改變不計入決策差異。", "",
        "## 成本後指標", "", "| 指標 | Baseline | Adaptive | Adaptive − Baseline |", "|---|---:|---:|---:|"]
    for label,key in (("已平倉雙腿 episode 數","closed_spreads"),("已平倉勝率 %","win_rate_pct"),
            ("已平倉平均淨利 TWD","mean_closed_net_twd"),("已平倉累計淨利 TWD","closed_net_twd"),
            ("期末現金 TWD","final_cash_twd"),("期末估計清算權益 TWD","final_liquidation_equity_twd"),
            ("清算權益變化 TWD（含未平倉）","net_liquidation_change_twd"),("清算權益 MDD %","max_drawdown_pct"),
            ("日報酬年化 Sharpe（僅5日期）","daily_sharpe"),("Trend Release probe 通過數","probe_pass_count")):
        a,b = r["baseline"][key],r["adaptive"][key]
        lines.append(f"| {label} | {fmt(a)} | {fmt(b)} | {fmt(b-a) if a is not None and b is not None else 'N/A'} |")
    lines += ["", f"期末清算權益差 / 初始模型資金：{r['capital_normalized_improvement_bps']:.4f} bps（非價差報酬率）。", "",
        "## 固定驗證模型", "",
        "- EMA：選方案 B，5 根完整 5 分 K、1500 秒有限視窗，最舊 close 為種子、權重 2/(5+1)。缺根或不足 5 根不產生參考線。60 秒方案在 5 分 K 上仍退化，未採用。",
        "- baseline／adaptive 均要求雙腿同方向、price-reference 距離至少 0.1%、連續 2 根確認，沿用 Renko＋ADL＋參考線 2-of-3 仲裁。SPREAD 使用既有 entry-side 映射：LONG NEAR／SHORT FAR 只在看多確認後釋放 FAR；SINGLE_LEG 另外允許確認反轉出場。",
        "- adaptive 額外要求雙腿 alpha≥0.3、adaptive-VWAP 位移至少 0.1%，且方向與 VWAP 一致。故 A/B 同時改變參考線與確認門檻，不能把差異全歸因 EMA。",
        "- 共享假設 LONG NEAR / SHORT FAR 進場；兩組均 flat 且滿足冷卻／資金條件才建立下一組。兩組分別維護 SPREAD→SINGLE_LEG→FLAT，不重疊計算 episode。",
        "- 進出場均在決策後下一根完整配對 K 線 open，採 adverse slippage；缺根不把舊訊號延至後續開盤成交。未平倉部位與未成交 pending 不偽造終點成交。",
        "- 直接呼叫既有 evaluate_lifecycle_actions：保留 hard stop／timeout、Policy J、Trend Release、protective release、reversal、trail 優先順序。門檻只影響 trend，不能擋風控。",
        "- marks_live=False：保留 closed-bar 下 Policy J release-intercept 拒絕；giveback／single-leg 路徑使用原函式。peak 是 bar close 代理，無法驗證 tick 確認。",
        "- 既有 lifecycle Policy J 決策內部固定 10 TWD/點、92 TWD friction，原樣保留並限制 product point_value=10。成交帳則另按 product 設定計費／稅／滑價，兩者未混為真實成本。",
        f"- 初始研究資金 {r['initial_equity_twd']} TWD；屬假設持倉研究，不是 paper 執行授權或既有 40000 TWD runtime 資金上限變更。", "",
        "## 成交量校準", "", "| 腿 | 有效 / 未知 | causal Z 中位數 | EXTREME % | HIGH+EXTREME % |", "|---|---:|---:|---:|---:|"]
    for leg,v in r["volume"].items():
        lines.append(f"| {leg} | {v['valid']} / {v['unknown']} | {fmt(v['median_z'])} | {fmt(v['extreme_pct'])} | {fmt(v['high_or_extreme_pct'])} |")
    lines += ["", "每腿／時段僅用過去最多120根完整5分量，至少30根才評分；當前量先評分再加入校準。"
        "全快照均值／標準差僅列診斷，不回填歷史訊號。原 ZSCORE_PARAMS 未改，避免同週訓練同週驗證前視。", "",
        "## 路徑與限制", "", "```json", json.dumps({arm:{k:r[arm][k] for k in
            ("actions","exit_reasons","probe_block_reasons","open_episode","pending_unfilled","data_gaps")} for arm in ("baseline","adaptive")},indent=2), "```", "",
        "尚缺來源合約／成交量單位驗真、OI／品質事件、tick MID／BBO、實際進場紀錄與完整7/10日盤；未做樣本外驗證。決策差異不等於績效改善。", "",
        "來源／執行參數／雜湊：`model_manifest.json`；逐筆決策／成交：每日 baseline/adaptive JSONL、`portfolio.json`、`replay_trace.jsonl`。", "",
        f"來源基準 commit：`{manifest['source_commit']}`。", ""]
    return "\n".join(lines)


def render_volume(diagnosis):
    """2026-10-16 Hermes Agent: descriptive unit uncertainty and leakage-safe calibration."""
    fmt = lambda v:"N/A" if v is None else f"{v:.3f}"
    lines = ["# 成交量校準診斷 — 2026-10-16", "",
        "資料僅為固定 20260712 快照的 7/6～7/10 部分樣本，不能稱為完整 2026-07 母體統計。", "",
        "原回放 98.63% 是 HIGH+EXTREME 合計，EXTREME 單獨 96.57%。", "",
        "## 統計比較", "", "| 腿 / 時段 | N | 5分量均值 | 標準差 | 舊均值 / std | 舊 Z 中位數 | 舊 EXTREME % | causal N | causal Z 中位數 | causal EXTREME % |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|"]
    for s in diagnosis["snapshot_statistics"]:
        old=s["old_params"]
        lines.append(f"| {s['leg']} / {s['segment']} | {s['count']} | {fmt(s['mean'])} | {fmt(s['std'])} | {str(old) if old else '未定義'} | {fmt(s['old_median_z'])} | {fmt(s['old_extreme_pct'])} | {s['causal_count']} | {fmt(s['causal_median_z'])} | {fmt(s['causal_extreme_pct'])} |")
    lines += ["", "## 單位與處理決定", "",
        "來源分鐘資料的數值檢查（仍無單位認證）：", "", "```json",
        json.dumps(diagnosis["source_unit_evidence"],indent=2), "```", "",
        "CSV 欄位只有 Volume／Amount，沒有資料字典、合約代碼或原始供應商單位證明。Volume 為來源數值，重採樣按五個完整分鐘相加；沒有乘以點值或價格。僅憑欄位不能確認它是口數、是否雙計或是否與舊校準同一合約。", "",
        "因此不把全快照均值直接寫回 production ZSCORE_PARAMS。v2 採每腿／時段 prior-only rolling mean/std；先評分、再加入當根量；至少30根，最多120根。off_segment 另分組，未知／零變異保留空值。", "",
        "全快照統計僅供診斷，未參與校準；rolling 校準只調整訊號分布，沒有用 PnL 選參。較低異常率不證明資料正確或分布常態，仍需驗證來源單位與跨期穩定性。", ""]
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
    if manifest.get("schema_version") not in (1,2):
        raise ValueError("unsupported model manifest")
    for name, digest in manifest["outputs"].items():
        if sha256(root / name) != digest:
            raise ValueError(f"output checksum mismatch: {name}")
    for name, digest in manifest["source_files"].items():
        if sha256(name) != digest:
            raise ValueError(f"source checksum mismatch: {name}")
    if manifest["schema_version"] == 2:
        from scripts.research.adaptive_validation_v2 import MODEL_NAME
        if manifest["model"] != MODEL_NAME:
            raise ValueError("v2 model mismatch")
        result = compare_v2(baseline_paths, adaptive_paths, root, manifest)
        result["manifest_sha256"] = sha256(root / "model_manifest.json")
        result["comparator_sha256"] = sha256(__file__)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_v2(result, manifest))
        args.output.with_suffix(".json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
        args.output.with_name("volume_calibration_diagnosis.md").write_text(render_volume(json.loads((root / "volume_diagnosis.json").read_text())))
        print(json.dumps({k:result[k] for k in ("paired_decisions","same_state_probe_difference_pct","portfolio_action_difference_pct","framework_acceptance","promotion")}))
        return
    if manifest["model"] != MODEL:
        raise ValueError("model manifest mismatch")
    cfg = AdaptiveVWAPConfig.from_mapping(manifest["config"])
    if cfg.reference_method != "twap" or cfg.window_secs != 300 or cfg.bar_secs != 300:
        raise ValueError("v1 report requires its frozen 300-second TWAP")
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
