"""
Coinbase Global Derivatives - where Coinbase perpetuals have lived since
1 October 2026.

On that date Coinbase moved international derivatives off INTX and onto a
Deribit-powered gateway, "with no parallel run". The gateway is JSON-RPC 2.0
at https://drb.coinbase.com/api/v2. Spot is untouched and stays on the
Advanced Trade REST API in coinbase.py.

WHY A ccxt.deribit SUBCLASS, AND NOT A CLIENT WRITTEN FROM SCRATCH
    ccxt has no support for this gateway - not in the pinned 4.5.58, and not
    on master either (checked at 4.5.87). But the gateway IS Deribit's API v2,
    method for method, and ccxt already ships a complete Deribit client. Only
    two things differ, and they are the only two things overridden here:

      host   drb.coinbase.com instead of www.deribit.com
      auth   a Bearer token from public/auth(grant_type=coinbase_cdp),
             instead of Deribit's per-request deri-hmac-sha256 signature

    Everything else - market loading, order parsing, error mapping - is
    ccxt's, which is what makes the orders come back in the same unified shape
    coinbase.closed_orders() already cleans for spot. Verified live against the
    gateway: load_markets, account summaries, positions, order history and
    user trades all work unchanged.

AUTH
    The participant's EXISTING CDP key works; Coinbase issues nothing new. A
    short-lived CDP JWT is exchanged at public/auth for a 900-second access
    token. ccxt.coinbase already builds CDP JWTs for both key types Coinbase
    issues - Ed25519 and ES256 PEM - so that is reused rather than
    re-implemented. There is no refresh token; a new exchange is cheap.

    The auth response also returns the key's granted `scope` - 'rat#view' for a
    view-only key - and the portfolio it is bound to. Both are kept: the scope
    is recorded at signup, and the portfolio is checked against the one stored
    for the credential.

THREE THINGS ABOUT THIS API THAT FAIL SILENTLY IF YOU GET THEM WRONG
    1. Valuation. Every per-currency summary carries a nonzero margin_balance
       even for currencies the account holds none of - it is the SAME
       collateral re-expressed in that currency's units. Summing it across
       currencies overstated a $19.95 account as $622.05. The account's equity
       is total_equity_usd, an account-level figure.

    2. History. `historical=false` (the default) returns only "orders for 30
       min, trades for 24h"; `historical=true` returns indexed history but
       "Recent data is not included." Neither alone is complete, so an hourly
       sync on the default would silently miss every order older than 30
       minutes. Both are fetched and merged.

    3. The migration itself. Each account's INTX balance arrived as a
       transaction-log `correction` with reason 'coinbase_migration', and each
       open position was recreated by a zero-fee block trade under a second
       later. Those are not trades anyone made; see drop_migration_artifacts().
"""

import logging
from collections import Counter
from datetime import datetime, timezone
from itertools import pairwise

import ccxt

from venue_common import (USD_EQUIVALENTS, install_shared_rate_limit,
                          load_shared_markets, resolve_since)

logger = logging.getLogger(__name__)

GATEWAY = "https://drb.coinbase.com"

# ccxt id for the gateway client. Distinct from 'coinbase' on purpose: the
# shared market and throttle caches in venue_common are keyed by exchange id,
# and the gateway's 5,900-instrument market map and rate budget have nothing
# in common with spot's.
EXCHANGE_ID = "coinbasederivatives"

# The cut-over day, UTC. Migration artifacts can only exist on it.
MIGRATION_DAY_START_MS = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
MIGRATION_DAY_END_MS = MIGRATION_DAY_START_MS + 86_400_000

# How long after an account's migration correction a recreated position's
# block trade can land. Observed: 757 ms. A minute is generous for several
# positions, and still far shorter than any participant could have traded on
# an account that was mid-migration with its open orders cancelled.
MIGRATION_ARTIFACT_WINDOW_MS = 60_000

