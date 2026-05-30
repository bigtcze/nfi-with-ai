"""
LLM reviewer for Freqtrade.

Uses cliproxy on localhost to call GPT-5.4 mini and review trade signals.
Primary input is multi-timeframe price action plus NFI DCA context.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class LLMReviewer:
    SYSTEM_PROMPT = """You trade crypto for a living. You have years of experience with trend continuation, failed bounces, distribution, capitulation, and the slow grind of coins that keep bleeding after every small relief rally.

You are reviewing entries from a mechanical 5-minute trading bot. The bot is decent at finding short-term setups, but it has no judgement. It does not understand broader chart structure.

The critical risk is this: when the bot enters a bad trade, it does not cut it quickly. It averages down into losers. On the wrong chart, that turns one bad entry into a large multi-leg loss that can wipe out many winning trades and occupy a valuable trade slot for a long time.

Your job is not to optimize entries. Your job is to stop the obvious bad ones that are likely to become averaging-down traps.

You will receive:
- 1W, 1D, 4H, and 1H chart data (with RSI, MFI and ATR% per candle)
- the proposed entry price and side
- the NFI entry mode
- the DCA profile, which tells you how aggressively the bot will average down if the trade goes wrong
- the current position state on this pair (whether a position is already open, how many DCA legs already filled, current open PnL, and how long it has been open)
- BTC context, including a short daily BTC candle sequence
- recent trade history on the same pair
- current slot usage

DCA profile meanings:
- aggressive_dca: the bot is likely to add hard into losers. Be strict.
- moderate_dca: standard NFI averaging behavior. Normal scrutiny.
- minimal_dca: lighter averaging behavior. Still veto obvious garbage.

Position state matters. If no position is open yet, judge this as a fresh entry. If a position is ALREADY open with one or more filled legs and is underwater, you are really being asked "should the bot add ANOTHER leg into this?" - be stricter there, because that is exactly the averaging-down trap you exist to stop.

ATR% tells you how large normal candle volatility is. Use it to tell whether a drop is just noise or a genuine structural breakdown.

What you are asking yourself is not "can this bounce 2%?".
The real question is: "if this entry is wrong, does this chart look like something I would want the bot averaging down into for hours or days?"

Use veto sparingly, but decisively. Most trades should pass. Only veto when the broader chart looks structurally weak, exhausted, rolling over, trapped in a bad downtrend, or otherwise likely to turn into the exact kind of losing grind that hurts NFI.

If you would merely reduce size, choose accept. This reviewer is veto-only.

