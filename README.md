# NFI With AI

An AI veto layer for `NostalgiaForInfinityX7` on Freqtrade.

`NFIWithVeto` keeps NFI X7 responsible for what it already does well: finding short-term 5m setups. Right before a new entry is placed, the signal is reviewed by AI using higher-timeframe price action. The goal is to block the small number of entries that are likely to turn into costly `grind` / `rebuy` traps.

This project does not try to outsmart NFI on every trade. It acts like a trader with broader chart awareness.

## What It Does

- preserves the full original `NostalgiaForInfinityX7` logic
- calls AI only after `super().confirm_trade_entry()` passes
- sends `1W`, `1D`, `4H`, and `1H` candle context (with RSI/MFI/ATR%), structured BTC context, open-position state (filled DCA legs, PnL, age), entry mode, and DCA profile to the reviewer
- allows the reviewer to return only `accept` or `veto`
- uses `fail-open`, so if AI is unavailable the trade proceeds through plain NFI

## What It Does Not Do

- it does not resize positions
- it does not touch `adjust_trade_position()`
- it does not manage exits
- it does not use web search
- it does not fall back to another model

## Repository Files

- `NostalgiaForInfinityX7.py`: upstream strategy baseline
- `nfi_with_veto.py`: wrapper strategy `NFIWithVeto`
- `llm_reviewer.py`: AI review layer via cliproxy
- `.github/workflows/sync-upstream.yml`: automatic upstream release sync
- `agents.md`: technical project overview

## Requirements

1. Freqtrade
2. a running cliproxy instance reachable from Freqtrade
3. a GPT model exposed through cliproxy
4. recommended first run in `dry_run`

## Installation

## Docker Quickstart (recommended)

This repository now includes the same Docker-oriented setup style used by upstream NFI:

- `docker-compose.yml`
- `configs/`
- `live-account-example.env`
- `docker/Dockerfile.custom`

Quick start:

```bash
git clone https://github.com/bigtcze/nfi-with-ai.git
cd nfi-with-ai
mkdir -p user_data
cp configs/recommended_config.json user_data/config.json
cp live-account-example.env .env
docker compose up --build
```

This starts both:

- `freqtrade`
- `nfi-updater`

`nfi-updater` keeps the upstream `NostalgiaForInfinityX7.py`, blacklist, and volume pairlist updated from the original NFI repository and restarts the bot only when one of those files changed.

What to edit first:

1. `user_data/config.json`
   - keep `dry_run: true` for the first run
2. `.env`
   - exchange API keys
   - bot name
   - API server credentials
   - cliproxy address and AI settings
   - optional updater schedule via `NFI_UPDATE_CRON`

Important:

- The included `docker-compose.yml` already mounts `NostalgiaForInfinityX7.py`, `nfi_with_veto.py`, and `llm_reviewer.py`.
- `configs/recommended_config.json` is already adjusted to use `NFIWithVeto`.
- The provided `.env` example defaults to `spot` and `dry_run`.
- Automatic updates affect the upstream strategy baseline (`NostalgiaForInfinityX7.py`), blacklist, and pairlist. Your wrapper files stay local.

### Option A: Freqtrade running directly on the host

1. Clone the repository:

```bash
git clone https://github.com/bigtcze/nfi-with-ai.git
```

2. Copy these files into `user_data/strategies`:

```bash
cp nfi-with-ai/NostalgiaForInfinityX7.py /path/to/freqtrade/user_data/strategies/
cp nfi-with-ai/nfi_with_veto.py /path/to/freqtrade/user_data/strategies/
cp nfi-with-ai/llm_reviewer.py /path/to/freqtrade/user_data/strategies/
```

3. Set environment variables:

```bash
export LLM_BASE_URL=http://localhost:8317/v1
export LLM_MODEL=gpt-5.4-mini
export LLM_REASONING_EFFORT=medium
export LLM_TIMEOUT=20
export LLM_MAX_TOKENS=16384
export LLM_COOLDOWN=60
```

4. Configure the strategy in `config.json`:

```json
{
  "strategy": "NFIWithVeto",
  "strategy_path": "/path/to/freqtrade/user_data/strategies",
  "dry_run": true
}
```

### Option B: Freqtrade running in Docker

If you use the included `docker-compose.yml`, prefer the Docker Quickstart above.

Inside Docker, `localhost` means the container, not the host. If cliproxy runs on the host machine, use the host address instead.

Typical setup:

```yaml
services:
  freqtrade:
    image: freqtradeorg/freqtrade:stable
    volumes:
      - ./user_data:/freqtrade/user_data
      - ./nfi-with-ai/NostalgiaForInfinityX7.py:/freqtrade/user_data/strategies/NostalgiaForInfinityX7.py
      - ./nfi-with-ai/nfi_with_veto.py:/freqtrade/user_data/strategies/nfi_with_veto.py
      - ./nfi-with-ai/llm_reviewer.py:/freqtrade/user_data/strategies/llm_reviewer.py
    environment:
      - LLM_BASE_URL=http://host.docker.internal:8317/v1
      - LLM_MODEL=gpt-5.4-mini
      - LLM_REASONING_EFFORT=medium
      - LLM_TIMEOUT=20
      - LLM_MAX_TOKENS=16384
      - LLM_COOLDOWN=60
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Then in `config.json`:

```json
{
  "strategy": "NFIWithVeto",
  "strategy_path": "/freqtrade/user_data/strategies",
  "dry_run": true
}
```

If cliproxy runs in the same Docker network as Freqtrade, use the cliproxy service name instead of `host.docker.internal`.

## How To Use It

1. Start cliproxy.
2. Verify the endpoint responds on `http://localhost:8317/v1` or whatever address Freqtrade will use.
3. Run Freqtrade with `dry_run = true`.
4. Confirm that the strategy `NFIWithVeto` is loaded.
5. Watch the logs and the vetoed trades.

## Recommended First Run

1. Start with `dry_run`.
2. Keep AI veto fully enabled.
3. Review vetoed trades, slot occupancy, and trades that would otherwise have fallen into `grind` / `rebuy` behavior.
4. Move to live trading only after that looks sane.

The point of this layer is not to increase trade count. The point is to remove the small number of entries that later turn into large averaging-down traps.

## Logging

Every AI review is written to:

```text
user_data/logs/llm_reviews/reviews-YYYY-MM-DD.jsonl
```

Example:

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
  "rationale": "4H structure is still making lower highs and the daily trend is weak. This is exactly the kind of bounce that can fail and turn into a costly grind."
}
```

Logs rotate daily and files older than 30 days are removed automatically.

## AI Configuration

Environment variables used by the reviewer:

- `LLM_BASE_URL`: cliproxy endpoint
- `LLM_MODEL`: model exposed by cliproxy
- `LLM_REASONING_EFFORT`: `low`, `medium`, `high` depending on cliproxy/model support. Default `medium`; `high` reasons long on every review and increases timeout risk.
- `LLM_TIMEOUT`: request timeout in seconds
- `LLM_MAX_TOKENS`: max completion tokens; high enough that reasoning plus JSON both fit (otherwise truncated JSON fails open)
- `LLM_COOLDOWN`: cache duration for repeated reviews in seconds

Repository defaults:

```bash
LLM_BASE_URL=http://localhost:8317/v1
LLM_MODEL=gpt-5.4-mini
LLM_REASONING_EFFORT=medium
LLM_TIMEOUT=20
LLM_MAX_TOKENS=16384
LLM_COOLDOWN=60
```

## Upstream Sync

The workflow in `.github/workflows/sync-upstream.yml` watches for a new upstream release tag from `iterativv/NostalgiaForInfinity` and updates `NostalgiaForInfinityX7.py` when a new release appears.

The AI layer stays separate in `nfi_with_veto.py` and `llm_reviewer.py`.

## Automatic Updates (Docker)

The included `nfi-updater` sidecar keeps the following files in sync with upstream NFI:

- `NostalgiaForInfinityX7.py`
- `configs/blacklist-<exchange>.json`
- the configured volume pairlist file

It does not overwrite:

- `nfi_with_veto.py`
- `llm_reviewer.py`
- your local `.env`
- your `user_data/config.json`

Relevant `.env` variables:

```bash
NFI_UPDATE_CRON=0 10 * * *
COMPOSE_PROJECT_NAME=nfiwithai
# Optional if your pairlist filename differs
# NFI_PAIRLIST_FILE=pairlist-volume-binance-usdt.json
```

To inspect updater logs:

```bash
docker compose logs -f nfi-updater
```

## Important Notes

1. If cliproxy is unavailable, the bot continues with plain NFI logic.
2. If you run Docker, double-check the cliproxy address. `localhost` inside the container is almost never what you want.
3. Always start with `dry_run` before going live.
