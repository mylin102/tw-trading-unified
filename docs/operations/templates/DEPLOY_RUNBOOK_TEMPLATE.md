# Deployment Runbook

## 前置條件

- 操作者、目標 branch、部署窗口與 rollback owner 已確認。

## 階段 1：驗證部署前條件

執行部署前檢查；若任一檢查回傳非 0，停止並回報 RC。

驗收：所有檢查 PASS，否則 FAIL。

## 階段 2：執行部署切換

執行既定部署命令並等待命令完成；非 0 RC 立即停止後續動作。

驗收：部署命令 RC=0，否則依 rollback 條件處理。

## 階段 3：驗證服務狀態

檢查服務健康、版本與關鍵流程；若健康檢查失敗，執行 rollback 判斷。

驗收：健康檢查 PASS 且版本符合預期。

### 〈產物〉

| 產物 | 何時產生 | 命令 | 落點 |
|---|---|---|---|
| deployment snapshot | 階段 1 執行時 | `<snapshot command>` | `artifacts/<timestamp>/snapshot.json` |
| gate evidence | 階段 1 驗收時 | `<gate command>` | `artifacts/<timestamp>/gate.json` |
| artifact hash | 產物寫入後 | `shasum -a 256 <path>` | `artifacts/<timestamp>/SHA256SUMS` |
| deployment report | 階段 3 完成時 | `<report command>` | `artifacts/<timestamp>/report.md` |

消費端新鮮度：snapshot/capture 與使用相隔超過 10 分鐘、session 改變或來源改變時，必須重抓；不得沿用舊產物。
