"""
NFI + AI veto wrapper.

Inherits from NostalgiaForInfinityX7 and adds an LLM review layer.
The LLM reads higher-timeframe price action and can only veto entries.
All original NFI logic stays intact.
"""

import logging
import os
from datetime import datetime
from typing import Optional

import numpy as np
import talib.abstract as ta
from pandas import DataFrame
from freqtrade.persistence import Trade

from NostalgiaForInfinityX7 import NostalgiaForInfinityX7
from llm_reviewer import LLMReviewer

logger = logging.getLogger(__name__)


class NFIWithVeto(NostalgiaForInfinityX7):
    def version(self) -> str:
        nfi_version = super().version()
        return f"{nfi_version}-veto.2"

    def bot_start(self, **kwargs) -> None:
        super().bot_start(**kwargs)

        self.reviewer = LLMReviewer(
            base_url=os.environ.get("LLM_BASE_URL", "http://localhost:8317/v1"),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", "gpt-5.4-mini"),
            reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", "medium"),
            timeout=float(os.environ.get("LLM_TIMEOUT", "20")),
            cooldown_seconds=int(os.environ.get("LLM_COOLDOWN", "60")),
            max_completion_tokens=int(os.environ.get("LLM_MAX_TOKENS", "16384")),
        )

        logger.info(
            "NFIWithVeto initialized - model=%s effort=%s base_url=%s timeout=%ss "
            "cooldown=%ss max_tokens=%s fail_mode=open",
            self.reviewer.model,
            self.reviewer.reasoning_effort,
            self.reviewer.base_url,
            self.reviewer.timeout,
            self.reviewer.cooldown_seconds,
            self.reviewer.max_completion_tokens,
        )

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:
        nfi_ok = super().confirm_trade_entry(
            pair,
            order_type,
            amount,
            rate,
            time_in_force,
            current_time,
            entry_tag,
            side,
            **kwargs,
        )
        if not nfi_ok:
            return False

        # Manual force entries are an explicit user action. NFI passes them
        # through unconditionally, so the AI veto must not block them either.
        if entry_tag == "force_entry":
            return True

        candles_1w = self._extract_candles(pair, "1w", count=12)
        candles_1d = self._extract_candles(pair, "1d", count=30)
        candles_4h = self._extract_candles(pair, "4h", count=20)
        candles_1h = self._extract_candles(pair, "1h", count=24)

        try:
            recent_trades = Trade.get_trades_proxy(pair=pair, is_open=False)
            recent_trades = sorted(recent_trades, key=lambda t: t.close_date or datetime.min)[-10:]
        except Exception:
            recent_trades = []

        position_state = self._get_position_state(pair, current_time, rate)
        btc_change, btc_trend, btc_candles = self._get_btc_context()
        entry_mode, dca_profile = self._resolve_entry_context(entry_tag or "unknown", side)

        try:
            open_count = Trade.get_open_trade_count()
            max_trades = self.config.get("max_open_trades", 0)
            slot_info = f"{open_count}/{max_trades}" if max_trades else f"{open_count}/?"
        except Exception:
            slot_info = None

        verdict = self.reviewer.review(
            pair=pair,
            entry_tag=entry_tag or "unknown",
            entry_mode=entry_mode,
            dca_profile=dca_profile,
            rate=rate,
            side=side,
            recent_trades=recent_trades,
            candles_1w=candles_1w,
            candles_1d=candles_1d,
            candles_4h=candles_4h,
            candles_1h=candles_1h,
            btc_change_pct=btc_change,
            btc_trend=btc_trend,
            btc_candles=btc_candles,
            position_state=position_state,
            open_slots=slot_info,
        )

        if verdict["verdict"] == "veto":
            logger.info(
                "[LLM VETO] %s %s tag=%s mode=%s dca=%s @ %s - confidence=%.2f - %s",
                pair,
                side,
                entry_tag,
                entry_mode,
                dca_profile,
                rate,
                verdict["confidence"],
                verdict["rationale"],
            )
            return False

        return True

    def _extract_candles(self, pair: str, timeframe: str, count: int) -> list[dict]:
        """Extract recent OHLCV plus key indicators from one timeframe.

        NFI merges its higher-timeframe data into the 5m dataframe via
        ``merge_informative_pair`` and never stores a separate analyzed
        dataframe for 1d/4h/1h. It also drops the higher-timeframe OHLCV
        during that merge, so ``get_analyzed_dataframe(pair, "1d")`` would
        return an empty frame here. We therefore pull raw OHLCV through
        ``get_pair_dataframe`` (live in dry/live, cached on disk in backtest)
        and recompute the same indicators NFI uses, so the reviewer sees real
        candles instead of "no data available".
        """
        try:
            df = self.dp.get_pair_dataframe(pair=pair, timeframe=timeframe)
            if df is None or df.empty:
                return []

            df = self._add_review_indicators(df)

            candles = []
            for _, row in df.tail(count).iterrows():
                candle = {
                    "date": str(row.get("date", ""))[:16],
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)),
                }

                for out_name in ("rsi_14", "rsi_3", "mfi_14", "change_pct", "atr_pct"):
                    value = row.get(out_name)
                    if value is not None and not (isinstance(value, float) and np.isnan(value)):
                        candle[out_name] = float(value)

                candles.append(candle)

            return candles
        except Exception as exc:
            logger.debug("Failed to extract %s candles for %s: %s", timeframe, pair, exc)
            return []

    @staticmethod
    def _add_review_indicators(df: DataFrame) -> DataFrame:
        """Recompute the indicators the reviewer expects, matching NFI formulas.

        NFI computes these from raw OHLCV with talib (see informative_*_indicators):
        RSI_3, RSI_14, MFI_14 and the per-candle change_pct.
        """
        close_np = df["close"].to_numpy(copy=False)
        high_np = df["high"].to_numpy(copy=False)
        low_np = df["low"].to_numpy(copy=False)
        open_np = df["open"].to_numpy(copy=False)
        volume_np = df["volume"].to_numpy(copy=False)

        df = df.copy()
        df["rsi_3"] = ta.RSI(close_np, timeperiod=3)
        df["rsi_14"] = ta.RSI(close_np, timeperiod=14)
        df["mfi_14"] = ta.MFI(high_np, low_np, close_np, volume_np, timeperiod=14)
        open_safe = np.where(open_np == 0, np.nan, open_np)
        df["change_pct"] = ((close_np - open_np) / open_safe) * 100.0
        # ATR as a percentage of price, so the reviewer can tell whether a move
        # is normal volatility or a structural break rather than reading raw ATR.
        atr = ta.ATR(high_np, low_np, close_np, timeperiod=14)
        close_safe = np.where(close_np == 0, np.nan, close_np)
        df["atr_pct"] = (atr / close_safe) * 100.0
        return df

    def _tags_all(self, tags: list[str], allowed: list[str]) -> bool:
        return bool(tags) and all(tag in allowed for tag in tags)

    def _tags_combo(self, tags: list[str], primary: list[str], combined_allowed: list[str]) -> bool:
        return bool(tags) and any(tag in primary for tag in tags) and all(tag in combined_allowed for tag in tags)

    def _tags_any(self, tags: list[str], allowed: list[str]) -> bool:
        return any(tag in allowed for tag in tags)

    def _uses_v3_system(self) -> bool:
        """All current NFI systems (v3 / v3_1 / v3_2) route non-grind modes into
        the grinding adjust path, so they average down too. Older non-v3 behavior
        only existed for trades opened before 2025-02-13."""
        try:
            return self.system_name_use in (
                self.system_v3_name,
                self.system_v3_1_name,
                self.system_v3_2_name,
            )
        except Exception:
            return True

    def _resolve_dca_profile(self, tags: list[str], aggressive_tags: list[str], light_tags: list[str]) -> str:
        """Map NFI entry tags to a DCA aggressiveness label for the reviewer.

        Reality check against NostalgiaForInfinityX7.adjust_trade_position:
        under the v3 systems, every non-grind/non-btc mode (normal, pump, quick,
        rapid, scalp, top_coins, high_profit) is routed into the grind adjust
        path and DOES average down. Only grind/btc/rebuy use the dedicated heavy
        ladders. So "minimal_dca" is only honest for the lighter rapid/scalp
        modes, and even those still grind under v3 - hence moderate, not minimal.
        """
        if self._tags_any(tags, aggressive_tags):
            return "aggressive_dca"
        if self._uses_v3_system():
            # Under v3 even rapid/scalp grind, just with lighter stake ladders.
            if self._tags_any(tags, light_tags):
                return "minimal_dca"
            return "moderate_dca"
        # Legacy (pre-v3) behavior: rapid/scalp barely averaged.
        if self._tags_any(tags, light_tags):
            return "minimal_dca"
        return "moderate_dca"

    def _resolve_entry_context(self, entry_tag: str, side: str) -> tuple[str, str]:
        tags = entry_tag.split()
        if side == "short":
            return self._resolve_short_entry_context(tags, entry_tag)
        return self._resolve_long_entry_context(tags, entry_tag)

    def _resolve_long_entry_context(self, tags: list[str], entry_tag: str) -> tuple[str, str]:
        if self._tags_all(tags, self.long_grind_mode_tags):
            mode = "long_grind"
        elif self._tags_combo(tags, self.long_rebuy_mode_tags, self.long_rebuy_mode_tags + self.long_grind_mode_tags):
            mode = "long_rebuy"
        elif self._tags_combo(
            tags,
            self.long_rapid_mode_tags,
            self.long_rapid_mode_tags + self.long_rebuy_mode_tags + self.long_grind_mode_tags + self.long_scalp_mode_tags,
        ):
            mode = "long_rapid"
        elif self._tags_combo(
            tags,
            self.long_scalp_mode_tags,
            self.long_scalp_mode_tags + self.long_rebuy_mode_tags + self.long_grind_mode_tags,
        ):
            mode = "long_scalp"
        elif self._tags_all(tags, self.long_btc_mode_tags):
            mode = "long_btc"
        elif self._tags_any(tags, self.long_top_coins_mode_tags):
            mode = "long_top_coins"
        elif self._tags_any(tags, self.long_high_profit_mode_tags):
            mode = "long_high_profit"
        elif self._tags_any(tags, self.long_pump_mode_tags):
            mode = "long_pump"
        elif self._tags_any(tags, self.long_quick_mode_tags):
            mode = "long_quick"
        elif self._tags_any(tags, self.long_normal_mode_tags):
            mode = "long_normal"
        else:
            mode = f"long_unknown ({entry_tag})"

        # btc mode (121) shares the grind stake ladder and grind adjust path,
        # so it averages down just as hard as grind/rebuy.
        aggressive_tags = self.long_grind_mode_tags + self.long_btc_mode_tags + self.long_rebuy_mode_tags
        light_tags = self.long_rapid_mode_tags + self.long_scalp_mode_tags
        dca_profile = self._resolve_dca_profile(tags, aggressive_tags, light_tags)

        return mode, dca_profile

    def _resolve_short_entry_context(self, tags: list[str], entry_tag: str) -> tuple[str, str]:
        if self._tags_all(tags, self.short_grind_mode_tags):
            mode = "short_grind"
        elif self._tags_combo(tags, self.short_rebuy_mode_tags, self.short_rebuy_mode_tags + self.short_grind_mode_tags):
            mode = "short_rebuy"
        elif self._tags_combo(
            tags,
            self.short_rapid_mode_tags,
            self.short_rapid_mode_tags + self.short_rebuy_mode_tags + self.short_grind_mode_tags,
        ):
            mode = "short_rapid"
        elif self._tags_combo(
            tags,
            self.short_scalp_mode_tags,
            self.short_scalp_mode_tags + self.short_rebuy_mode_tags + self.short_grind_mode_tags,
        ):
            mode = "short_scalp"
        elif self._tags_any(tags, self.short_top_coins_mode_tags):
            mode = "short_top_coins"
        elif self._tags_any(tags, self.short_high_profit_mode_tags):
            mode = "short_high_profit"
        elif self._tags_any(tags, self.short_pump_mode_tags):
            mode = "short_pump"
        elif self._tags_any(tags, self.short_quick_mode_tags):
            mode = "short_quick"
        elif self._tags_any(tags, self.short_normal_mode_tags):
            mode = "short_normal"
        else:
            mode = f"short_unknown ({entry_tag})"

        aggressive_tags = self.short_grind_mode_tags + self.short_rebuy_mode_tags
        light_tags = self.short_rapid_mode_tags + self.short_scalp_mode_tags
        dca_profile = self._resolve_dca_profile(tags, aggressive_tags, light_tags)

        return mode, dca_profile

    def _btc_info_pair(self) -> str:
        """Resolve the BTC reference pair the same way NFI does (spot vs futures)."""
        stake = self.config.get("stake_currency", "USDT")
        fiat_like = ["USDT", "BUSD", "USDC", "DAI", "TUSD", "FDUSD", "PAX", "USD", "EUR", "GBP", "TRY"]
        if stake in fiat_like:
            if self.config.get("trading_mode") in ["futures", "margin"]:
                return f"BTC/{stake}:{stake}"
            return f"BTC/{stake}"
        if self.config.get("trading_mode") in ["futures", "margin"]:
            return "BTC/USDT:USDT"
        return "BTC/USDT"

    def _get_btc_context(
        self,
    ) -> tuple[Optional[float], Optional[str], list[dict]]:
        """Return BTC 24h change, a coarse 7d trend label, and a short 1D
        candle sequence so the reviewer can read BTC structure instead of a
        single number (alts usually follow BTC)."""
        try:
            df = self.dp.get_pair_dataframe(pair=self._btc_info_pair(), timeframe="1d")
            if df is None or len(df) < 2:
                return None, None, []

            yesterday = float(df.iloc[-2]["close"])
            today = float(df.iloc[-1]["close"])
            day_change = ((today - yesterday) / yesterday) * 100

            btc_candles: list[dict] = []
            for _, row in df.tail(7).iterrows():
                open_price = float(row.get("open", 0))
                close = float(row.get("close", 0))
                btc_candles.append(
                    {
                        "date": str(row.get("date", ""))[:16],
                        "open": open_price,
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "close": close,
                        "volume": float(row.get("volume", 0)),
                        "change_pct": ((close - open_price) / open_price * 100.0) if open_price else 0.0,
                    }
                )

            trend = None
            if len(df) >= 7:
                week_ago = float(df.iloc[-7]["close"])
                week_change = ((today - week_ago) / week_ago) * 100
                if week_change > 5:
                    trend = "bullish"
                elif week_change < -5:
                    trend = "bearish"
                else:
                    trend = "sideways"

            return day_change, trend, btc_candles
        except Exception:
            return None, None, []

    def _get_position_state(
        self, pair: str, current_time: datetime, current_rate: float
    ) -> Optional[dict]:
        """Describe any already-open position on this pair.

        This is the reviewer's biggest blind spot: the whole point of the veto
        is to avoid letting NFI average down into a bad chart, but without
        knowing how many DCA legs already filled and the current PnL, the model
        cannot tell a fresh entry from a deep grind. NFI counts DCA legs via
        ``trade.select_filled_orders(trade.entry_side)`` (see X7), so we mirror
        that here.
        """
        try:
            open_trades = Trade.get_trades_proxy(pair=pair, is_open=True)
        except Exception:
            return None

        if not open_trades:
            return {"has_open_position": False}

        trade = open_trades[0]
        try:
            filled_entries = trade.select_filled_orders(trade.entry_side)
            nr_filled_entries = len(filled_entries)
        except Exception:
            nr_filled_entries = None

        try:
            current_profit = trade.calc_profit_ratio(current_rate)
        except Exception:
            current_profit = None

        try:
            open_dt = trade.open_date_utc.replace(tzinfo=None)
            age_hours = (current_time.replace(tzinfo=None) - open_dt).total_seconds() / 3600.0
        except Exception:
            age_hours = None

        return {
            "has_open_position": True,
            "nr_filled_entries": nr_filled_entries,
            "open_profit_ratio": current_profit,
            "age_hours": age_hours,
        }
