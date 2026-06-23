import pandas as pd

from strategies.futures.squeeze_futures.data.shioaji_client import ShioajiClient


class _Contract:
    def __init__(self, code):
        self.code = code


class _TxfNode:
    def __init__(self, contract):
        self.near_month = contract


class _Futures:
    def __init__(self, contract):
        self.TXF = _TxfNode(contract)


class _Contracts:
    def __init__(self, contract):
        self.Futures = _Futures(contract)


class _Api:
    def __init__(self, contract):
        self.Contracts = _Contracts(contract)
        self.kbars_calls = []

    def kbars(self, contract, start):
        self.kbars_calls.append((contract.code, start))
        return {
            "ts": pd.date_range("2026-04-21 09:00:00", periods=3, freq="1min"),
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, 101.5, 102.5],
            "Volume": [10, 12, 14],
        }


def test_get_futures_contract_accepts_tx_alias():
    client = ShioajiClient()
    client.is_logged_in = True
    client.api = _Api(_Contract("TXFG6"))

    contract = client.get_futures_contract("TX")

    assert contract is not None
    assert contract.code == "TXFG6"


def test_get_kline_uses_resolved_tx_alias_contract():
    client = ShioajiClient()
    client.is_logged_in = True
    client.api = _Api(_Contract("TXFG6"))

    df = client.get_kline("TX", interval="5m")

    assert not df.empty
    assert client.api.kbars_calls[0][0] == "TXFG6"
