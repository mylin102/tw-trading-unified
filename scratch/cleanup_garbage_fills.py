import json
import os

# 2026-07-01 Gemini CLI: Script to clean up garbage trade entries with 40118 price anomaly.
# Affected trade IDs:
GARBAGE_IDS = {
    "mts-auto-031117-455",
    "mts-auto-033623-232",
    "mts-20260701-114955",
    "mts-20260701-121803"
}

files_to_clean = [
    "logs/mts_trade_fills.jsonl",
    "logs/mts_spread_events.jsonl"
]

for filename in files_to_clean:
    if not os.path.exists(filename):
        print(f"File {filename} not found, skipping.")
        continue
        
    print(f"Cleaning {filename}...")
    filtered_lines = []
    removed_count = 0
    
    with open(filename, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                trade_id = data.get("trade_id")
                if trade_id in GARBAGE_IDS:
                    removed_count += 1
                    continue
                filtered_lines.append(line)
            except Exception as e:
                # If JSON parsing fails, keep the line but log warning
                print(f"Warning: failed to parse JSON line: {line.strip()} - {e}")
                filtered_lines.append(line)
                
    # Write back the filtered lines
    with open(filename, "w") as f:
        f.writelines(filtered_lines)
        
    print(f"Done. Removed {removed_count} lines from {filename}.")
