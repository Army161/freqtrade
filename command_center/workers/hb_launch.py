"""Launch Hummingbot headless with TCC compatibility shims, then hand over to quickstart.

Shim: V2 executors call ``connector.trading_rules[pair]``, which Hummingbot's Cython
``PaperTradeExchange`` does not implement (AttributeError on every executor). For connectors
without ``trading_rules`` we derive a permissive TradingRule from the connector's own price /
size quanta. Real exchange connectors are untouched.

Usage (cwd = Hummingbot home):  python hb_launch.py --headless -p PW --v2 conf.yml
"""

import runpy
import sys
from decimal import Decimal

sys.path.insert(0, ".")

from hummingbot.connector.trading_rule import TradingRule  # noqa: E402
from hummingbot.strategy_v2.executors import executor_base  # noqa: E402

_orig = executor_base.ExecutorBase.get_trading_rules


def _get_trading_rules(self, connector_name: str, trading_pair: str):
    conn = self.connectors[connector_name]
    if hasattr(conn, "trading_rules"):
        return _orig(self, connector_name, trading_pair)
    price = conn.get_price(trading_pair, True) or Decimal("1")
    return TradingRule(
        trading_pair,
        min_order_size=Decimal("0"),
        min_price_increment=conn.get_order_price_quantum(trading_pair, price),
        min_base_amount_increment=conn.get_order_size_quantum(trading_pair, Decimal("1")),
        min_notional_size=Decimal("1"),
    )


executor_base.ExecutorBase.get_trading_rules = _get_trading_rules

# quickstart imports bin/path_util.py; append (not prepend) so bin/hummingbot.py can't shadow the package
sys.path.append("bin")

sys.argv = ["hummingbot_quickstart.py", *sys.argv[1:]]
runpy.run_path("bin/hummingbot_quickstart.py", run_name="__main__")
