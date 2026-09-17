
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")

CAPITAL_USD = float(os.getenv("CAPITAL_USD", "5"))
ENTRY_PRICE = float(os.getenv("ENTRY_PRICE", "0.75"))
STOP_PRICE = float(os.getenv("STOP_PRICE", "0.49"))
MAX_SECONDS_LEFT = int(os.getenv("MAX_SECONDS_LEFT", "180"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
LOG_FILE = Path(os.getenv("LOG_FILE", "trades.jsonl"))
STATE_FILE = Path(os.getenv("STATE_FILE", "position_state.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

session = requests.Session()
session.headers.update({"User-Agent": "BTC-5m-Polymarket-Bot/1.0"})


@dataclass
class Position:
    condition_id: str
    token_id: str
    outcome: str
    shares: float
    entry_price: float
    entry_cost: float
    market_end: float
    paper: bool


def utc_now() -> float:
    return time.time()


def parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def get_current_event():
    # Polymarket's BTC 5m event slugs use the Unix timestamp of the
    # beginning of the 5-minute interval.
    interval_start = int(utc_now() // 300) * 300
    slug = f"btc-updown-5m-{interval_start}"

    r = session.get(f"{GAMMA_URL}/events/slug/{slug}", timeout=5)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def select_market(event):
    markets = event.get("markets") or []
    candidates = []
    for market in markets:
        question = (market.get("question") or "").lower()
        if (
            market.get("active") is True
            and market.get("closed") is not True
            and market.get("enableOrderBook") is not False
            and "bitcoin" in question
            and "up" in question
            and "down" in question
        ):
            candidates.append(market)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logging.warning("Multiple BTC Up/Down markets found; skipping event")
    return None

def market_end_timestamp(market, event):
    raw = market.get("endDate") or market.get("endDateIso") or event.get("endDate")
    if not raw:
        raise ValueError("Market has no endDate")

    if isinstance(raw, (int, float)):
        return float(raw)

    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def get_tokens_and_prices(market):
    outcomes = parse_json_field(market.get("outcomes")) or []
    prices = parse_json_field(market.get("outcomePrices")) or []
    token_ids = parse_json_field(market.get("clobTokenIds")) or []

    if not (len(outcomes) == len(prices) == len(token_ids)):
        raise ValueError("outcomes/outcomePrices/clobTokenIds length mismatch")

    result = []
    for outcome, price, token_id in zip(outcomes, prices, token_ids):
        try:
            result.append(
                {
                    "outcome": str(outcome),
                    "display_price": float(price),
                    "token_id": str(token_id),
                }
            )
        except (TypeError, ValueError):
            continue
    return result


def get_best_prices(token_id):
    # Public CLOB orderbook endpoint.
    r = session.get(
        f"{CLOB_URL}/book",
        params={"token_id": token_id},
        timeout=3,
    )
    r.raise_for_status()
    book = r.json()

    bids = book.get("bids") or []
    asks = book.get("asks") or []

    def price(level):
        if isinstance(level, dict):
            return float(level["price"])
        return float(level[0])

    best_bid = max((price(x) for x in bids), default=None)
    best_ask = min((price(x) for x in asks), default=None)
    return best_bid, best_ask


def log_trade(event_type, **data):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def save_state(positions):
    payload = {}
    for key, pos in positions.items():
        payload[key] = {
            "condition_id": pos.condition_id,
            "token_id": pos.token_id,
            "outcome": pos.outcome,
            "shares": pos.shares,
            "entry_price": pos.entry_price,
            "entry_cost": pos.entry_cost,
            "market_end": pos.market_end,
            "paper": pos.paper,
        }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            key: Position(
                condition_id=v["condition_id"],
                token_id=v["token_id"],
                outcome=v["outcome"],
                shares=float(v["shares"]),
                entry_price=float(v["entry_price"]),
                entry_cost=float(v["entry_cost"]),
                market_end=float(v["market_end"]),
                paper=bool(v["paper"]),
            )
            for key, v in raw.items()
        }
    except Exception as exc:
        logging.error("Could not load position state: %s", exc)
        raise RuntimeError("Position state is unreadable; refusing to trade.")


class Trader:
    def __init__(self):
        self.client = None
        self.positions = load_state()
        if self.positions:
            logging.warning("Recovered %d tracked position(s).", len(self.positions))

        if LIVE_TRADING:
            if any(pos.paper for pos in self.positions.values()):
                raise RuntimeError("Live mode found paper positions in state; refusing to trade.")
            self._init_live_client()

    def _init_live_client(self):
        # Official Polymarket CLOB V2 Python client.
        from py_clob_client_v2 import (
            ApiCreds,
            ClobClient,
            MarketOrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        pk = os.getenv("PK")
        if not pk:
            raise RuntimeError("LIVE_TRADING=true but PK is missing")

        creds = None
        if os.getenv("CLOB_API_KEY"):
            creds = ApiCreds(
                api_key=os.environ["CLOB_API_KEY"],
                api_secret=os.environ["CLOB_SECRET"],
                api_passphrase=os.environ["CLOB_PASS_PHRASE"],
            )

        self.client = ClobClient(
            host=CLOB_URL,
            chain_id=int(os.getenv("CHAIN_ID", "137")),
            key=pk,
            creds=creds,
        )

        if creds is None:
            creds = self.client.create_or_derive_api_key()
            self.client = ClobClient(
                host=CLOB_URL,
                chain_id=int(os.getenv("CHAIN_ID", "137")),
                key=pk,
                creds=creds,
            )

        self.OrderType = OrderType
        self.PartialCreateOrderOptions = PartialCreateOrderOptions
        self.MarketOrderArgs = MarketOrderArgs
        self.Side = Side

        logging.info("LIVE TRADING ENABLED")

    def buy(self, market, token, ask, end_ts):
        key = market["conditionId"]

        if key in self.positions:
            return

        # $5 means $5 of USDC, not $5 worth of shares.
        if LIVE_TRADING:
            response = self.client.create_and_post_market_order(
                order_args=self.MarketOrderArgs(
                    token_id=token["token_id"],
                    amount=CAPITAL_USD,
                    side=self.Side.BUY,
                    price=ask,
                    order_type=self.OrderType.FOK,
                ),
                options=self.PartialCreateOrderOptions(tick_size="0.01"),
                order_type=self.OrderType.FOK,
            )
            logging.info("BUY response: %s", response)

            if not response.get("success") or response.get("status") != "matched":
                logging.warning("BUY not fully matched; no position recorded.")
                return

            shares = float(response.get("takingAmount") or 0)
            actual_cost = float(response.get("makingAmount") or 0)
            if shares <= 0 or actual_cost <= 0:
                logging.error("BUY response contained no valid fill; no position recorded.")
                return

            entry = actual_cost / shares
            paper = False
        else:
            shares = CAPITAL_USD / ask
            actual_cost = CAPITAL_USD
            entry = ask
            paper = True

        self.positions[key] = Position(
            condition_id=key,
            token_id=token["token_id"],
            outcome=token["outcome"],
            shares=shares,
            entry_price=entry,
            entry_cost=actual_cost,
            market_end=end_ts,
            paper=paper,
        )

        save_state(self.positions)

        log_trade(
            "BUY",
            mode="LIVE" if LIVE_TRADING else "PAPER",
            condition_id=key,
            outcome=token["outcome"],
            token_id=token["token_id"],
            shares=shares,
            price=entry,
            cost=actual_cost,
            seconds_left=max(0, end_ts - utc_now()),
        )

        logging.info(
            "BOUGHT %s | %.4f shares @ %.4f | cost $%.2f",
            token["outcome"],
            shares,
            entry,
            actual_cost,
        )

    def sell(self, position, bid):
        if position.paper:
            proceeds = position.shares * bid
            log_trade(
                "SELL",
                mode="PAPER",
                condition_id=position.condition_id,
                outcome=position.outcome,
                token_id=position.token_id,
                shares=position.shares,
                price=bid,
                proceeds=proceeds,
                pnl=proceeds - position.entry_cost,
                reason="STOP",
            )
            logging.info(
                "PAPER STOP SELL | %s | %.4f @ %.4f | P&L %.4f",
                position.outcome,
                position.shares,
                bid,
                proceeds - position.entry_cost,
            )
            self.positions.pop(position.condition_id, None)
            return

        # FAK is used for the emergency exit: execute immediately against
        # available bids and cancel any unfilled remainder.
        response = self.client.create_and_post_market_order(
            order_args=self.MarketOrderArgs(
                token_id=position.token_id,
                amount=position.shares,
                side=self.Side.SELL,
                price=bid,
                order_type=self.OrderType.FAK,
            ),
            options=self.PartialCreateOrderOptions(tick_size="0.01"),
            order_type=self.OrderType.FAK,
        )
        logging.warning("STOP SELL response: %s", response)

        sold = float(response.get("takingAmount") or 0)
        proceeds = float(response.get("makingAmount") or 0)

        log_trade(
            "SELL",
            mode="LIVE",
            condition_id=position.condition_id,
            outcome=position.outcome,
            token_id=position.token_id,
            requested_shares=position.shares,
            sold_shares=sold,
            observed_bid=bid,
            proceeds=proceeds,
            response=response,
            reason="STOP",
        )

        remaining = max(0.0, position.shares - sold)
        if remaining <= 0.0001:
            self.positions.pop(position.condition_id, None)
        elif sold > 0:
            position.shares = remaining

        save_state(self.positions)

    def settle(self, condition_id):
        # No order is sent at expiry. The winning shares resolve according
        # to the market. The position is removed from local tracking.
        position = self.positions.pop(condition_id, None)
        if position:
            log_trade(
                "EXPIRY",
                mode="LIVE" if not position.paper else "PAPER",
                condition_id=condition_id,
                outcome=position.outcome,
                token_id=position.token_id,
                shares=position.shares,
                entry_price=position.entry_price,
            )
            logging.info(
                "HELD TO EXPIRY | %s | %.4f shares",
                position.outcome,
                position.shares,
            )
        save_state(self.positions)


def run():
    logging.info(
        "BTC 5m bot started | capital=$%.2f | entry>%.2f | stop<%.2f | "
        "max_seconds_left=%d | live=%s",
        CAPITAL_USD,
        ENTRY_PRICE,
        STOP_PRICE,
        MAX_SECONDS_LEFT,
        LIVE_TRADING,
    )

    trader = Trader()

    while True:
        try:
            event = get_current_event()
            if not event:
                time.sleep(POLL_SECONDS)
                continue

            market = select_market(event)
            if not market:
                time.sleep(POLL_SECONDS)
                continue

            end_ts = market_end_timestamp(market, event)
            seconds_left = end_ts - utc_now()

            # We only enter during the final 3 minutes, and never after expiry.
            if 0 < seconds_left <= MAX_SECONDS_LEFT:
                tokens = get_tokens_and_prices(market)

                for token in tokens:
                    try:
                        bid, ask = get_best_prices(token["token_id"])
                    except Exception as exc:
                        logging.warning("Orderbook error: %s", exc)
                        continue

                    condition_id = market.get("conditionId")
                    if not condition_id:
                        logging.warning("Market has no conditionId; skipping")
                        continue

                    if condition_id in trader.positions:
                        position = trader.positions[condition_id]
                        if position.token_id == token["token_id"] and bid is not None:
                            if bid < STOP_PRICE:
                                trader.sell(position, bid)
                        continue

                    if ask is not None and ask > ENTRY_PRICE:
                        trader.buy(market, token, ask, end_ts)
                        break


                # Expiry cleanup.
                if seconds_left <= 0.5:
                    trader.settle(market["conditionId"])

            elif seconds_left <= 0:
                trader.settle(market["conditionId"])

        except KeyboardInterrupt:
            logging.info("Stopped by user")
            return
        except Exception:
            logging.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
