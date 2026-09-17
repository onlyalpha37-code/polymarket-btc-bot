# Polymarket BTC 5-Minute Bot

This bot implements the specified strategy:

1. BTC Up/Down 5-minute Polymarket markets.
2. Enter only when 3 minutes or less remain.
3. Buy an outcome only when its executable best ask is strictly above $0.75.
4. Allocate exactly $5 USDC per entry.
5. Hold until expiry.
6. If the executable best bid falls strictly below $0.49 before expiry, immediately attempt an FAK market sell.
7. No compounding logic is used.
8. Paper trading is the default.

## Important implementation detail

The bot checks the CLOB order book rather than relying only on the displayed Gamma probability. That matters because a displayed price is not necessarily the price at which a $5 order can actually execute.

## Run on a computer/server

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python bot.py
```

## iPad use

The iPad is the control device; the bot itself should run on an always-on computer/cloud server. The same project can later be wrapped with a small browser dashboard.

## Live trading

Set `LIVE_TRADING=true` only after paper testing.

The bot uses Polymarket's current CLOB V2 Python client. Live trading requires a Polygon wallet private key and CLOB API credentials. Keep them only in the server's `.env` file.

Do not put a private key in chat, GitHub, screenshots, or the dashboard.

## Strategy interpretation

"Price above 75 cents" is implemented as the best executable ASK > 0.75.

"Below 49 cents" is implemented as the best executable BID < 0.49.

"Not more than 3 minutes left" means `0 < seconds_to_expiry <= 180`.

The bot uses FOK for the $5 entry so the entry is either filled immediately or cancelled, and FAK for the emergency sell so available liquidity is taken immediately and any remainder is cancelled.

## Files

- `bot.py` — trading engine
- `.env.example` — configuration template
- `requirements.txt` — dependencies
- `trades.jsonl` — created automatically when the bot runs


## Reviewed-build safety checks

- Live BUY uses FOK and the observed best ask as a worst-price limit.
- Live STOP SELL uses FAK and the observed best bid as a worst-price limit.
- A live BUY is recorded only when the CLOB reports `success=true`,
  `status=matched`, and positive `takingAmount`/`makingAmount`.
- A failed/rejected FOK cannot create a fake local position.
- A partial FAK exit keeps the remaining position tracked.
- Position state is persisted to `position_state.json`.
- The market selector no longer falls back to an arbitrary active market.
- Paper mode remains the default.
- The bot polls approximately once per second; it is not sub-second.
