from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import shioaji as sj

from strategies.options.options_engine.engine.broker_adapter import ShioajiBrokerAdapter


def _make_contract(code: str):
    return SimpleNamespace(
        security_type=sj.constant.SecurityType.Option,
        exchange=sj.constant.Exchange.TAIFEX,
        code=code,
        symbol=code,
        name=code,
        category="TXO",
        currency=sj.constant.Currency.TWD,
        delivery_month="202504",
        delivery_date="2025/04/30",
        strike_price=22000,
        option_right=sj.constant.OptionRight.Call,
        underlying_kind="I",
        underlying_code="TXO",
        unit=1,
        multiplier=50,
        limit_up=100.0,
        limit_down=1.0,
        reference=10.0,
        update_date="2025/04/21",
    )


def test_place_combo_entry_order_builds_two_leg_combo():
    api = MagicMock()
    api.futopt_account = object()
    combo_trade = SimpleNamespace(status=SimpleNamespace(status="Submitted"))
    api.place_comboorder.return_value = combo_trade

    adapter = ShioajiBrokerAdapter(api, execution_cfg={"futures_order_type": "IOC"})
    legs = [
        {"contract": _make_contract("TXO22000C"), "action": sj.constant.Action.Sell},
        {"contract": _make_contract("TXO22100C"), "action": sj.constant.Action.Buy},
    ]

    with patch("strategies.options.options_engine.engine.broker_adapter.sj.contracts.ComboBase") as combo_base_cls, \
         patch("strategies.options.options_engine.engine.broker_adapter.sj.contracts.ComboContract") as combo_contract_cls, \
         patch("strategies.options.options_engine.engine.broker_adapter.sj.order.ComboOrder") as combo_order_cls:
        leg_one = SimpleNamespace(code="LEG-1")
        leg_two = SimpleNamespace(code="LEG-2")
        combo_base_cls.side_effect = [leg_one, leg_two]
        combo_contract = SimpleNamespace(legs=["LEG-1", "LEG-2"])
        combo_order = SimpleNamespace(id="combo-order")
        combo_contract_cls.return_value = combo_contract
        combo_order_cls.return_value = combo_order

        trade = adapter.place_comboorder(
            legs,
            price=12.5,
            quantity=1,
            action=sj.constant.Action.Sell,
        )

    assert trade is combo_trade
    assert combo_base_cls.call_count == 2
    combo_contract_cls.assert_called_once_with(legs=[leg_one, leg_two])
    combo_order_cls.assert_called_once()
    api.place_comboorder.assert_called_once_with(combo_contract, combo_order)
    api.place_order.assert_not_called()


@pytest.mark.parametrize("leg_count", [1, 3])
def test_place_comboorder_rejects_non_two_legs(leg_count):
    api = MagicMock()
    api.futopt_account = object()
    adapter = ShioajiBrokerAdapter(api)

    legs = [
        {"contract": _make_contract(f"TXO22{i}00C"), "action": sj.constant.Action.Buy}
        for i in range(leg_count)
    ]

    with pytest.raises(ValueError, match="two legs"):
        adapter.place_comboorder(
            legs,
            price=10.0,
            quantity=1,
            action=sj.constant.Action.Buy,
        )


def test_combo_helper_methods_delegate_to_api():
    api = MagicMock()
    api.futopt_account = object()
    adapter = ShioajiBrokerAdapter(api)
    combo_trade = SimpleNamespace(ordno="COMBO-001")

    adapter.cancel_comboorder(combo_trade)
    adapter.update_combostatus(account="acct")
    adapter.list_combotrades()

    api.cancel_comboorder.assert_called_once_with(combo_trade)
    api.update_combostatus.assert_called_once_with(account="acct")
    api.list_combotrades.assert_called_once_with()
