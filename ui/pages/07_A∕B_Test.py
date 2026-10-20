# 2026-10-20 Hermes Agent spec, implemented by Codex: read-only MTS paired A/B dashboard.
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import fmean
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.runtime_paths import mts_runtime_path

TAIPEI = ZoneInfo("Asia/Taipei")
PAIR_FILE_RE = re.compile(r"paired_(\d{8})\.jsonl$")
DETAIL_FIELDS = (
    ("baseline.status", "status"),
    ("adaptive.status", "status"),
    ("baseline.block_reason", "block_reason"),
    ("adaptive.block_reason", "block_reason"),
    ("baseline.signal_15m", "signal_15m"),
    ("adaptive.signal_15m", "signal_15m"),
    ("baseline.vol_anomaly_level", "vol_anomaly_level"),
    ("adaptive.vol_anomaly_level", "vol_anomaly_level"),
    ("baseline.oi_direction", "oi_direction"),
    ("adaptive.oi_direction", "oi_direction"),
    ("baseline.adaptive_price", "adaptive_price"),
    ("adaptive.adaptive_price", "adaptive_price"),
    ("baseline.alpha", "alpha"),
    ("adaptive.alpha", "alpha"),
)


def paired_log_directory() -> Path:
    """Resolve only the active MTS runtime's A/B log directory."""
    return Path(mts_runtime_path("logs", "ab_test"))


def list_available_days(directory=None) -> list[str]:
    root = Path(directory) if directory is not None else paired_log_directory()
    try:
        days = [match.group(1) for path in root.glob("paired_*.jsonl")
                if (match := PAIR_FILE_RE.fullmatch(path.name))]
    except OSError:
        return []
    return sorted(set(days), reverse=True)


def load_paired_records(day: str, directory=None) -> list[dict]:
    """Read one selected day's JSONL; absent or malformed lines are ignored."""
    if not re.fullmatch(r"\d{8}", str(day)):
        return []
    root = Path(directory) if directory is not None else paired_log_directory()
    path = root / f"paired_{day}.jsonl"
    records = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _arm(record, name):
    value = record.get(name)
    return value if isinstance(value, dict) else {}


def _status(arm):
    value = arm.get("status")
    return str(value).upper() if value not in (None, "") else "UNKNOWN"


def _finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _price_comparisons(records):
    hypothetical_deltas = []
    exit_deltas = []
    exit_sides = []
    for record in records:
        baseline, adaptive = _arm(record, "baseline"), _arm(record, "adaptive")
        hypothetical = (
            _finite_number(baseline.get("hypothetical_exit_price")),
            _finite_number(adaptive.get("hypothetical_exit_price")),
        )
        if all(value is not None for value in hypothetical):
            hypothetical_deltas.append(hypothetical[1] - hypothetical[0])

        exits = (
            _finite_number(baseline.get("exit_price")),
            _finite_number(adaptive.get("exit_price")),
        )
        if all(value is not None for value in exits):
            exit_deltas.append(exits[1] - exits[0])
            baseline_leg = str(baseline.get("hypothetical_release_leg") or "").lower()
            adaptive_leg = str(adaptive.get("hypothetical_release_leg") or "").lower()
            baseline_state = baseline.get(baseline_leg)
            adaptive_state = adaptive.get(adaptive_leg)
            baseline_side = (baseline_state.get("side")
                             if isinstance(baseline_state, dict) else None)
            adaptive_side = (adaptive_state.get("side")
                             if isinstance(adaptive_state, dict) else None)
            baseline_exit_side = {"LONG": "SELL", "SHORT": "BUY"}.get(
                str(baseline_side).upper() if baseline_side is not None else ""
            )
            adaptive_exit_side = {"LONG": "SELL", "SHORT": "BUY"}.get(
                str(adaptive_side).upper() if adaptive_side is not None else ""
            )
            exit_sides.append(
                baseline_exit_side
                if baseline_exit_side is not None and baseline_exit_side == adaptive_exit_side
                else None
            )

    return hypothetical_deltas, exit_deltas, exit_sides


