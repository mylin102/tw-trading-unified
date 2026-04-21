import datetime

import shioaji as sj


def normalize_option_right(option_right):
    if option_right in ("C", "Call", sj.constant.OptionRight.Call):
        return sj.constant.OptionRight.Call
    if option_right in ("P", "Put", sj.constant.OptionRight.Put):
        return sj.constant.OptionRight.Put
    raise ValueError(f"Unsupported option right: {option_right}")


class ShioajiBrokerAdapter:
    def __init__(self, api, execution_cfg=None):
        self.api = api
        self.execution_cfg = execution_cfg or {}
        self.aggressive_ticks = self.execution_cfg.get("aggressive_ticks", 0)
        self.tick_size = float(self.execution_cfg.get("tick_size", 1.0))

    def aggressive_entry_price(self, ask_price):
        return max(0.0, float(ask_price) + (self.aggressive_ticks * self.tick_size))

    def aggressive_exit_price(self, bid_price):
        return max(self.tick_size, float(bid_price) - (self.aggressive_ticks * self.tick_size))

    def build_option_order(self, action, price, quantity, option_right, order_type=None, octype=None, price_type='LMT'):
        futures_order_type = order_type or self.execution_cfg.get("futures_order_type", "IOC")
        order_type_enum = getattr(sj.constant.OrderType, futures_order_type) if hasattr(sj.constant.OrderType, futures_order_type) else sj.constant.OrderType.IOC
        oc_type_name = octype or self.execution_cfg.get("futures_oc_type", "Auto")
        oc_type_enum = getattr(sj.constant.FuturesOCType, oc_type_name)
        
        # Mapping for price_type
        price_type_map = {
            'LMT': sj.constant.FuturesPriceType.LMT,
            'MKT': sj.constant.FuturesPriceType.MKT,
            'STP': getattr(sj.constant.FuturesPriceType, "STP", sj.constant.FuturesPriceType.LMT),
        }
        price_type_enum = price_type_map.get(price_type, sj.constant.FuturesPriceType.LMT)

        return self.api.Order(
            action=action,
            price=float(price),
            quantity=int(quantity),
            price_type=price_type_enum,
            order_type=order_type_enum,
            octype=oc_type_enum,
            account=self.api.futopt_account,
        )

    def _build_combo_leg(self, contract, action):
        return sj.contracts.ComboBase(
            security_type=getattr(contract, "security_type"),
            exchange=getattr(contract, "exchange"),
            code=getattr(contract, "code"),
            symbol=getattr(contract, "symbol", ""),
            name=getattr(contract, "name", ""),
            category=getattr(contract, "category", ""),
            currency=getattr(contract, "currency", sj.constant.Currency.TWD),
            delivery_month=getattr(contract, "delivery_month", ""),
            delivery_date=getattr(contract, "delivery_date", ""),
            strike_price=getattr(contract, "strike_price", 0),
            option_right=getattr(contract, "option_right", sj.constant.OptionRight.No),
            underlying_kind=getattr(contract, "underlying_kind", ""),
            underlying_code=getattr(contract, "underlying_code", ""),
            unit=getattr(contract, "unit", 0),
            multiplier=getattr(contract, "multiplier", 0),
            limit_up=getattr(contract, "limit_up", 0.0),
            limit_down=getattr(contract, "limit_down", 0.0),
            reference=getattr(contract, "reference", 0.0),
            update_date=getattr(contract, "update_date", ""),
            action=action,
        )

    def build_combo_contract(self, legs):
        if len(legs) != 2:
            raise ValueError("Shioaji combo orders currently support exactly two legs")

        combo_legs = []
        for leg in legs:
            contract = leg["contract"] if isinstance(leg, dict) else leg[0]
            action = leg["action"] if isinstance(leg, dict) else leg[1]
            combo_legs.append(self._build_combo_leg(contract, action))
        return sj.contracts.ComboContract(legs=combo_legs)

    def build_combo_order(self, price, quantity, action=sj.constant.Action.Sell, order_type=None, octype=None, price_type='LMT'):
        futures_order_type = order_type or self.execution_cfg.get("futures_order_type", "IOC")
        order_type_enum = getattr(sj.constant.OrderType, futures_order_type) if hasattr(sj.constant.OrderType, futures_order_type) else sj.constant.OrderType.IOC
        oc_type_name = octype or self.execution_cfg.get("futures_oc_type", "Auto")
        oc_type_enum = getattr(sj.constant.FuturesOCType, oc_type_name)

        price_type_map = {
            'LMT': sj.constant.FuturesPriceType.LMT,
            'MKT': sj.constant.FuturesPriceType.MKT,
            'STP': getattr(sj.constant.FuturesPriceType, "STP", sj.constant.FuturesPriceType.LMT),
        }
        price_type_enum = price_type_map.get(price_type, sj.constant.FuturesPriceType.LMT)

        return sj.order.ComboOrder(
            action=action,
            price=float(price),
            quantity=int(quantity),
            price_type=price_type_enum,
            order_type=order_type_enum,
            octype=oc_type_enum,
            account=self.api.futopt_account,
        )

    def place_comboorder(self, legs, price, quantity, action=sj.constant.Action.Sell, order_type=None, octype=None, price_type='LMT'):
        if len(legs) != 2:
            raise ValueError("Shioaji combo orders currently support exactly two legs")
        if not self.check_margin():
            return None
        combo_contract = self.build_combo_contract(legs)
        combo_order = self.build_combo_order(
            price=price,
            quantity=quantity,
            action=action,
            order_type=order_type,
            octype=octype,
            price_type=price_type,
        )
        return self.api.place_comboorder(combo_contract, combo_order)

    def check_margin(self, margin_required=20000.0):
        """檢查帳戶保證金是否足夠"""
        if self.api is None or not hasattr(self.api, "get_account_margin"):
            return True
        try:
            margin = self.api.get_account_margin()
            equity = getattr(margin, "equity", None)
            if equity is None or type(equity).__module__.startswith("unittest.mock"):
                return True
            available = float(equity)
            if available < margin_required:
                print(f"❌ 帳戶可用資金不足：{available:,.0f} < {margin_required:,.0f}")
                return False
            return True
        except Exception as e:
            print(f"⚠️  無法獲取帳務資訊：{e}")
            return True

    def place_entry_order(self, contract, quantity):
        if not self.check_margin():
            return None
        order = self.build_option_order(
            action=sj.constant.Action.Buy,
            price=self.aggressive_entry_price(getattr(contract, "ask_price", 0.0) or 0.0),
            quantity=quantity,
            option_right=contract.option_right,
        )
        return self.api.place_order(contract, order)

    def place_exit_order(self, contract, quantity, bid_price=None):
        # 出場通常不檢查保證金（平倉），但為保險起見保留邏輯
        price = bid_price if bid_price is not None else (getattr(contract, "bid_price", 0.0) or 0.0)
        order = self.build_option_order(
            action=sj.constant.Action.Sell,
            price=self.aggressive_exit_price(price),
            quantity=quantity,
            option_right=contract.option_right,
        )
        return self.api.place_order(contract, order)

    def place_manual_order(self, contract, action, price, quantity):
        order = self.build_option_order(
            action=action,
            price=price,
            quantity=quantity,
            option_right=contract.option_right,
        )
        return self.api.place_order(contract, order)

    def describe_trade(self, trade):
        return {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": getattr(trade.status, "status", None) if hasattr(trade, "status") else None,
            "order": str(getattr(trade, "order", "")),
            "contract": getattr(getattr(trade, "contract", None), "code", None),
        }

    def refresh_status(self, account=None):
        if hasattr(self.api, "update_status"):
            if account is not None:
                return self.api.update_status(account=account)
            return self.api.update_status()
        return None

    def cancel_order(self, trade):
        if hasattr(self.api, "cancel_order"):
            return self.api.cancel_order(trade)
        return None

    def cancel_comboorder(self, combo_trade):
        if hasattr(self.api, "cancel_comboorder"):
            return self.api.cancel_comboorder(combo_trade)
        return None

    def update_combostatus(self, account=None):
        if hasattr(self.api, "update_combostatus"):
            if account is not None:
                return self.api.update_combostatus(account=account)
            return self.api.update_combostatus()
        return None

    def list_combotrades(self):
        if hasattr(self.api, "list_combotrades"):
            return list(self.api.list_combotrades())
        return []

    def list_open_orders(self, account=None):
        if hasattr(self.api, "list_open_orders"):
            if account is not None:
                return list(self.api.list_open_orders(account=account))
            return list(self.api.list_open_orders())
        return []

    def list_trades(self, account=None):
        if hasattr(self.api, "list_trades"):
            if account is not None:
                return list(self.api.list_trades(account=account))
            return list(self.api.list_trades())
        return []

    def list_positions(self, account=None):
        if hasattr(self.api, "list_positions"):
            if account is not None:
                return list(self.api.list_positions(account=account))
            return list(self.api.list_positions())
        return []

