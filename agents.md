# NFI + AI Veto

An LLM review layer on top of `NostalgiaForInfinityX7`. NFI generates a 5m entry signal, and AI looks at the broader picture using `1D`, `4H`, and `1H` price action before deciding whether to allow or block the trade.

## Why

NFI is very good at finding short-term setups on 5m, but it has no judgment. It cannot see that a coin just pumped 60% and is trying to buy the top, or that BTC is rolling over and the alt will likely follow, or that a coin has been bleeding for two weeks and nothing in the structure has improved. AI reviews the chart like an experienced trader, with context and judgment instead of a checklist.

## Architecture

```text
NFI X7 buy signal (5m)
  |
  v
confirm_trade_entry()
  |- super() - all original NFI filters (grind/scalp/top_coins mode, slippage, futures slots)
  |  |
  |  |- NFI rejected -> STOP (no LLM call)
  |  \- NFI accepted
  |
  |- 1D candles (14) + RSI/MFI
  |- 4H candles (20) + RSI/MFI
  |- 1H candles (24) + RSI/MFI
  |- BTC 24h change + 7d trend
  |- recent closed trades on the pair
  |- entry mode + DCA profile
  |- open slot usage
  |
  v
GPT-5.4 mini via cliproxy
  |
  |- accept -> trade proceeds
  \- veto   -> trade is blocked
```

## What The AI Does

The reviewer receives multi-timeframe OHLCV data and thinks like a trader looking at a chart with their own money on the line.

The prompt does not ask the model to optimize entries or invent hidden signals. It asks one important question:

> If this entry is wrong, does this look like the kind of chart I want NFI averaging down into for hours or days?

That is the real weak point of NFI. The strategy is already excellent at indicator-based filtering. What it lacks is broader chart judgment.

The AI is meant to catch:
- failed bounces inside weak higher-timeframe structure
- post-pump rollover entries
- obvious trend exhaustion
- slow bleed structures where NFI would keep averaging down
- bad opportunity-cost trades that would consume scarce slots

Most trades should still pass. The AI is a veto layer, not a replacement strategy.

## Files

| File | Purpose |
|------|---------|
| `NostalgiaForInfinityX7.py` | Upstream NFI strategy. Do not edit manually; sync workflow replaces it on release updates. |
| `nfi_with_veto.py` | Wrapper strategy. Overrides `confirm_trade_entry()`, extracts higher-timeframe candles, and maps NFI tags into entry mode + DCA profile. |
| `llm_reviewer.py` | Formats price action, calls GPT-5.4 mini through cliproxy with `reasoning_effort=medium`, parses the veto decision, caches repeated reviews, and writes rotating logs. |
| `.github/workflows/sync-upstream.yml` | Watches upstream release tags and updates X7 from the released version. |

## Safety Rules

- **Fail-open**: if cliproxy/GPT is unavailable, times out, or returns invalid output, the trade is accepted.
- **Low-confidence override**: veto with confidence below `0.3` is converted to `accept`.
- **Cooldown cache**: keyed by `pair + side + entry_tag + entry_mode + dca_profile`, not just pair. Default is 60 seconds.
- **Price invalidation**: if price moves more than 2% since the last cached review, the cache is discarded and a fresh review is requested.
- **NFI first**: the LLM is called only if the original X7 entry passed all native NFI filters.

## Setup

```bash
export LLM_BASE_URL=http://localhost:8317/v1
export LLM_MODEL=gpt-5.4-mini
export LLM_REASONING_EFFORT=medium
export LLM_TIMEOUT=8
export LLM_COOLDOWN=60
```

```json
{
  "strategy": "NFIWithVeto",
  "strategy_path": "/freqtrade/user_data/strategies"
}
```

## Rollout

This setup is not meant for shadow mode. It makes more sense to run it directly in Freqtrade `dry_run`, because AI should already be making real veto decisions on the actual NFI order flow.

Recommended rollout:
- `dry_run = true`
- AI veto fully enabled
- watch vetoed trades, slot occupancy, and trades that would otherwise have gone into `grind` / `rebuy`
- move to live only after dry-run behavior looks sane

This setup is not about increasing entries. It is about removing the small number of entries that later become large averaging-down traps.

## Logging

Logs are written to:

```text
user_data/logs/llm_reviews/reviews-YYYY-MM-DD.jsonl
```

Example record:

```json
{
  "timestamp": "2026-05-29T14:32:01",
  "pair": "SOL/USDT",
  "entry_tag": "120",
  "entry_mode": "long_grind",
  "dca_profile": "aggressive_dca",
  "side": "long",
  "rate": 178.5,
  "open_slots": "6/10",
  "source": "live",
  "verdict": "veto",
  "confidence": 0.88,
  "rationale": "4H structure is still making lower highs and the daily trend is weak. This is exactly the kind of bounce that can fail and turn into a costly grind.",
  "llm_response_raw": "{...}"
}
```