def summarize_selected_day(records) -> dict:
    total = len(records)
    valid = 0
    valid_mismatches = 0
    unknown = 0
    mismatches = 0
    for record in records:
        baseline_status = _status(_arm(record, "baseline"))
        adaptive_status = _status(_arm(record, "adaptive"))
        pair_is_valid = baseline_status != "UNKNOWN" and adaptive_status != "UNKNOWN"
        valid += pair_is_valid
        # 2026-10-20 Hermes Agent: conclusion agreement uses only effective samples.
        valid_mismatches += pair_is_valid and baseline_status != adaptive_status
        unknown += baseline_status == "UNKNOWN" or adaptive_status == "UNKNOWN"
        mismatches += baseline_status != adaptive_status

    hypothetical, executed, exit_sides = _price_comparisons(records)
    hypothetical_mean = fmean(hypothetical) if hypothetical else None
    exit_mean = fmean(executed) if executed else None
    return {
        "total_pairs": total,
        "valid_samples": valid,
        "status_consistency_rate": (valid - valid_mismatches) / valid if valid else None,
        "unknown_pairs": unknown,
        "status_mismatch_count": mismatches,
        "status_mismatch_ratio": mismatches / total if total else None,
        "hypothetical_comparison_pairs": len(hypothetical),
        "price_comparison_pairs": len(executed),
        "exit_comparison_sides": exit_sides,
        "hypothetical_exit_delta_points": hypothetical_mean,
        "hypothetical_exit_delta_twd": hypothetical_mean * 10 if hypothetical_mean is not None else None,
        "exit_delta_points": exit_mean,
        "exit_delta_twd": exit_mean * 10 if exit_mean is not None else None,
    }


# 2026-10-20 Hermes Agent: explain paired results from existing summaries and price comparisons.
CONCLUSION_EXPLANATION = (
    "Baseline 是相同輸入下關閉 adaptive observation 的候選判定；adaptive 是在 baseline 判定上附加 "
    "AdaptiveVWAP observation 的結果，使用近月 bars、OI snapshot 與 volume 品質資料，不重新評分 voters。"
    "判定層比較兩邊 status；出場價層比較兩邊皆有出場價的配對。暖機期 UNKNOWN 已排除於有效樣本。"
    "這是 shadow 評估，不影響實際下單。decision_ts 保留原始值。"
)


def build_conclusion(records) -> dict[str, str]:
    summary = summarize_selected_day(records)
    valid = summary["valid_samples"]
    if valid == 0:
        return {"conclusion": "目前無有效樣本（全為暖機/UNKNOWN），無法下結論。",
                "explanation": CONCLUSION_EXPLANATION}

    n = summary["price_comparison_pairs"]
    if n == 0:
        consistency = summary["status_consistency_rate"]
        agreement = consistency * 100 if consistency is not None else 0.0
        return {
            "conclusion": (
                f"判定層：兩邊 status 一致率 {agreement:.1f}%（有效樣本 {valid}）。"
                "出場價層：尚無兩邊皆有出場價的配對，無法比較 adaptive 與 baseline 的出場價優劣。"
            ),
            "explanation": CONCLUSION_EXPLANATION,
        }

    delta = summary["exit_delta_points"]
    twd = summary["exit_delta_twd"]
    comparable_sides = summary["exit_comparison_sides"]
    exit_side = comparable_sides[0] if comparable_sides else None
    if exit_side is None or len(comparable_sides) != n or any(
        side != exit_side for side in comparable_sides
    ):
        amount = f"{delta:+.2f} 點（{twd:+.1f} TWD）"
        phrase = f"出場價層：adaptive 與 baseline 平均價差 {amount}，方向未定，n={n}"
    elif delta == 0:
        phrase = f"出場價層：adaptive 與 baseline 平均持平 0.00 點（0.0 TWD），n={n}"
    else:
        is_better = (delta > 0) if exit_side == "SELL" else (delta < 0)
        quality = "好" if is_better else "差"
        phrase = (
            f"出場價層：adaptive 平均較 baseline {quality} {abs(delta):.2f} 點"
            f"（{abs(twd):.1f} TWD），n={n}"
        )

    if n < 30:
        phrase += "，樣本不足 30，僅供參考。"
    return {"conclusion": phrase, "explanation": CONCLUSION_EXPLANATION}


def _distribution(records, arm_name, field):
    counts = Counter()
    for record in records:
        arm = _arm(record, arm_name)
        value = _status(arm) if field == "status" else arm.get(field)
        if value in (None, ""):
            continue
        counts[str(value)] += 1
    return ", ".join(f"{name}: {count}" for name, count in
                      sorted(counts.items(), key=lambda item: (-item[1], item[0]))) or "N/A"


def _top_reasons(records, arm_name):
    counts = Counter(
        str(_arm(record, arm_name).get("block_reason"))
        for record in records
        if _arm(record, arm_name).get("block_reason") not in (None, "")
    )
    return ", ".join(f"{reason}: {count}" for reason, count in
                      sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]) or "N/A"


