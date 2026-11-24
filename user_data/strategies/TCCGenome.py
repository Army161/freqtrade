# pragma pylint: disable=missing-docstring
"""Universal Freqtrade strategy for the Trading Command Center (TCC).

One strategy class executes ANY genome produced by the TCC research engine. The genome is
passed via the bot config key ``tcc_genome`` (see command_center/tcc/research/genome.py),
so thousands of variants can be backtested / deployed without generating thousands of files.
Signals come from the same module the Stage-1 screener uses (tcc.research.indicators).

ROI / stoploss / trailing / timeframe are written into the bot config by TCC
(config values override the class defaults below).
"""

from datetime import datetime, timedelta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

from tcc.research.indicators import compute_signals

TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}


class TCCGenome(IStrategy):
    INTERFACE_VERSION = 3
    can_short = False
    timeframe = "5m"
    minimal_roi = {"0": 0.02}
    stoploss = -0.03
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    process_only_new_candles = True
    startup_candle_count = 300

    def bot_start(self, **kwargs) -> None:
        g = self.config.get("tcc_genome")
        if not g:
            raise ValueError("TCCGenome requires 'tcc_genome' in the config")
        self.genome = g
        self.max_hold = timedelta(minutes=TF_MIN[g["timeframe"]] * int(g["exit"]["hold"]))

    @property
    def protections(self):
        # Freqtrade-native protections, generated per bot by TCC (cooldown, stoploss guard,
        # max drawdown, low-profit pairs). Only active when enable_protections is set.
        return self.config.get("tcc_protections", [])

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        entry, exit_ = compute_signals(dataframe, self.genome)
        dataframe["tcc_entry"] = entry.astype(int)
        dataframe["tcc_exit"] = exit_.astype(int)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["tcc_entry"] == 1, ["enter_long", "enter_tag"]] = (
            1, self.genome["family"])
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["tcc_exit"] == 1, "exit_long"] = 1
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        if current_time - trade.open_date_utc >= self.max_hold:
            return "tcc_time_limit"
        return None
