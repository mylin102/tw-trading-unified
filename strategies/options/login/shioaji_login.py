import json
import threading
import os
import time
import shioaji as sj
from dotenv import load_dotenv

_api_instance = None
_api_lock = threading.Lock()

def logout():
    global _api_instance
    with _api_lock:
        if _api_instance is not None:
            try:
                _api_instance.logout()
            except Exception:
                pass
            _api_instance = None

def login():
    global _api_instance
    with _api_lock:
        if _api_instance is not None:
            return _api_instance
        _api_instance = _do_login()
        return _api_instance

def _do_login():
    """
    Login with account info from environment variables or 'account_info.json'.
    :return: shioaji api.
    """
    # 優先載入 .env 檔案
    load_dotenv(override=True)

    account_info_path = 'login/account/account_info.json'
    account_data = {}
    
    # 嘗試從環境變數讀取
    user_id = os.getenv('SHIOAJI_API_KEY') or os.getenv('SHIOAJI_PERSON_ID')
    password = os.getenv('SHIOAJI_SECRET_KEY') or os.getenv('SHIOAJI_PASSWD')
    ca_path_base = os.getenv('SHIOAJI_CA_PATH')
    ca_name = os.getenv('SHIOAJI_CA_NAME')
    ca_passwd = os.getenv('SHIOAJI_CA_PASSWD')

    # 如果環境變數不完整，嘗試從 JSON 讀取備援
    if not (user_id and password) and os.path.exists(account_info_path):
        print(f"環境變數不完整，嘗試從 {account_info_path} 讀取備援...")
        try:
            with open(account_info_path, newline='') as jsonfile:
                account_data = json.load(jsonfile)
                user_id = user_id or account_data.get('API_KEY') or account_data.get('person_id')
                password = password or account_data.get('SECRET_KEY') or account_data.get('passwd')
                ca_path_base = ca_path_base or account_data.get('ca_path')
                ca_name = ca_name or account_data.get('ca_name')
                ca_passwd = ca_passwd or account_data.get('ca_passwd')
        except Exception as e:
            print(f"讀取 JSON 備援時發生錯誤: {e}")

    if not user_id:
        raise KeyError("找不到 SHIOAJI_API_KEY (或 person_id)，請檢查環境變數或 account_info.json")

    ca_full_path = ""
    if ca_path_base and ca_name:
        ca_full_path = os.path.join(ca_path_base, ca_name)

    api = sj.Shioaji()

    for attempt in range(1, 4):
        try:
            if len(user_id) > 15:
                print("檢測到 API Key 模式...")
                api_login = api.login(api_key=user_id, secret_key=password, contracts_timeout=10000)
            else:
                print("檢測到身分證號模式...")
                api_login = api.login(user_id, password, contracts_timeout=10000)
            print(f'Login status: {api_login}')
            break
        except Exception as e:
            err = str(e)
            if 'Too Many Connections' in err and attempt < 3:
                wait = attempt * 30
                print(f"Too Many Connections，等待 {wait} 秒後重試 ({attempt}/3)...")
                time.sleep(wait)
            else:
                raise
    
    # 憑證啟用 (模擬交易不需要憑證)
    person_id_for_ca = os.getenv('SHIOAJI_PERSON_ID') or account_data.get('person_id') or user_id
    try:
        if ca_full_path and os.path.exists(ca_full_path):
            activate = api.activate_ca(ca_path=ca_full_path, ca_passwd=ca_passwd, person_id=person_id_for_ca)
            if(activate):
                print(f'成功啟用憑證: {ca_full_path}')
            else:
                print('憑證啟用失敗，目前僅支援模擬交易/行情監控。')
        else:
            print(f'找不到憑證檔案 {ca_full_path}，將進入行情監控模式 (不支援下單)。')
    except Exception as e:
        print(f'憑證啟用過程發生錯誤: {e}，切換至監控模式。')
    
    return api