def build_daily_summary(records_by_day) -> list[dict]:
    rows = []
    for day in sorted(records_by_day, reverse=True):
        records = records_by_day[day]
        summary = summarize_selected_day(records)
        rows.append({
            "日期": day,
            "baseline.status 分布": _distribution(records, "baseline", "status"),
            "adaptive.status 分布": _distribution(records, "adaptive", "status"),
            "baseline.block_reason Top 3": _top_reasons(records, "baseline"),
            "adaptive.block_reason Top 3": _top_reasons(records, "adaptive"),
            "有效樣本數": summary["valid_samples"],
        })
    return rows


def _display_value(value):
    if value is None or value == "":
        return "N/A"
    number = _finite_number(value)
    if number is not None and isinstance(value, (int, float)):
        return f"{number:g}"
    return str(value)


def _display_time(value):
    timestamp = _finite_number(value)
    if timestamp is None:
        return "N/A"
    try:
        return datetime.fromtimestamp(timestamp, TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "N/A"


def build_detail_rows(records) -> list[dict]:
    def sort_key(record):
        timestamp = _finite_number(record.get("decision_ts"))
        return timestamp if timestamp is not None else float("-inf")

    ordered = sorted(records, key=sort_key, reverse=True)
    rows = []
    for record in ordered:
        row = {"時間": _display_time(record.get("decision_ts"))}
        for column, field in DETAIL_FIELDS:
            arm_name, _ = column.split(".", 1)
            row[column] = _display_value(_arm(record, arm_name).get(field))
        rows.append(row)
    return rows


def _delta_label(points, twd, count):
    if points is None or twd is None:
        return "N/A"
    return f"{points:+.2f} 點 / {twd:+.1f} TWD（{count} 組完整配對）"


def render_page():
    st.set_page_config(page_title="A/B Test", page_icon="🧪", layout="wide")
    st.title("A/B Test")
    st.caption("唯讀 paired shadow records；不執行交易，也不寫入資料。")
    st.caption("decision_ts 為原始值；以原始 epoch 值轉成台北時間顯示，不修正既有時間偏移。")

    directory = paired_log_directory()
    days = list_available_days(directory)
    if not days:
        st.info("無資料：目前 runtime mode 沒有 paired A/B 紀錄。")
        return

    records_by_day = {day: load_paired_records(day, directory) for day in days}
    selected_day = st.selectbox("選擇日期", days, index=0)
    records = records_by_day.get(selected_day, [])
    if not records:
        st.info("無資料：所選日期沒有有效 paired records。")

    summary = summarize_selected_day(records)
    st.subheader(f"{selected_day} 摘要")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總配對數", summary["total_pairs"])
    c2.metric("有效樣本數", summary["valid_samples"])
    c3.metric("暖機 / UNKNOWN 筆數", summary["unknown_pairs"])
    mismatch = summary["status_mismatch_ratio"]
    c4.metric(
        "Status 不一致比例",
        f"{mismatch:.1%}" if mismatch is not None else "N/A",
        f"{summary['status_mismatch_count']} / {summary['total_pairs']} 組",
    )

    conclusion = build_conclusion(records)
    st.subheader("結論說明")
    st.write(conclusion["conclusion"])
    st.caption(conclusion["explanation"])

    hypothetical_points = summary["hypothetical_exit_delta_points"]
    hypothetical_twd = summary["hypothetical_exit_delta_twd"]
    exit_points = summary["exit_delta_points"]
    exit_twd = summary["exit_delta_twd"]
    hypothetical_count = summary["hypothetical_comparison_pairs"]
    price_count = summary["price_comparison_pairs"]
    p1, p2 = st.columns(2)
    p1.metric("Hypothetical exit 價差（adaptive − baseline）",
              _delta_label(hypothetical_points, hypothetical_twd, hypothetical_count))
    p2.metric("Executed exit 價差（adaptive − baseline）",
              _delta_label(exit_points, exit_twd, price_count))
    st.caption("Hypothetical 與 executed 出場價各自只比較兩邊都有對應價格的配對；一點 = 10 TWD，不補值。")

    st.subheader("每日彙總")
    st.dataframe(pd.DataFrame(build_daily_summary(records_by_day)), use_container_width=True, hide_index=True)

    st.subheader("配對明細")
    limit = int(st.number_input("最近 N 筆", min_value=1, max_value=500, value=50, step=1))
    details = build_detail_rows(records)[:limit]
    if details:
        st.dataframe(pd.DataFrame(details), use_container_width=True, hide_index=True)
    else:
        st.info("無資料可顯示。")


if __name__ == "__main__":
    render_page()
