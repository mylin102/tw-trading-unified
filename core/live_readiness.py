import sys
import logging
from datetime import datetime
import os

logger = logging.getLogger("Readiness")

def check_contract_sanity(contract):
    """檢查合約是否為預期的台指/微台"""
    if contract is None:
        return False, "Contract is None"
    
    code = getattr(contract, "code", "")
    # 預防誤訂閱道瓊期 (BRF/UDF)
    if any(x in code for x in ["BRF", "UDF", "SPF"]):
        return False, f"Detected non-TAIEX contract: {code}"
    
    return True, "OK"

def check_env_vars():
    """檢查關鍵環境變數"""
    keys = ["SHIOAJI_API_KEY", "SHIOAJI_PERSON_ID"]
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        return False, f"Missing env vars: {missing}"
    return True, "OK"
from types import SimpleNamespace

def check_all():
    """執行所有就緒檢查，返回 (is_ready, results_dict)"""
    results = {}

    # 1. 環境變數
    env_ok, env_msg = check_env_vars()
    results["Environment"] = SimpleNamespace(passed=env_ok, message=env_msg)

    # 2. 檔案目錄
    dirs = ["logs", "logs/market_data", "config"]
    missing_dirs = [d for d in dirs if not os.path.exists(d)]
    dir_ok = len(missing_dirs) == 0
    dir_msg = "OK" if dir_ok else f"Missing: {missing_dirs}"
    results["Directories"] = SimpleNamespace(passed=dir_ok, message=dir_msg)

    is_ready = all(r.passed for r in results.values())
    return is_ready, results

def get_readiness_items(check_output):
    """
    Normalize readiness output for UI rendering.
    Returns a list of objects with `name`, `passed`, and `detail`.
    """
    if isinstance(check_output, tuple):
        _, results = check_output
    else:
        results = check_output

    return [
        SimpleNamespace(
            name=name,
            passed=bool(getattr(result, "passed", False)),
            detail=getattr(result, "message", ""),
        )
        for name, result in results.items()
    ]

def get_readiness_summary(check_output):
    """
    符合 dashboard.py 預期的簽章
    傳入: check_all() 得到的 (is_ready, results) 元組 或 results 字典
    回傳: (status_text, passed_count, total_count)
    """
    if isinstance(check_output, tuple):
        _, results = check_output
    else:
        results = check_output

    total = len(results)
    passed = sum(1 for r in results.values() if getattr(r, "passed", False))

    if passed == total:
        status = "READY"
    elif passed > 0:
        status = "DEGRADED"
    else:
        status = "CRITICAL"

    return status, passed, total
