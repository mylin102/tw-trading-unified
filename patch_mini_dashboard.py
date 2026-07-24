"""Apply dashboard spread-fix patches via targeted line operations."""
import sys

path = sys.argv[1]

with open(path) as f:
    lines = f.readlines()

original = list(lines)

# Step 1: Insert "import re" after "import time" at line 18 (0-indexed 17)
line17 = lines[17].strip()
if line17 == "import time" and lines[18].strip() != "import re":
    lines.insert(18, "import re\n")
    print("Step 1: inserted import re")
else:
    print("Step 1: skipped (already present or unexpected content)")

# Step 2: Find the line with "@st.cache_data(ttl=30)" before load_calendar_spread_data
# and the "    return None" line before it
target_before = "    return None\n"
target_decorator = "@st.cache_data(ttl=30)\n"
target_def = "def load_calendar_spread_data():\n"

helper_block = """    return None

# ── Spread CSV discovery helpers (2026-07-22: ticker-scoped, no mtime cross-ticker) ──
_SPREAD_FILE_RE = re.compile(
    r"^(?P<ticker>[a-z0-9]+)_calendar_spread_(?P<date>\\d{8})\\.csv$"
)


def _latest_spread_csv(ticker: str, data_dir=None):
    \"\"\"Find the latest calendar spread CSV for a specific ticker.

    Selection priority: filename date, then st_mtime_ns tiebreaker.
    Never crosses ticker boundaries.
    \"\"\"
    from pathlib import Path
    if data_dir is None:
        data_dir = Path("data")
    normalized_ticker = ticker.lower()
    candidates = []
    for path in data_dir.glob(f"{normalized_ticker}_calendar_spread_*.csv"):
        m = _SPREAD_FILE_RE.fullmatch(path.name)
        if m is None:
            continue
        if m.group("ticker") != normalized_ticker:
            continue
        candidates.append((m.group("date"), path))
    if not candidates:
        return None
    _, latest_path = max(
        candidates,
        key=lambda item: (item[0], item[1].stat().st_mtime_ns),
    )
    return latest_path


@st.cache_data
def _load_spread_csv(path_str: str, mtime_ns: int):
    \"\"\"Load spread CSV, keyed by path + mtime_ns for cache invalidation.\"\"\"
    del mtime_ns
    import pandas as pd
    try:
        df = pd.read_csv(path_str)
        if "timestamp" not in df.columns:
            if "ts" in df.columns:
                df = df.rename(columns={"ts": "timestamp"})
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.dropna(subset=["timestamp"])
        return df
    except Exception as e:
        print(f"[Calendar Spread] LOAD FAILED: {e}")
        return None


@st.cache_data(ttl=60)
def load_calendar_spread_data():\n"""

# Find the insertion point
insert_idx = None
for i in range(len(lines)):
    if lines[i] == target_before and i + 1 < len(lines) and lines[i + 1] == target_decorator:
        insert_idx = i + 1  # insert BEFORE the decorator, replacing it
        break

if insert_idx is not None:
    # Remove the old decorator and def lines (we replace them)
    # Find the def line
    end_idx = insert_idx
    for j in range(insert_idx, min(insert_idx + 5, len(lines))):
        if lines[j] == target_def:
            end_idx = j + 1
            break
    # Replace the range
    lines[insert_idx - 1:end_idx] = [helper_block]
    print(f"Step 2: replaced lines {insert_idx-1}-{end_idx-1} with helpers")
else:
    print("Step 2: marker not found")

# Step 3: Find and replace the old body pattern inside load_calendar_spread_data
# The old body has: glob("*spread*.csv"), max(files, key=mtime), pd.read_csv
# Search for the exact pattern
for i in range(len(lines)):
    stripped = lines[i].strip()
    if stripped.startswith("spread_files = list(Path(\"data\").glob(\"*spread*.csv\"))"):
        # This is the first line of the old body
        body_start = i
        # Find where the old body ends (next line with "def " at column 0, or "@st.cache_data")
        body_end = body_start
        for j in range(body_start, min(body_start + 15, len(lines))):
            ls = lines[j].strip()
            if ls.startswith("df = pd.read_csv(latest_file)"):
                body_end = j + 1
                break
        
        new_body = [
            "        # 優先載入預先計算的價差檔案（ticker-scoped）\n",
            "        spread_path = _latest_spread_csv(_TICKER)\n",
            "        if spread_path is not None:\n",
            "            stat = spread_path.stat()\n",
            "            df = _load_spread_csv(str(spread_path.resolve()), stat.st_mtime_ns)\n",
            "            if df is not None and not df.empty:\n",
            "                numeric_cols = [\"spread\", \"spread_z\", \"spread_ma\", \"spread_std\",\n",
            "                               \"vwap_z\", \"price_vs_vwap\", \"Close_near\", \"Close_far\"]\n",
            "                for col in numeric_cols:\n",
            "                    if col in df.columns:\n",
            "                        df[col] = pd.to_numeric(df[col], errors=\"coerce\")\n",
            "                df = df.replace([np.inf, -np.inf], np.nan)\n",
            "                print(f\"[Calendar Spread] 載入價差資料: {len(df)} 筆, \"\n",
            "                      f\"來自 {spread_path.name}, \"\n",
            "                      f\"範圍 {df['timestamp'].min()} ~ {df['timestamp'].max()}\")\n",
            "                return df\n",
            "            print(f\"[Calendar Spread] 載入失敗或空資料: {spread_path.name}\")\n",
            "        else:\n",
            "            print(f\"[Calendar Spread] 找不到 {_TICKER} calendar spread CSV\")\n",
        ]
        lines[body_start:body_end] = new_body
        print(f"Step 3: replaced body at line {body_start}-{body_end-1}")
        break
else:
    print("Step 3: body pattern not found")

# Step 4: Remove dead code (old timestamp normalization referencing df/latest_file)
# Find the line with "找不到 {_TICKER} calendar spread CSV" and remove everything
# between it and "如果沒有預先計算的檔案"
dead_start = None
dead_end = None
for i in range(len(lines)):
    stripped = lines[i].strip()
    if "找不到 {_TICKER} calendar spread CSV" in stripped:
        dead_start = i + 1  # start of dead code
    if dead_start is not None and "如果沒有預先計算的檔案" in stripped:
        dead_end = i
        break

if dead_start is not None and dead_end is not None and dead_end > dead_start:
    # Check if there's actual dead code
    dead_lines = lines[dead_start:dead_end]
    dead_text = "".join(dead_lines).strip()
    if dead_text and ("df[" in dead_text or "latest_file" in dead_text or "standardize" in dead_text.lower() or "標準化" in dead_text):
        lines[dead_start:dead_end] = ["\n"]
        print(f"Step 4: removed dead code lines {dead_start}-{dead_end-1}")
    else:
        print(f"Step 4: no dead code found (text={repr(dead_text[:80])})")
else:
    print(f"Step 4: marker not found (dead_start={dead_start}, dead_end={dead_end})")

# Write if changed
if lines != original:
    with open(path, "w") as f:
        f.writelines(lines)
    print("FILE_WRITTEN")
else:
    print("NO_CHANGES")