# Refresh the access token this long before it expires, so a call never goes
# out on a token that lapses in flight.
_TOKEN_REFRESH_MARGIN_MS = 60_000

# Orders per history request. The gateway allows up to 1000.
_ORDER_PAGE = 500

# Backstop against an endpoint that ignores `offset` and re-serves one page.
_MAX_ORDER_PAGES = 200

# How far behind the resume point every run re-reads order history.
#
# The two history modes leave a seam: historical=false covers only the last
# 30 minutes, and historical=true has orders "available after a short delay
# due to indexing". An order older than 30 minutes but not yet indexed is in
# NEITHER at sync time - and if a newer order is stored in the same run, the
# resume point moves past it and it is never asked for again.
#
# Re-reading a window behind the resume point closes that seam for any
# indexing delay shorter than the window. It is cheap: trade_metrics upserts
# on order_id, so an order seen twice is simply rewritten, and paging still
# stops at the first page older than the window.
_REREAD_WINDOW_MS = 6 * 3_600_000

# The marker the gateway grants a view-only key. Anything else in the scope
# string is logged, not rejected: the vocabulary is Coinbase's and only this
# value has been observed. The REST permissions check in
# coinbase.verify_credential (can_trade / can_transfer) remains the control,
# exactly as LIGHTER_ENFORCE_SCOPE stays off until real values are seen.
VIEW_ONLY_SCOPE_SUFFIX = "#view"


def cdp_key_is_ed25519(secret: str) -> bool:
    """
    Whether a CDP secret is an Ed25519 key (signed EdDSA) rather than an ES256
    PEM. The same test ccxt.coinbase.sign() applies to REST calls, so the two
    halves of the venue can never disagree about a key's type.
    """
    return len(secret) == 88 or secret.endswith("=")


class coinbasederivatives(ccxt.deribit):
    """ccxt's Deribit client, pointed at Coinbase's gateway with Coinbase auth."""

    def describe(self):
        return self.deep_extend(super().describe(), {
            "id": EXCHANGE_ID,
            "name": "Coinbase Global Derivatives",
            "urls": {"api": {"rest": GATEWAY}},
        })

    def __init__(self, config=None):
        super().__init__(config or {})
        # Used only to mint the CDP JWT for the token exchange. It is never
        # asked to make a request.
        self._cdp = ccxt.coinbase({"apiKey": self.apiKey, "secret": self.secret})
        self._token = None
        self._token_expires_ms = 0
        self.granted_scope = None
        self.gateway_portfolio_uuid = None
        # The portfolio stored for this credential at signup, if known. A
        # different one coming back from the gateway means the key no longer
        # points where it did when it was verified.
        self.expected_portfolio_uuid = None

    def fetch_markets(self, params={}):
        """
        Perpetuals and dated futures only - Deribit files both under
        kind='future'.

        Unfiltered, the gateway lists ~5,900 instruments, almost all of them
        options, which Coinbase says are "not yet available through the
        gateway". Every one was downloaded and parsed on each process start
        to use 144 perps: filtering cut it to ~220 instruments, with the same
        perps, and parsing from 0.31s to 0.02s.

        Should options open up, an options order would still be recorded -
        parse_order falls back to the raw instrument name as its symbol.
        """
        return super().fetch_markets(self.extend({"kind": "future"}, params))

    def access_token(self) -> str:
        """A live Bearer token, exchanging the CDP key for a new one if needed."""
        if self._token and self.milliseconds() < self._token_expires_ms - _TOKEN_REFRESH_MARGIN_MS:
            return self._token

        url = self.urls["api"]["rest"] + "/api/" + self.version + "/public/auth"
        jwt = self._cdp.create_auth_token(
            self.seconds(), "POST", url, cdp_key_is_ed25519(self._cdp.secret))

        # POST with the credential in the body: the gateway refuses
        # public/auth any other way, and it keeps the JWT out of URLs and logs.
        response = self.fetch(url, "POST", {"Content-Type": "application/json"},
                              self.json({"jsonrpc": "2.0", "id": 1,
                                         "method": "public/auth",
                                         "params": {"grant_type": "coinbase_cdp",
                                                    "token": jwt}}))

        result = response.get("result") if isinstance(response, dict) else None
        token = self.safe_string(result or {}, "access_token")
        if not token:
            raise ccxt.AuthenticationError(
                f"{self.id} public/auth returned no access token: "
                f"{self.json((response or {}).get('error'))}")

        self._token = token
        self._token_expires_ms = (self.milliseconds()
                                  + self.safe_integer(result, "expires_in", 900) * 1000)
        self.granted_scope = self.safe_string(result, "scope")
        self.gateway_portfolio_uuid = self.safe_string(result, "portfolio_uuid")

        if (self.expected_portfolio_uuid and self.gateway_portfolio_uuid
                and self.gateway_portfolio_uuid != self.expected_portfolio_uuid):
            logger.warning(
                "Derivatives gateway authenticated this key against portfolio "
                "%s, not the %s stored for it at signup - the data recorded is "
                "for the gateway's portfolio",
                self.gateway_portfolio_uuid, self.expected_portfolio_uuid)

        return self._token

    def sign(self, path, api="public", method="GET", params={}, headers=None, body=None):
        if api != "private":
            return super().sign(path, api, method, params, headers, body)

        request = "/api/" + self.version + "/private/" + path
        if params:
            request += "?" + self.urlencode(params)
        return {"url": self.urls["api"]["rest"] + request, "method": method,
                "body": body,
                "headers": {"Authorization": "Bearer " + self.access_token()}}


