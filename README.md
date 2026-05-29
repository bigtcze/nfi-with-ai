# NFI With AI

AI veto vrstva nad `NostalgiaForInfinityX7` pro Freqtrade.

`NFIWithVeto` nechá NFI X7 dělat to, v čem je silná: hledání krátkodobých setupů na 5m. Před samotným vstupem ale pustí signál přes AI review, které se podívá na vyšší timeframe price action a zablokuje obchody, které vypadají jako budoucí `grind` / `rebuy` past.

AI neřeší sentiment, zprávy ani stock-picking styl rozhodování. Chová se jako trader s nadhledem nad chartem.

## Co to dělá

- zachová kompletní původní logiku `NostalgiaForInfinityX7`
- zapojí AI až po `super().confirm_trade_entry()`
- AI dostane `1D`, `4H`, `1H` candles, BTC kontext, entry mode a DCA profil
- AI vrací jen `accept` nebo `veto`
- při výpadku AI běží režim `fail-open`, takže trade projde čistě přes NFI

## Co to nedělá

- nemění size pozice
- nesahá do `adjust_trade_position()`
- neřeší exity
- nepoužívá websearch
- nepoužívá fallback na jiný model

## Soubory v repu

- `NostalgiaForInfinityX7.py`: upstream strategie
- `nfi_with_veto.py`: wrapper strategie `NFIWithVeto`
- `llm_reviewer.py`: AI review vrstva přes cliproxy
- `.github/workflows/sync-upstream.yml`: automatický sync nového upstream releasu
- `agents.md`: interní technický overview

## Požadavky

1. Freqtrade
2. běžící cliproxy na stroji, kde je dostupné z Freqtrade
3. GPT model přes cliproxy
4. doporučený první rollout v `dry_run`

## Instalace

### Varianta A: Freqtrade běží přímo na hostu

1. Naklonuj repo:

```bash
git clone https://github.com/bigtcze/nfi-with-ai.git
```

2. Zkopíruj tyto soubory do `user_data/strategies`:

```bash
cp nfi-with-ai/NostalgiaForInfinityX7.py /path/to/freqtrade/user_data/strategies/
cp nfi-with-ai/nfi_with_veto.py /path/to/freqtrade/user_data/strategies/
cp nfi-with-ai/llm_reviewer.py /path/to/freqtrade/user_data/strategies/
```

3. Nastav environment variables:

```bash
export LLM_BASE_URL=http://localhost:8317/v1
export LLM_MODEL=gpt-5.4-mini
export LLM_REASONING_EFFORT=medium
export LLM_TIMEOUT=8
export LLM_COOLDOWN=60
```

4. V `config.json` nastav strategii:

```json
{
  "strategy": "NFIWithVeto",
  "strategy_path": "/path/to/freqtrade/user_data/strategies",
  "dry_run": true
}
```

### Varianta B: Freqtrade běží v Dockeru

V Dockeru `localhost` znamená kontejner, ne host. Pokud cliproxy běží na hostu, musíš použít adresu hosta.

Nejčastější varianta je:

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
      - LLM_TIMEOUT=8
      - LLM_COOLDOWN=60
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

V `config.json` pak:

```json
{
  "strategy": "NFIWithVeto",
  "strategy_path": "/freqtrade/user_data/strategies",
  "dry_run": true
}
```

Pokud cliproxy běží ve stejném Docker networku jako Freqtrade, nastav místo `host.docker.internal` přímo název služby.

## Jak to použít

1. spusť cliproxy
2. ověř, že endpoint odpovídá na `http://localhost:8317/v1` nebo na adresu, kterou používá Freqtrade
3. spusť Freqtrade s `dry_run = true`
4. zkontroluj, že se načetla strategie `NFIWithVeto`
5. sleduj logy a vetované obchody

## Doporučený rollout

1. nejdřív `dry_run`
2. AI veto zapnuté naplno
3. sledovat hlavně vetované obchody, obsazení slotů a obchody, které by jinak spadly do `grind/rebuy`
4. až potom přepnout na live

Smysl této vrstvy není zvýšit počet entry. Smysl je odstranit malý počet vstupů, které se později změní ve velké averaging-down pasti.

## Logging

Každé AI review se zapisuje do:

```text
user_data/logs/llm_reviews/reviews-YYYY-MM-DD.jsonl
```

Log obsahuje například:

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

Logy rotují denně a starší než 30 dní se automaticky mažou.

## Konfigurace AI

Použité proměnné:

- `LLM_BASE_URL`: cliproxy endpoint
- `LLM_MODEL`: model dostupný přes cliproxy
- `LLM_REASONING_EFFORT`: `low`, `medium`, `high` podle podpory cliproxy/modelu
- `LLM_TIMEOUT`: timeout requestu v sekundách
- `LLM_COOLDOWN`: cache pro stejné review v sekundách

Defaulty v repu:

```bash
LLM_BASE_URL=http://localhost:8317/v1
LLM_MODEL=gpt-5.4-mini
LLM_REASONING_EFFORT=medium
LLM_TIMEOUT=8
LLM_COOLDOWN=60
```

## Upstream sync

Workflow v `.github/workflows/sync-upstream.yml` kontroluje nový upstream release tag z `iterativv/NostalgiaForInfinity` a při nové verzi stáhne čerstvý `NostalgiaForInfinityX7.py`.

Tvoje AI vrstva zůstává odděleně v `nfi_with_veto.py` a `llm_reviewer.py`.

## Důležité poznámky

1. Repo počítá s tím, že AI vrstva je jen `entry veto` pro Fázi 1.
2. Pokud cliproxy není dostupné, bot jede dál přes čisté NFI.
3. Pokud jedeš Docker, zkontroluj adresu cliproxy. `localhost` uvnitř kontejneru téměř nikdy není to, co chceš.
4. Před live nasazením vždy nejdřív `dry_run`.