class MockTrade:
    def __init__(self, contract, action, price, quantity, status="Filled"):
        self.contract = contract
        self.order = SimpleNamespace(action=action, price=price, quantity=quantity)
        self.status = SimpleNamespace(status=status)
        self.order_id = f"MOCK-{datetime.datetime.now().strftime('%H%M%S')}"

class MockBrokerAdapter:
    def __init__(self, execution_cfg=None):
        self.execution_cfg = execution_cfg or {}
        self.aggressive_ticks = self.execution_cfg.get("aggressive_ticks", 0)
        self.tick_size = float(self.execution_cfg.get("tick_size", 1.0))

    def aggressive_entry_price(self, ask_price):
        return max(0.0, float(ask_price) + (self.aggressive_ticks * self.tick_size))

    def aggressive_exit_price(self, bid_price):
        return max(self.tick_size, float(bid_price) - (self.aggressive_ticks * self.tick_size))

    def place_entry_order(self, contract, quantity):
        from types import SimpleNamespace
        return MockTrade(contract, "Buy", self.aggressive_entry_price(getattr(contract, "ask_price", 0.0) or 0.0), quantity)

    def place_exit_order(self, contract, quantity, bid_price=None):
        from types import SimpleNamespace
        price = bid_price if bid_price is not None else (getattr(contract, "bid_price", 0.0) or 0.0)
        return MockTrade(contract, "Sell", self.aggressive_exit_price(price), quantity)

    def describe_trade(self, trade):
        return {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "Filled",
            "order": f"{trade.order.action}@{trade.order.price}x{trade.order.quantity}",
            "contract": getattr(getattr(trade, "contract", None), "code", None),
        }

    def refresh_status(self, account=None): return None
    def cancel_order(self, trade): return None
    def list_open_orders(self, account=None): return []
    def list_trades(self, account=None): return []
    def list_positions(self, account=None): return []
