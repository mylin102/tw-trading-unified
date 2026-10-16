# Broker truth reconciliation

券商唯讀查詢是部位真相來源，但單次空回應不是 flat 證據。現行
`live_broker_preflight` 的 `POSITIONS_EMPTY_AFTER_PRIOR_HOLDING` 防護保留不變：
它仍會把第一次「先前有倉、這次空」標成 fail-closed。

`core.broker_truth_reconciler.BrokerTruthConsensus` 補上後半段：同一部位結果
必須達到預設至少 2 次連續觀測，且觀測間隔達到設定門檻，才會 atomic 更新
local state。中間出現相反結果會重置計數並留下 `BROKER_TRUTH_FLAP` audit。

收斂 state 會標記 `reconciled_from=broker_query_consensus`，保存每次 capture
的 `canonical_input_hash`、`captured_at` 與 capture path；capture unavailable
或有 working orders 時不收斂。

P0：共識完成前，受影響 leg 的 `exit_allowed()` 必須為 false；因此這層不能
被當成 exit 授權來源，只有 broker truth 已穩定且 state 已寫入後才可讓上層
繼續判斷。這是補上單次空防護的自動收斂路徑，不是移除原有防護。