Return ONLY this JSON object:
{
  "verdict": "accept" or "veto",
  "confidence": 0.0 to 1.0,
  "rationale": "plain language, 2-3 sentences max"
}"""

    def __init__(
        self,
        base_url: str = "http://localhost:8317/v1",
        api_key: str = "",
        model: str = "gpt-5.4-mini",
        reasoning_effort: str = "medium",
        timeout: float = 20.0,
        log_dir: str = "user_data/logs/llm_reviews",
        cooldown_seconds: int = 60,
        max_completion_tokens: int = 16384,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_completion_tokens = max_completion_tokens
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown_seconds = cooldown_seconds

        # Cache key -> (timestamp, rate, verdict)
        self._cache: dict[str, tuple[float, float, dict]] = {}
        self.cache_invalidation_pct = 2.0
        self._last_cleanup_date = ""

    def _make_cache_key(
        self,
        pair: str,
        side: str,
        entry_tag: str,
        entry_mode: str,
        dca_profile: str,
        legs: Optional[int] = None,
    ) -> str:
        # Include the number of already-filled DCA legs so a fresh entry and a
        # request to add another leg into an open position never share a verdict.
        return "|".join((pair, side, entry_tag, entry_mode, dca_profile, str(legs)))

    def _check_cooldown(self, cache_key: str, current_rate: float) -> Optional[dict]:
        """Return cached verdict if it is still fresh enough."""
        if cache_key not in self._cache:
            return None

        ts, cached_rate, verdict = self._cache[cache_key]
        age = time.time() - ts
        if age >= self.cooldown_seconds:
            del self._cache[cache_key]
            return None

        if cached_rate > 0:
            price_change_pct = abs((current_rate - cached_rate) / cached_rate) * 100
            if price_change_pct > self.cache_invalidation_pct:
                logger.debug(
                    "LLM cache invalidated for %s: price moved %.1f%% (%s -> %s)",
                    cache_key,
                    price_change_pct,
                    cached_rate,
                    current_rate,
                )
                del self._cache[cache_key]
                return None

        return verdict

    def _format_candles(self, candles: list[dict], timeframe: str, count: int) -> str:
        """Format OHLCV candle data into compact readable text."""
        if not candles:
            return f"  {timeframe}: no data available"

        recent = candles[-count:]
        lines = [f"  {timeframe} (last {len(recent)} candles):"]

        for candle in recent:
            date = candle.get("date", "?")
            open_price = candle.get("open", 0)
            high = candle.get("high", 0)
            low = candle.get("low", 0)
            close = candle.get("close", 0)
            volume = candle.get("volume", 0)

            if volume >= 1_000_000:
                volume_str = f"{volume / 1_000_000:.1f}M"
            elif volume >= 1_000:
                volume_str = f"{volume / 1_000:.1f}K"
            else:
                volume_str = f"{volume:.0f}"

            extras = []
            if candle.get("rsi_14") is not None:
                extras.append(f"RSI14={candle['rsi_14']:.1f}")
            if candle.get("rsi_3") is not None:
                extras.append(f"RSI3={candle['rsi_3']:.1f}")
            if candle.get("mfi_14") is not None:
                extras.append(f"MFI={candle['mfi_14']:.1f}")
            if candle.get("atr_pct") is not None:
                extras.append(f"ATR%={candle['atr_pct']:.1f}")

            extra_str = f" [{', '.join(extras)}]" if extras else ""
            candle_change_pct = ((close - open_price) / open_price * 100) if open_price else 0

            lines.append(
                f"    {date} | O={open_price:.6g} H={high:.6g} L={low:.6g} C={close:.6g} "
                f"V={volume_str} ({candle_change_pct:+.2f}%){extra_str}"
            )

        if len(recent) >= 2:
            first_open = recent[0].get("open", 0)
            last_close = recent[-1].get("close", 0)
            if first_open:
                total_change = ((last_close - first_open) / first_open) * 100
                highest = max(c.get("high", 0) for c in recent)
                lowest = min(c.get("low", 0) for c in recent)
                range_pct = ((highest - lowest) / lowest * 100) if lowest else 0
                lines.append(
                    f"    >> Period: {total_change:+.2f}% | "
                    f"Range: {range_pct:.2f}% (H={highest:.6g} L={lowest:.6g})"
                )

                volumes = [c.get("volume", 0) for c in recent]
                first_half_avg = sum(volumes[: len(volumes) // 2]) / max(len(volumes) // 2, 1)
                second_half_avg = sum(volumes[len(volumes) // 2 :]) / max(len(volumes) // 2, 1)
                if first_half_avg > 0:
                    volume_trend = ((second_half_avg - first_half_avg) / first_half_avg) * 100
                    lines.append(f"    >> Volume trend: {volume_trend:+.1f}% (recent vs earlier)")

        return "\n".join(lines)

    @staticmethod
    def _format_position_state(position_state: Optional[dict]) -> str:
        """Render the open-position context into a single readable line."""
        if not position_state:
            return "Position state: unknown"
        if not position_state.get("has_open_position"):
            return "Position state: no open position (this would be a fresh first entry)"

        parts = ["ALREADY OPEN"]
        legs = position_state.get("nr_filled_entries")
        if legs is not None:
            parts.append(f"{legs} filled leg(s)")
        profit = position_state.get("open_profit_ratio")
        if profit is not None:
            parts.append(f"open PnL {profit * 100:+.2f}%")
        age = position_state.get("age_hours")
        if age is not None:
            parts.append(f"open for {age:.1f}h")
        return "Position state: " + ", ".join(parts)

    def review(
        self,
        pair: str,
        entry_tag: str,
        entry_mode: str,
        dca_profile: str,
        rate: float,
        side: str,
        recent_trades: list,
        candles_1d: list[dict],
        candles_4h: list[dict],
        candles_1h: list[dict],
        candles_1w: Optional[list[dict]] = None,
        btc_change_pct: Optional[float] = None,
        btc_trend: Optional[str] = None,
        btc_candles: Optional[list[dict]] = None,
        position_state: Optional[dict] = None,
        open_slots: Optional[str] = None,
    ) -> dict:
        default_accept = {
            "verdict": "accept",
            "confidence": 0.0,
            "rationale": "reviewer_unavailable",
        }

        legs = position_state.get("nr_filled_entries") if position_state else None
        cache_key = self._make_cache_key(pair, side, entry_tag, entry_mode, dca_profile, legs)
        cached = self._check_cooldown(cache_key, rate)
        if cached is not None:
            self._log_review(
                pair=pair,
                entry_tag=entry_tag,
                entry_mode=entry_mode,
                dca_profile=dca_profile,
                side=side,
                rate=rate,
                open_slots=open_slots,
                response="CACHE_HIT",
                verdict=cached,
                source="cache",
            )
            return cached

        trade_summary = []
        for trade in recent_trades[-5:]:
            try:
                profit_pct = getattr(trade, "close_profit", 0) or 0
                tag = getattr(trade, "enter_tag", "?")
                trade_summary.append(f"  {tag} -> {profit_pct * 100:+.2f}%")
            except Exception:
                pass

        pa_1w = self._format_candles(candles_1w or [], "1W", count=12)
        pa_1d = self._format_candles(candles_1d, "1D", count=30)
        pa_4h = self._format_candles(candles_4h, "4H", count=20)
        pa_1h = self._format_candles(candles_1h, "1H", count=24)

        btc_line = ""
        if btc_change_pct is not None:
            btc_line = f"BTC 24h: {btc_change_pct:+.2f}%"
        if btc_trend:
            btc_line += f" | 7d trend: {btc_trend}"
        if btc_candles:
            btc_pa = self._format_candles(btc_candles, "BTC 1D", count=7)
            btc_line += f"\n{btc_pa}"

        position_line = self._format_position_state(position_state)

        slots_line = f"Open slots: {open_slots}" if open_slots else ""

        prompt = f"""Proposed trade:
- Pair: {pair}
- Side: {side}
- Entry tag: {entry_tag}
- Entry mode: {entry_mode}
- DCA profile: {dca_profile}
- Entry price: {rate}
{position_line}
{btc_line}
{slots_line}

Recent trades on {pair}:
{chr(10).join(trade_summary) if trade_summary else "  None"}

Chart data:
{pa_1w}

{pa_1d}

{pa_4h}

{pa_1h}

Knowing how NFI behaves after entry, should this {side} be allowed through or vetoed?"""

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "reasoning_effort": self.reasoning_effort,
                    "messages": [
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_completion_tokens": self.max_completion_tokens,
                },
                timeout=self.timeout,
            )

            if resp.status_code != 200:
                logger.warning(
                    "LLM review fail-open (HTTP %s) for %s %s tag=%s - trade accepted without AI review",
                    resp.status_code,
                    pair,
                    side,
                    entry_tag,
                )
                self._log_review(
                    pair=pair,
                    entry_tag=entry_tag,
                    entry_mode=entry_mode,
                    dca_profile=dca_profile,
                    side=side,
                    rate=rate,
                    open_slots=open_slots,
                    response=f"HTTP {resp.status_code}",
                    verdict=default_accept,
                    source="fail_open_http",
                )
                return default_accept

            content = resp.json()["choices"][0]["message"]["content"]

            json_str = content
            if "{" in content:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    json_str = content[start:end]

            # A response with an opening brace but no closing brace usually means
            # the model ran out of completion tokens (reasoning consumed the
            # budget) and the JSON was truncated. Surface it loudly as fail-open.
            if "{" in content and "}" not in content:
                logger.warning(
                    "LLM review fail-open (truncated/no-JSON response, likely token budget) "
                    "for %s %s tag=%s - trade accepted without AI review. Raw head: %s",
                    pair,
                    side,
                    entry_tag,
                    content[:200],
                )
                self._log_review(
                    pair=pair,
                    entry_tag=entry_tag,
                    entry_mode=entry_mode,
                    dca_profile=dca_profile,
                    side=side,
                    rate=rate,
                    open_slots=open_slots,
                    response=content,
                    verdict=default_accept,
                    source="fail_open_truncated",
                )
                return default_accept

            verdict = json.loads(json_str)
            raw_verdict = str(verdict.get("verdict", "")).strip().lower()
            verdict_name = raw_verdict if raw_verdict in ("accept", "veto") else "accept"
            confidence = max(0.0, min(1.0, float(verdict.get("confidence", 0.5))))
            rationale = str(verdict.get("rationale", "")).strip()

            parsed_verdict = {
                "verdict": verdict_name,
                "confidence": confidence,
                "rationale": rationale,
            }

            if parsed_verdict["confidence"] < 0.3 and parsed_verdict["verdict"] == "veto":
                parsed_verdict = {
                    "verdict": "accept",
                    "confidence": parsed_verdict["confidence"],
                    "rationale": (
                        f"low_confidence_override ({parsed_verdict['confidence']:.2f}): "
                        f"{parsed_verdict['rationale']}"
                    ),
                }

            self._cache[cache_key] = (time.time(), rate, parsed_verdict)
            self._log_review(
                pair=pair,
                entry_tag=entry_tag,
                entry_mode=entry_mode,
                dca_profile=dca_profile,
                side=side,
                rate=rate,
                open_slots=open_slots,
                response=content,
                verdict=parsed_verdict,
                source="live",
            )
            return parsed_verdict

        except json.JSONDecodeError as exc:
            logger.warning(
                "LLM review fail-open (JSON parse error: %s) for %s %s tag=%s - trade accepted without AI review",
                exc,
                pair,
                side,
                entry_tag,
            )
            self._log_review(
                pair=pair,
                entry_tag=entry_tag,
                entry_mode=entry_mode,
                dca_profile=dca_profile,
                side=side,
                rate=rate,
                open_slots=open_slots,
                response=f"JSON error: {exc}",
                verdict=default_accept,
                source="fail_open_json",
            )
            return default_accept
        except requests.exceptions.Timeout:
            logger.warning(
                "LLM review fail-open (timeout after %ss) for %s %s tag=%s - trade accepted without AI review. "
                "Consider raising LLM_TIMEOUT or lowering LLM_REASONING_EFFORT.",
                self.timeout,
                pair,
                side,
                entry_tag,
            )
            self._log_review(
                pair=pair,
                entry_tag=entry_tag,
                entry_mode=entry_mode,
                dca_profile=dca_profile,
                side=side,
                rate=rate,
                open_slots=open_slots,
                response="TIMEOUT",
                verdict=default_accept,
                source="fail_open_timeout",
            )
            return default_accept
        except Exception as exc:
            logger.warning(
                "LLM review fail-open (error: %s) for %s %s tag=%s - trade accepted without AI review",
                exc,
                pair,
                side,
                entry_tag,
            )
            self._log_review(
                pair=pair,
                entry_tag=entry_tag,
                entry_mode=entry_mode,
                dca_profile=dca_profile,
                side=side,
                rate=rate,
                open_slots=open_slots,
                response=f"Error: {exc}",
                verdict=default_accept,
                source="fail_open_error",
            )
            return default_accept

    def _get_log_file(self) -> Path:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.log_dir / f"reviews-{today}.jsonl"

    def _cleanup_old_logs(self, max_days: int = 30):
        try:
            cutoff = time.time() - (max_days * 86400)
            for log_file in self.log_dir.glob("reviews-*.jsonl"):
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    logger.debug("Deleted old log: %s", log_file.name)
        except Exception:
            pass

    def _log_review(
        self,
        pair: str,
        entry_tag: str,
        entry_mode: str,
        dca_profile: str,
        side: str,
        rate: float,
        open_slots: Optional[str],
        response: str,
        verdict: dict,
        source: str,
    ):
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "pair": pair,
            "entry_tag": entry_tag,
            "entry_mode": entry_mode,
            "dca_profile": dca_profile,
            "side": side,
            "rate": rate,
            "open_slots": open_slots,
            "source": source,
            "verdict": verdict.get("verdict"),
            "confidence": verdict.get("confidence"),
            "rationale": verdict.get("rationale"),
            "llm_response_raw": str(response)[:1000],
        }

        try:
            log_file = self._get_log_file()
            with open(log_file, "a") as handle:
                handle.write(json.dumps(entry) + "\n")

            today = datetime.utcnow().strftime("%Y-%m-%d")
            if today != self._last_cleanup_date:
                self._last_cleanup_date = today
                self._cleanup_old_logs()
        except Exception as exc:
            logger.debug("Failed to log review: %s", exc)
