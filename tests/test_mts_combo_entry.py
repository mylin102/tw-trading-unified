from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _fake_sdk():
    class _Action:
        Buy = "Buy"
        Sell = "Sell"

    class _ComboBase:
        @classmethod
        def from_contract(cls, contract, action):
            return SimpleNamespace(code=contract.code, action=action)

    class _ComboContract:
        def __init__(self, legs):
            self.legs = legs

    class _ComboOrder:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    return SimpleNamespace(
        Action=_Action,
        FuturesPriceType=SimpleNamespace(LMT="LMT"),
        OrderType=SimpleNamespace(IOC="IOC"),
        FuturesOCType=SimpleNamespace(Auto="Auto"),
        contracts=SimpleNamespace(ComboBase=_ComboBase,
                                  ComboContract=_ComboContract),
        ComboOrder=_ComboOrder,
    )


def test_futures_adapter_places_one_two_leg_combo(monkeypatch):
    from strategies.futures.squeeze_futures.data import shioaji_client as mod

    fake = _fake_sdk()
    monkeypatch.setattr(mod, "sj", fake)
    client = mod.ShioajiClient.__new__(mod.ShioajiClient)
    client.api = SimpleNamespace(
        futopt_account="acct",
        place_comboorder=MagicMock(return_value=SimpleNamespace(
            id="BROKER-COMBO-1", seqno="SEQ-1", ordno="ORD-1")),
    )
    client.is_logged_in = True
    client._gate_or_raise = MagicMock()
    contracts = {code: SimpleNamespace(code=code) for code in ("TMF1", "TMF2")}
    client.get_contract = lambda code: contracts.get(code)

    order = SimpleNamespace(
        price=12.0, quantity=1,
        combo_legs=[
            {"symbol": "TMF1", "side": "buy", "quantity": 1},
            {"symbol": "TMF2", "side": "sell", "quantity": 1},
        ],
    )
    receipt = client.place_combo_order_object(order)

    assert receipt.id == "BROKER-COMBO-1"
    client._gate_or_raise.assert_called_once()
    client.api.place_comboorder.assert_called_once()
    combo_contract, combo_order = client.api.place_comboorder.call_args.args
    assert [leg.code for leg in combo_contract.legs] == ["TMF1", "TMF2"]
    assert combo_order.price == 12.0
    assert combo_order.order_type == "IOC"


def test_futures_adapter_rejects_non_two_leg_combo(monkeypatch):
    from strategies.futures.squeeze_futures.data import shioaji_client as mod

    monkeypatch.setattr(mod, "sj", _fake_sdk())
    client = mod.ShioajiClient.__new__(mod.ShioajiClient)
    client.api = SimpleNamespace(futopt_account="acct")
    client.is_logged_in = True
    client._gate_or_raise = MagicMock()
    with pytest.raises(mod.AdapterOrderError) as exc:
        client.place_combo_order_object(SimpleNamespace(combo_legs=[]))
    assert exc.value.code == "ADAPTER_COMBO_LEGS_INVALID"


def test_order_manager_routes_combo_to_combo_adapter():
    from core.order_management.order import OrderSide, OrderType
    from core.order_management.order_manager import OrderManager

    adapter = MagicMock()
    adapter.place_combo_order_object.return_value = SimpleNamespace(
        id="COMBO-ID", seqno="COMBO-SEQ", ordno="COMBO-ORD")
    manager = OrderManager(mode="live", broker_adapter=adapter)
    order = manager.create_order(
        symbol="TMF1-TMF2", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, price=12.0, strategy="MTS_ENTRY",
        combo_legs=[
            {"symbol": "TMF1", "side": "buy", "quantity": 1},
            {"symbol": "TMF2", "side": "sell", "quantity": 1},
        ], combo_strategy="TIME_SPREAD")
    assert manager.submit(order) is True
    adapter.place_combo_order_object.assert_called_once_with(order)
    adapter.place_order_object.assert_not_called()