def build_exchange(api_key: str, api_secret: str,
                   portfolio_uuid: str = None) -> coinbasederivatives:
    """A gateway client for one credential, paced against every other one."""
    if not api_key or not api_secret:
        raise ValueError("Both api_key and api_secret are required")

    exchange = coinbasederivatives({"apiKey": api_key, "secret": api_secret,
                                    "enableRateLimit": True})
    exchange.expected_portfolio_uuid = portfolio_uuid
    install_shared_rate_limit(exchange)
    return exchange


def is_gateway(exchange) -> bool:
    return isinstance(exchange, coinbasederivatives)


def scope_is_view_only(scope: str) -> bool:
    """True when every granted scope token is a view scope."""
    tokens = (scope or "").split()
    return bool(tokens) and all(t.endswith(VIEW_ONLY_SCOPE_SUFFIX) for t in tokens)


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def _number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def account_equity_usd(summaries: list[dict]) -> float:
    """
    The account's total equity from get_account_summaries.

    total_equity_usd is an ACCOUNT-level figure, repeated identically on every
    currency summary that carries margin and reported as 0 on the rest. So the
    answer is the common nonzero value - not the max, which would misreport an
    account in deficit as zero, and certainly not a sum of margin_balance,
    which counts the same collateral once per listed currency.

    It is also continuous with history: INTX's total_balance, which every perp
    snapshot before the migration recorded, matched the gateway's
    total_equity_usd to the last decimal place.
    """
    values = [v for v in (_number(s.get("total_equity_usd")) for s in summaries)
              if v is not None]

    if values:
        nonzero = [v for v in values if abs(v) > 1e-9]
        if not nonzero:
            return 0.0
        first = nonzero[0]
        if any(abs(v - first) > 1e-6 * max(1.0, abs(first)) for v in nonzero):
            common = Counter(round(v, 6) for v in nonzero).most_common(1)[0][0]
            logger.warning(
                "total_equity_usd differs between currency summaries (%s) - "
                "using the most common value, %s",
                sorted({round(v, 6) for v in nonzero}), common)
            return common
        return first

    # The field is documented but absent. USD-equivalent equity can still be
    # summed exactly; anything else can't be valued here, and an undervalued
    # snapshot is worse than a missing one - a missing point is carried
    # forward, a wrong one becomes a fabricated return.
    unpriced = [s.get("currency") for s in summaries
                if s.get("currency") not in USD_EQUIVALENTS
                and abs(_number(s.get("equity")) or 0) > 1e-9]
    if unpriced:
        raise ValueError(
            f"Account summaries carry no total_equity_usd and hold equity in "
            f"{sorted(unpriced)}, which cannot be valued without it")

    return sum(_number(s.get("equity")) or 0.0 for s in summaries
               if s.get("currency") in USD_EQUIVALENTS)


