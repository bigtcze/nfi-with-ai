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
            model=os.environ.get("LLM_MODEL", "gpt-5.4-mini"),
            reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", "medium"),
            timeout=float(os.environ.get("LLM_TIMEOUT", "8")),
            cooldown_seconds=int(os.environ.get("LLM_COOLDOWN", "60")),
        )

        logger.info(
            "NFIWithVeto initialized - model=%s effort=%s base_url=%s cooldown=%ss fail_mode=open",
            self.reviewer.model,
            self.reviewer.reasoning_effort,
            self.reviewer.base_url,
            self.reviewer.cooldown_seconds,
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

        candles_1d = self._extract_candles(pair, "1d", count=14)
        candles_4h = self._extract_candles(pair, "4h", count=20)
        candles_1h = self._extract_candles(pair, "1h", count=24)

        try:
            recent_trades = Trade.get_trades_proxy(pair=pair, is_open=False)
            recent_trades = sorted(recent_trades, key=lambda t: t.close_date or datetime.min)[-10:]
        except Exception:
            recent_trades = []

        btc_change, btc_trend = self._get_btc_context()
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
            candles_1d=candles_1d,
            candles_4h=candles_4h,
            candles_1h=candles_1h,
            btc_change_pct=btc_change,
            btc_trend=btc_trend,
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
        """Extract recent OHLCV plus key indicators from one timeframe."""
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, timeframe)
            if df is None or df.empty:
                return []

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

                indicator_map = {
                    "rsi_14": ["RSI_14", "rsi_14"],
                    "rsi_3": ["RSI_3", "rsi_3"],
                    "mfi_14": ["MFI_14", "mfi_14"],
                    "change_pct": ["change_pct"],
                }
                for out_name, candidates in indicator_map.items():
                    for column_name in candidates:
                        if (
                            column_name in row.index
                            and row[column_name] is not None
                            and not (isinstance(row[column_name], float) and np.isnan(row[column_name]))
                        ):
                            candle[out_name] = float(row[column_name])
                            break

                candles.append(candle)

            return candles
        except Exception as exc:
            logger.debug("Failed to extract %s candles for %s: %s", timeframe, pair, exc)
            return []

    def _tags_all(self, tags: list[str], allowed: list[str]) -> bool:
        return bool(tags) and all(tag in allowed for tag in tags)

    def _tags_combo(self, tags: list[str], primary: list[str], combined_allowed: list[str]) -> bool:
        return bool(tags) and any(tag in primary for tag in tags) and all(tag in combined_allowed for tag in tags)

    def _tags_any(self, tags: list[str], allowed: list[str]) -> bool:
        return any(tag in allowed for tag in tags)

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
        elif self._tags_any(tags, self.long_pump_mode_tags):
            mode = "long_pump"
        elif self._tags_any(tags, self.long_quick_mode_tags):
            mode = "long_quick"
        elif self._tags_any(tags, self.long_normal_mode_tags):
            mode = "long_normal"
        else:
            mode = f"long_unknown ({entry_tag})"

        if self._tags_any(tags, self.long_grind_mode_tags + self.long_rebuy_mode_tags):
            dca_profile = "aggressive_dca"
        elif self._tags_any(tags, self.long_rapid_mode_tags + self.long_scalp_mode_tags):
            dca_profile = "minimal_dca"
        else:
            dca_profile = "moderate_dca"

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
        elif self._tags_any(tags, self.short_pump_mode_tags):
            mode = "short_pump"
        elif self._tags_any(tags, self.short_quick_mode_tags):
            mode = "short_quick"
        elif self._tags_any(tags, self.short_normal_mode_tags):
            mode = "short_normal"
        else:
            mode = f"short_unknown ({entry_tag})"

        if self._tags_any(tags, self.short_grind_mode_tags + self.short_rebuy_mode_tags):
            dca_profile = "aggressive_dca"
        elif self._tags_any(tags, self.short_rapid_mode_tags + self.short_scalp_mode_tags):
            dca_profile = "minimal_dca"
        else:
            dca_profile = "moderate_dca"

        return mode, dca_profile

    def _get_btc_context(self) -> tuple[Optional[float], Optional[str]]:
        try:
            df, _ = self.dp.get_analyzed_dataframe("BTC/USDT", "1d")
            if df is None or len(df) < 2:
                return None, None

            yesterday = float(df.iloc[-2]["close"])
            today = float(df.iloc[-1]["close"])
            day_change = ((today - yesterday) / yesterday) * 100

            if len(df) >= 7:
                week_ago = float(df.iloc[-7]["close"])
                week_change = ((today - week_ago) / week_ago) * 100
                if week_change > 5:
                    trend = "bullish"
                elif week_change < -5:
                    trend = "bearish"
                else:
                    trend = "sideways"
                return day_change, trend

            return day_change, None
        except Exception:
            return None, None
