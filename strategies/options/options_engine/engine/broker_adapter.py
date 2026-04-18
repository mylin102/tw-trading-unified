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
            'STP': sj.constant.FuturesPriceType.STP,
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

    def check_margin(self, margin_required=20000.0):
        """檢查帳戶保證金是否足夠"""
        if self.api is None or not hasattr(self.api, "get_account_margin"):
            return True
        try:
            margin = self.api.get_account_margin()
            available = float(margin.equity)
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

    def place_exit_order(self, contract, quantity):
        # 出場通常不檢查保證金（平倉），但為保險起見保留邏輯
        order = self.build_option_order(
            action=sj.constant.Action.Sell,
            price=self.aggressive_exit_price(getattr(contract, "bid_price", 0.0) or 0.0),
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