def get_account_value(exchange: coinbasederivatives) -> dict:
    """
    Value a derivatives account in ONE request.

    Returns the same shape as every other venue's valuation - a 'total_usdc'
    headline plus detail - so balance_snapshots stores it identically. USD and
    USDC are treated as 1:1 here as everywhere else in the system.
    """
    response = exchange.privateGetGetAccountSummaries({"extended": "true"})
    summaries = (response.get("result") or {}).get("summaries") or []
    timestamp = exchange.milliseconds()

    usdc = next((s for s in summaries if s.get("currency") == "USDC"), {})

    return {
        "timestamp": timestamp,
        "datetime": exchange.iso8601(timestamp),
        "account_type": "perp",
        "total_usdc": account_equity_usd(summaries),
        "margin_balance_usdc": _number(usdc.get("margin_balance")),
        "available_funds_usdc": _number(usdc.get("available_funds")),
        "initial_margin_usdc": _number(usdc.get("initial_margin")),
        "maintenance_margin_usdc": _number(usdc.get("maintenance_margin")),
        "unrealized_pnl_usdc": _number(usdc.get("session_upl")),
        # Marks where the source changed, so a reader of balance_snapshots can
        # see the switch from INTX without consulting git history.
        "source": "coinbase_derivatives_gateway",
    }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def settlement_currencies(exchange: coinbasederivatives) -> list[str]:
    """
    Every currency a perpetual on the gateway settles in.

    Order history is queried per settlement currency, and not every perp
    settles in USDC: alongside 134 linear USDC perps the gateway lists inverse
    BTC and ETH perps, whose orders only appear under BTC and ETH. Derived from
    the market map so a newly listed settlement currency is picked up without
    a code change.
    """
    load_shared_markets(exchange)
    found = {m.get("settleId") or m.get("settle")
             for m in exchange.markets.values() if m.get("swap")}
    return sorted(c for c in found if c) or ["USDC"]


def _raw_last_touched(raw: dict) -> int:
    """Latest of creation and last update, on a raw gateway order."""
    return max(int(_number(raw.get("creation_timestamp")) or 0),
               int(_number(raw.get("last_update_timestamp")) or 0))


def _last_touched(order: dict) -> int:
    """
    Latest moment a unified order changed.

    Filtering on creation time alone would lose a resting limit order placed
    before the last sync and filled after it: its creation is behind the
    resume point, so it would never be fetched again. trade_metrics upserts on
    order_id, so re-reading an order is harmless and missing one is not.
    """
    return max(order.get("timestamp") or 0, order.get("lastTradeTimestamp") or 0,
               _raw_last_touched(order.get("info") or {}))


def _newest_first(raw_page: list[dict]) -> bool:
    """
    Whether a raw page is sorted newest-first by last update.

    The gateway does not document its sort order, so it is read off the data
    rather than assumed. Early stopping is only safe when this holds; if it
    doesn't, paging simply continues to the end - slower, never wrong.
    """
    stamps = [_raw_last_touched(o) for o in raw_page]
    return len(stamps) >= 2 and all(a >= b for a, b in pairwise(stamps))


