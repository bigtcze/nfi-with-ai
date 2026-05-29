# NFI + AI Veto

LLM reviewer vrstva nad NostalgiaForInfinityX7. NFI generuje buy signal na 5m svíčkách, AI se podívá na větší obrázek (1d, 4h, 1h price action) a buď ho pustí, nebo zablokuje.

## Proč

NFI je dobrá v hledání krátkodobých setupů na 5m. Ale nemá úsudek — nevidí, že coin právě pumpnul 60 % a kupuje top, nebo že BTC padá a alt ho bude následovat, nebo že se coin 2 týdny sesypává a nic se nezměnilo. AI (GPT-5.4 mini přes cliproxy) se na data podívá jako zkušený trader — s nadhledem a úsudkem, ne s checklistem.

## Architektura

```
NFI X7 buy signal (5m)
  │
  ▼
confirm_trade_entry()
  ├── super() — všechny originální NFI filtry (grind/scalp/top_coins mode, slippage, futures slots)
  │   │
  │   ├── NFI rejected → STOP (bez LLM volání)
  │   └── NFI accepted ▼
  │
  ├── 1D candles (14) + RSI
  ├── 4H candles (20) + RSI
  ├── 1H candles (24) + RSI
  ├── BTC 24h change + 7d trend
  ├── Posledních 5 uzavřených tradů na páru
  │
  ▼
GPT-5.4 mini (přes cliproxy)
  │
  ├── accept  → trade projde
  └── veto    → trade zablokován
```

## Co AI dělá

Dostane OHLCV data ze tří timeframů. Přemýšlí nad nimi jako trader, který se na to dívá s vlastními penězi v sázce. Prompt mu nedává pravidla ani checklist — dává mu roli a zkušenost:

> "You trade crypto for a living. You've seen every kind of market — manias, crashes, slow bleeds, chop, manipulated pumps, coordinated shorts. Think about what you'd actually do with your own money. Would you take this trade right now, seeing this chart?"

AI má lidský nadhled, který bot nemá. Vidí:
- Strukturu ceny — kde je v pohybu, jestli je síla nebo slabost
- Kontext — jestli dává smysl kupovat **právě teď** při tom, co se děje na vyšších TF
- To, co je "očividné" pro člověka ale neviditelné pro indikátory na 5m
- Hlavně: jestli je tenhle vstup přesně ten typ obchodu, do kterého NFI pak bude nebezpečně průměrovat dolů

Většinou pustí trade — bot už udělal svou práci. Ale když je to zjevné, zasáhne.

## Soubory

| Soubor | Co dělá |
|--------|---------|
| `NostalgiaForInfinityX7.py` | Upstream NFI. Neupravovat — sync workflow ji přepíše. |
| `nfi_with_veto.py` | Dědí z X7, overriduje `confirm_trade_entry()`. Vytahuje candle data z 1d/4h/1h a překládá NFI entry tag do mode + DCA profilu. |
| `llm_reviewer.py` | Formátuje price action, volá GPT-5.4 mini přes cliproxy s `reasoning_effort=medium`, parsuje verdict. Cooldown, fail-open, rotující logy. |
| `.github/workflows/sync-upstream.yml` | Kontroluje nový upstream release tag a při nové verzi stáhne X7 z releasu. |

## Bezpečnost

- **Fail-open**: Cliproxy/GPT nedostupné, timeout, parse error → accept.
- **Low confidence override**: Veto s confidence < 0.3 → accept.
- **Cooldown**: Cache je klíčovaná podle `pair + side + entry_tag + entry_mode + dca_profile`, ne jen podle páru. Default 60s.
- **Price invalidation**: Pokud se cena od posledního review pohne o víc než 2 %, cache se zahodí a AI dostane čerstvý review.
- **NFI first**: LLM se volá jen pokud NFI signál prošel všemi filtry.

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

Fáze 1 se nepouští v shadow módu. Dává větší smysl pustit ji rovnou ve `dry_run` Freqtrade, protože AI už reálně veto rozhodnutí uplatňuje nad skutečným order flow NFI.

Pořadí rolloutu:
- `dry_run = true`
- AI veto zapnuté naplno
- sledovat hlavně vetované obchody, obsazení slotů a obchody, které by jinak spadly do `grind/rebuy`
- teprve po ověření v dry run přepnout na live

Smysl Fáze 1 není zvýšit počet entry. Smysl je odstranit malý počet vstupů, které se později změní ve velké averaging-down pasti.

## Logging

`user_data/logs/llm_reviews/reviews-YYYY-MM-DD.jsonl`:

```json
{"timestamp": "2026-05-29T14:32:01", "pair": "SOL/USDT", "entry_tag": "120", "entry_mode": "long_grind", "dca_profile": "aggressive_dca", "side": "long", "rate": 178.5, "open_slots": "6/10", "source": "live", "verdict": "veto", "confidence": 0.88, "rationale": "4H structure is still making lower highs and the daily trend is weak. This is exactly the kind of bounce that can fail and turn into a costly grind.", "llm_response_raw": "{...}"}
```
