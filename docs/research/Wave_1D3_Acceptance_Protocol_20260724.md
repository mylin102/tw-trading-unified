# Wave 1D.3 Production Shadow Soak Acceptance Protocol & Evidence Archive

**Archive Date**: 2026-07-24  
**Author**: Gemini CLI & Quantitative Trading System Architecture Team  
**Baseline Commit**: `66d1e7cd`  
**Static Unit Acceptance Status**: PASS (97 / 97 Tests PASSED)  
**Static Unit Test Evidence**: [data/acceptance/wave1d3-static-unit.xml](file:///Users/mylin/Documents/mylin102/tw-trading-unified/data/acceptance/wave1d3-static-unit.xml)  
**Independent Verifier Engine**: [strategies/futures/mts/acceptance_verifier.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/futures/mts/acceptance_verifier.py)  

---

## 📌 1. 專案當前權威狀態 (Authoritative State - 2026-07-24)

```text
RC1 Baseline            : 66d1e7cd
Remote Alignment        : PASS (Mini 遠端同步 100% 通過)
Static Acceptance       : PASS — 97/97 (data/acceptance/wave1d3-static-unit.xml)
Independent Verifier    : READY (strategies/futures/mts/acceptance_verifier.py)
Dynamic Soak Observation: GO / RUNNABLE
Wave 1E                 : BLOCKED (Pending Independent Verifier PASS)
```

---

## 🏛️ 2. 九大標準驗收門禁規約 (The 9 Standard Acceptance Gates)

```text
       Raw Telemetry Files (*.jsonl)  +  manifest.json  +  manifest.sha256
                                      │
                                      ▼
               Independent Acceptance Verifier (acceptance_verifier.py)
                                      │
       ┌──────────────────────────────┴──────────────────────────────┐
       ▼                                                             ▼
[G1] Provenance & Preflight (66d1e7cd)                [G6] Session Coverage (Day>=100, Night>=100)
[G2] Non-Interference (6 Counters=0)                  [G7] Controlled Restart (Segments>=2)
[G3] Evaluation Accounting                            [G8] Performance Budget (p99<=100us)
[G4] Delivery & Runtime-to-Disk Reconciliation        [G9] SHA-256 Digest & Raw Integrity
[G5] Zero Decision Mismatch (mismatches=0)
       └──────────────────────────────┬──────────────────────────────┘
                                      ▼
                      AcceptanceReport.overall_status
                       ├── PASS       ==> Unlock Wave 1E
                       ├── FAIL       ==> Block Wave 1E
                       ├── INCOMPLETE ==> Block Wave 1E
                       └── INVALID    ==> Block Wave 1E
```

| 門禁 ID | 門禁名稱 | 判定公式 / 檢驗條件 | 要求門檻 | 失敗終態 |
| :--- | :--- | :--- | :---: | :---: |
| **G1** | **Baseline Provenance & Preflight** | `git_commit == expected_rc_commit` $\land$ `git_clean_status == True` $\land$ `authority == "legacy"` | `66d1e7cd` | `INVALID` |
| **G2** | **Runtime Non-Interference** | $\text{orders} = 0 \land \text{commits} = 0 \land \text{appends} = 0 \land \text{dup\_legacy} = 0 \land \text{dup\_shadow} = 0 \land \text{unclassified} = 0$ | 6 大計數器＝0 | `FAIL` |
| **G3** | **Evaluation Accounting** | $\text{cycles\_seen} = \text{matches} + \text{mismatches} + \dots + \text{context\_build\_failed}$ | 100% 完備 | `FAIL` |
| **G4** | **Delivery & Reconciliation** | $\text{enqueued} = \text{written} + \text{dropped} + \text{pending}$ $\land$ $\text{pending} = 0$ $\land$ $\text{runtime\_cycles} = \text{raw\_records} + \text{dropped}$ | 零未清 Queue | `FAIL` |
| **G5** | **Zero Decision Mismatch** | $\text{mismatches} = 0 \land \text{unexplained\_mismatches} = 0$ | 零豁免 (Zero Waiver) | `FAIL` |
| **G6** | **Minimum Session Coverage** | $\text{cycles} \ge 200 \land \text{day} \ge 100 \land \text{night} \ge 100 \land \text{lifecycles} \ge 5$ | 顯性分體門檻 | `INCOMPLETE` |
| **G7** | **Controlled Restart Continuity**| $\text{process\_segments} \ge 2 \land \text{restart\_reconciliations} \ge 1 \land \text{termination} == \text{CLEAN}$ | 需含重啟實證 | `INCOMPLETE` |
| **G8** | **Performance & Latency Budget** | $\text{shadow\_eval\_p99\_us} \le 100.0\mu s \land \text{queue\_overflow\_rate} = 0.0$ | $\le 100\mu s$ SLA | `FAIL` |
| **G9** | **SHA-256 Digest & Raw Integrity**| Python 純 Native Hash $\text{sha256}(\text{manifest.json}) == \text{manifest.sha256}$ | 跨平台無遺漏 | `INVALID` |

---

## 📁 3. 實證數據包歸檔目錄架構

當盤中觀察結束後，證據將歸檔至以下目錄：

```text
data/acceptance/wave1d3/20260724/
├── static-unit.xml                      # 靜態 97/97 門禁測試 JUnit XML 報告
├── soak-generation-reference.json       # Generation 磁碟指標與路徑對照
├── manifest.json                        # Soak Collector 產出之 Manifest
├── manifest.sha256                      # Manifest 之 SHA-256 摘要
├── independent-verifier-report.json     # 獨立驗收器 9 大門禁評估報告
├── independent-verifier-report.sha256   # 驗收報告之 SHA-256 摘要
├── runtime-provenance.json              # 包含 Commit, Config SHA, Authority 紀錄
└── promotion-decision.md                # 最終機械式晉級判定決策書
```

---

## 💻 4. 盤中一鍵獨立驗收指令

```bash
ssh myllin_mini@100.98.237.43 "cd ~/Documents/mylin102/tw-trading-unified-git && .venv/bin/python3 -c \"
from strategies.futures.mts.acceptance_verifier import IndependentAcceptanceVerifier

verifier = IndependentAcceptanceVerifier(
    generation_dir='data/telemetry/shadow-soak/generation-XXXX',
    expected_rc_commit='66d1e7cd',
    min_day_cycles=100,
    min_night_cycles=100,
    min_total_cycles=200,
    min_lifecycles=5
)

report = verifier.verify()
print(f'=== 獨立驗收結果: {report.overall_status} ===')
for g in report.gates:
    print(f'  [{g.status}] {g.gate_id} - {g.name}: {g.details}')
\""
```