def _order_history(exchange, currency: str, historical: bool, since: int,
                   page_size: int) -> list[dict]:
    collected: list[dict] = []
    for page_number in range(_MAX_ORDER_PAGES):
        response = exchange.privateGetGetOrderHistoryByCurrency({
            "currency": currency,
            "count": page_size,
            "offset": page_number * page_size,
            "historical": "true" if historical else "false",
        })
        raw = response.get("result") or []
        if isinstance(raw, dict):           # the with_continuation shape
            raw = raw.get("orders") or []
        if not raw:
            break

        collected.extend(exchange.parse_orders(raw))

        if len(raw) < page_size:
            break
        if _newest_first(raw) and _raw_last_touched(raw[-1]) < since:
            break
    else:
        logger.warning("Stopped paging %s %s order history after %d pages - "
                       "history may be incomplete", currency,
                       "historical" if historical else "recent", _MAX_ORDER_PAGES)
    return collected


def _is_migration_entry(entry: dict) -> bool:
    info = entry.get("info")
    if isinstance(info, dict):
        return info.get("reason") == "coinbase_migration"
    return "coinbase_migration" in str(info or "")


def drop_migration_artifacts(exchange, orders: list[dict], since: int) -> list[dict]:
    """
    Remove the block trades that recreated open positions at the migration.

    Those positions were opened by real orders on INTX, which the old code
    path already stored. Keeping the recreations would record every position
    open on 1 October twice - once when it was actually traded, and again as a
    zero-fee "buy" on migration day.

    Identified exactly rather than by heuristic: each account's migration is a
    transaction-log correction with reason 'coinbase_migration', and the
    recreating trades follow it within a second. Costs one request per
    settlement currency, and only on a sync whose window includes 1 October -
    in practice once per credential, on its first run after this change.
    """
    if since >= MIGRATION_DAY_END_MS:
        return orders

    candidates = [o for o in orders
                  if MIGRATION_DAY_START_MS <= (o.get("timestamp") or 0) < MIGRATION_DAY_END_MS]
    if not candidates:
        return orders

    currencies = set()
    for order in candidates:
        market = exchange.safe_market(order.get("symbol")) if order.get("symbol") else {}
        currencies.add((market or {}).get("settleId") or "USDC")

    moments = []
    for currency in sorted(currencies):
        response = exchange.privateGetGetTransactionLog({
            "currency": currency,
            "start_timestamp": MIGRATION_DAY_START_MS,
            "end_timestamp": MIGRATION_DAY_END_MS,
            "query": "correction",
            "count": 1000,
        })
        for entry in (response.get("result") or {}).get("logs") or []:
            if _is_migration_entry(entry):
                moments.append(int(_number(entry.get("timestamp")) or 0))

    if not moments:
        return orders

    def is_artifact(order):
        created = order.get("timestamp") or 0
        return any(m <= created <= m + MIGRATION_ARTIFACT_WINDOW_MS for m in moments)

    kept = [o for o in orders if not is_artifact(o)]
    if len(kept) != len(orders):
        logger.info("Dropped %d order(s) that recreated positions at the "
                    "1 October migration", len(orders) - len(kept))
    return kept


def closed_orders(exchange: coinbasederivatives, since=None,
                  page_size: int = _ORDER_PAGE) -> list[dict]:
    """
    Every order with a fill since `since`, as ccxt unified orders.

    Both history modes, every settlement currency, merged on order id - see
    the module docstring for why neither mode alone is complete. Where both
    modes return the same order, the more recently updated copy wins.

    A `since` that is a resume point is widened by _REREAD_WINDOW_MS. With no
    `since` there is nothing to widen - the read starts at COMPETITION_START.
    """
    is_resume_point = since is not None
    since = resolve_since(exchange, since)
    if is_resume_point:
        since -= _REREAD_WINDOW_MS
    load_shared_markets(exchange)

    merged: dict = {}
    for currency in settlement_currencies(exchange):
        for historical in (True, False):
            for order in _order_history(exchange, currency, historical, since, page_size):
                existing = merged.get(order["id"])
                if existing is None or _last_touched(order) >= _last_touched(existing):
                    merged[order["id"]] = order

    orders = [o for o in merged.values() if _last_touched(o) >= since]
    orders = drop_migration_artifacts(exchange, orders, since)
    return sorted(orders, key=lambda o: o.get("timestamp") or 0)
