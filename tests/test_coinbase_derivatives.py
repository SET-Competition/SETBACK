"""
Tests for the Coinbase derivatives gateway adapter.

Offline. Markets are loaded through ccxt's REAL Deribit parsing from captured
public instrument data (tests/fixtures/coinbase_derivatives_markets.json),
and orders are parsed by ccxt's real parse_order - so what is under test is
our logic on top of ccxt's actual behaviour, not on top of a stand-in.

Account summaries and orders are written by hand in the shapes observed live
on 2026-10-13/14, with no real identifiers.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

coinbase_derivatives = pytest.importorskip("coinbase_derivatives")
coinbase = pytest.importorskip("coinbase")
venue_common = pytest.importorskip("venue_common")

FIXTURE = json.loads((Path(__file__).parent / "fixtures"
                      / "coinbase_derivatives_markets.json").read_text(encoding="utf-8"))

# A throwaway Ed25519-shaped CDP secret: 64 random bytes, 88 base64 chars.
ED25519_SECRET = base64.b64encode(os.urandom(64)).decode()
KEY_NAME = "organizations/test-org/apiKeys/test-key"


def ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _cold_caches():
    venue_common.clear_shared_markets()
    venue_common.clear_shared_throttles()
    yield
    venue_common.clear_shared_markets()
    venue_common.clear_shared_throttles()


class FakeGateway(coinbase_derivatives.coinbasederivatives):
    """
    The gateway client with its transport replaced by fixtures.

    The overrides use ccxt's SNAKE_CASE names on purpose. ccxt's constructor
    walks every snake_case method and copies it onto the camelCase name ON THE
    CLASS - so overriding privateGetGetAccountSummaries directly is silently
    undone the moment an instance is built, and the "fake" goes to the real
    gateway. That is exactly what the first version of these tests did.
    fetch() is also closed off, so anything that slips past fails loudly
    instead of reaching the network.
    """

    def __init__(self, summaries=None, history=None, logs=None):
        super().__init__({"apiKey": KEY_NAME, "secret": ED25519_SECRET})
        self.calls = []
        self._summaries = summaries or []
        self._history = history or {}       # (currency, historical) -> raw orders
        self._logs = logs or {}             # currency -> transaction-log entries

    def fetch(self, url, method="GET", headers=None, body=None):
        raise AssertionError(f"test reached the network: {method} {url}")

    def public_get_get_instruments(self, params={}):
        self.calls.append(("instruments", dict(params)))
        return {"result": FIXTURE["instruments"]}

    def public_get_get_currencies(self, params={}):
        return {"result": FIXTURE["currencies"]}

    def private_get_get_account_summaries(self, params={}):
        self.calls.append(("summaries", dict(params)))
        return {"result": {"summaries": self._summaries}}

    def private_get_get_order_history_by_currency(self, params={}):
        self.calls.append(("history", dict(params)))
        rows = self._history.get((params["currency"], params["historical"] == "true"), [])
        offset, count = int(params["offset"]), int(params["count"])
        return {"result": rows[offset:offset + count]}

    def private_get_get_transaction_log(self, params={}):
        self.calls.append(("log", dict(params)))
        return {"result": {"logs": self._logs.get(params["currency"], [])}}

    def history_calls(self):
        return [p for name, p in self.calls if name == "history"]


def raw_order(order_id, created, updated=None, instrument="PUMP_USDC-PERPETUAL",
              filled=5542.0, price=0.00552, commission=0.0012, direction="buy"):
    return {
        "order_id": order_id, "instrument_name": instrument, "direction": direction,
        "order_type": "limit", "order_state": "filled", "label": "", "api": True,
        "creation_timestamp": created, "last_update_timestamp": updated or created,
        "amount": filled, "filled_amount": filled, "average_price": price,
        "price": price, "commission": commission, "time_in_force": "good_til_cancelled",
        "post_only": False, "reduce_only": False,
    }


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

# The live shape: total_equity_usd repeated on every summary carrying margin,
# "0" elsewhere, and a nonzero margin_balance on currencies the account holds
# none of - the same collateral re-expressed in each currency's units.
SUMMARIES = [
    {"currency": "BTC", "equity": "0.0", "margin_balance": "0.00023745",
     "total_equity_usd": "19.850429434"},
    {"currency": "USDC", "equity": "19.85440031", "margin_balance": "19.85440031",
     "available_funds": "14.27026156", "initial_margin": "5.5896612",
     "maintenance_margin": "0.74528817", "session_upl": "-0.238306",
     "total_equity_usd": "19.850429434"},
    {"currency": "DOGE", "equity": "0.0", "margin_balance": "234.2581",
     "total_equity_usd": "19.850429434"},
    {"currency": "BCH", "equity": "0.0", "margin_balance": "0.0",
     "total_equity_usd": "0"},
]


def test_equity_is_the_account_level_figure_not_a_sum():
    """
    The trap this function exists for. Summing margin_balance across
    currencies turned a $19.95 account into $622.05 live; here it would read
    $254 for a $19.85 account.
    """
    assert coinbase_derivatives.account_equity_usd(SUMMARIES) == pytest.approx(19.850429434)
    assert sum(float(s["margin_balance"]) for s in SUMMARIES) > 250


def test_an_account_in_deficit_is_not_reported_as_zero():
    """Why the rule is 'the common nonzero value' and not max()."""
    negative = [{"currency": "USDC", "total_equity_usd": "-3.5"},
                {"currency": "BCH", "total_equity_usd": "0"}]
    assert coinbase_derivatives.account_equity_usd(negative) == pytest.approx(-3.5)


def test_an_empty_account_is_worth_zero():
    empty = [{"currency": "USDC", "total_equity_usd": "0"},
             {"currency": "BTC", "total_equity_usd": "0"}]
    assert coinbase_derivatives.account_equity_usd(empty) == 0.0


def test_disagreeing_totals_use_the_most_common_and_say_so(caplog):
    odd = [{"currency": c, "total_equity_usd": v}
           for c, v in (("USDC", "10.0"), ("BTC", "10.0"), ("ETH", "11.0"))]
    with caplog.at_level(logging.WARNING):
        assert coinbase_derivatives.account_equity_usd(odd) == pytest.approx(10.0)
    assert "differs between currency summaries" in caplog.text


def test_without_the_field_usd_equivalents_are_summed():
    legacy = [{"currency": "USDC", "equity": "12.5"},
              {"currency": "USDT", "equity": "2.5"},
              {"currency": "BTC", "equity": "0"}]
    assert coinbase_derivatives.account_equity_usd(legacy) == pytest.approx(15.0)


def test_without_the_field_non_usd_equity_refuses_rather_than_undervalues():
    """
    A wrong snapshot becomes a fabricated return; a missing one is carried
    forward. So this raises.
    """
    with pytest.raises(ValueError, match="cannot be valued"):
        coinbase_derivatives.account_equity_usd(
            [{"currency": "USDC", "equity": "10"}, {"currency": "BTC", "equity": "0.01"}])


def test_get_account_value_costs_one_request_and_marks_its_source():
    gateway = FakeGateway(summaries=SUMMARIES)
    value = coinbase_derivatives.get_account_value(gateway)

    assert value["total_usdc"] == pytest.approx(19.850429434)
    assert value["account_type"] == "perp"
    assert value["source"] == "coinbase_derivatives_gateway"
    assert value["unrealized_pnl_usdc"] == pytest.approx(-0.238306)
    assert [name for name, _ in gateway.calls] == ["summaries"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _gateway_with_auth(response, clock):
    gateway = coinbase_derivatives.coinbasederivatives(
        {"apiKey": KEY_NAME, "secret": ED25519_SECRET})
    posts, minted = [], []

    def fake_fetch(url, method="GET", headers=None, body=None):
        posts.append({"url": url, "method": method, "body": json.loads(body)})
        return response

    def fake_mint(seconds, method, url, use_eddsa):
        minted.append({"method": method, "url": url, "use_eddsa": use_eddsa})
        return "minted-jwt"

    gateway.fetch = fake_fetch
    gateway._cdp.create_auth_token = fake_mint
    gateway.milliseconds = lambda: clock["now"]
    return gateway, posts, minted


AUTH_OK = {"jsonrpc": "2.0", "id": 1, "result": {
    "access_token": "a" * 64, "token_type": "bearer", "expires_in": 900,
    "scope": "rat#view", "portfolio_uuid": "portfolio-1", "gateway_id": "g"}}


def test_the_cdp_key_is_exchanged_for_a_bearer_token():
    gateway, posts, minted = _gateway_with_auth(AUTH_OK, {"now": ms(2026, 10, 14)})

    request = gateway.sign("get_account_summaries", "private", "GET", {"extended": "true"})

    assert request["url"].startswith(
        "https://drb.coinbase.com/api/v2/private/get_account_summaries?")
    assert request["headers"] == {"Authorization": "Bearer " + "a" * 64}

    assert len(posts) == 1
    assert posts[0]["method"] == "POST"
    assert posts[0]["url"] == "https://drb.coinbase.com/api/v2/public/auth"
    assert posts[0]["body"]["method"] == "public/auth"
    assert posts[0]["body"]["params"] == {"grant_type": "coinbase_cdp", "token": "minted-jwt"}

    # Signed for the exact endpoint, as an Ed25519 key - the same detection
    # ccxt.coinbase applies to REST calls.
    assert minted == [{"method": "POST", "url": posts[0]["url"], "use_eddsa": True}]
    assert gateway.granted_scope == "rat#view"


def test_the_token_is_reused_until_it_nearly_expires():
    clock = {"now": ms(2026, 10, 14)}
    gateway, posts, _ = _gateway_with_auth(AUTH_OK, clock)

    gateway.access_token()
    clock["now"] += 10 * 60_000                      # 10 of 15 minutes
    gateway.access_token()
    assert len(posts) == 1

    clock["now"] += 4 * 60_000 + 30_000              # inside the refresh margin
    gateway.access_token()
    assert len(posts) == 2


def test_public_calls_never_authenticate():
    gateway, posts, _ = _gateway_with_auth(AUTH_OK, {"now": ms(2026, 10, 14)})
    request = gateway.sign("get_instruments", "public", "GET", {"currency": "USDC"})
    assert "Authorization" not in (request.get("headers") or {})
    assert posts == []


def test_an_auth_reply_without_a_token_is_an_authentication_error():
    import ccxt
    bad = {"jsonrpc": "2.0", "error": {"code": 13004, "message": "invalid_credentials"}}
    gateway, _, _ = _gateway_with_auth(bad, {"now": ms(2026, 10, 14)})
    with pytest.raises(ccxt.AuthenticationError, match="no access token"):
        gateway.access_token()


def test_a_different_portfolio_is_reported(caplog):
    gateway, _, _ = _gateway_with_auth(AUTH_OK, {"now": ms(2026, 10, 14)})
    gateway.expected_portfolio_uuid = "portfolio-stored-at-signup"
    with caplog.at_level(logging.WARNING):
        gateway.access_token()
    assert "not the portfolio-stored-at-signup stored for it" in caplog.text


@pytest.mark.parametrize("secret,expected", [
    (ED25519_SECRET, True),
    ("-----BEGIN EC PRIVATE KEY-----\nnot-a-real-key\n-----END EC PRIVATE KEY-----\n", False),
])
def test_key_type_detection_matches_ccxt(secret, expected):
    assert coinbase_derivatives.cdp_key_is_ed25519(secret) is expected


@pytest.mark.parametrize("scope,view_only", [
    ("rat#view", True),
    ("rat#view other#view", True),
    ("rat#view rat#trade", False),
    ("", False),
    (None, False),
])
def test_scope_is_view_only(scope, view_only):
    assert coinbase_derivatives.scope_is_view_only(scope) is view_only


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

SINCE = ms(2026, 10, 10)


def test_settlement_currencies_come_from_the_markets_not_a_constant():
    """Inverse BTC and ETH perps settle in their own coin, not USDC."""
    assert coinbase_derivatives.settlement_currencies(FakeGateway()) == ["BTC", "ETH", "USDC"]


def test_every_settlement_currency_and_both_history_modes_are_asked():
    gateway = FakeGateway()
    coinbase_derivatives.closed_orders(gateway, since=SINCE)

    asked = {(p["currency"], p["historical"]) for p in gateway.history_calls()}
    assert asked == {(c, h) for c in ("BTC", "ETH", "USDC") for h in ("true", "false")}


def test_recent_and_historical_orders_are_merged():
    """
    historical=false is only the last 30 minutes of orders; historical=true
    excludes the most recent. An order in either must come back.
    """
    old = raw_order("USDC-1", ms(2026, 10, 11))
    new = raw_order("USDC-2", ms(2026, 10, 14, 9))
    gateway = FakeGateway(history={("USDC", True): [old], ("USDC", False): [new]})

    orders = coinbase_derivatives.closed_orders(gateway, since=SINCE)

    assert [o["id"] for o in orders] == ["USDC-1", "USDC-2"]
    assert orders[0]["symbol"] == "PUMP/USDC:USDC"


def test_an_order_in_both_modes_is_kept_once_at_its_latest_state():
    partial = raw_order("USDC-1", ms(2026, 10, 11), filled=1000.0)
    complete = raw_order("USDC-1", ms(2026, 10, 11), updated=ms(2026, 10, 12), filled=5542.0)
    gateway = FakeGateway(history={("USDC", True): [partial], ("USDC", False): [complete]})

    orders = coinbase_derivatives.closed_orders(gateway, since=SINCE)

    assert len(orders) == 1
    assert orders[0]["filled"] == pytest.approx(5542.0)


def test_a_resting_order_filled_after_the_resume_point_is_not_lost():
    """
    Created before `since`, filled after it. Filtering on creation time would
    drop it permanently, since the resume point never moves back.
    """
    resting = raw_order("USDC-7", created=ms(2026, 10, 8), updated=ms(2026, 10, 12))
    stale = raw_order("USDC-8", created=ms(2026, 10, 2), updated=ms(2026, 10, 3))
    gateway = FakeGateway(history={("USDC", True): [resting, stale]})

    orders = coinbase_derivatives.closed_orders(gateway, since=SINCE)

    assert [o["id"] for o in orders] == ["USDC-7"]


def test_inverse_perp_orders_are_found_under_their_own_currency():
    btc = raw_order("BTC-1", ms(2026, 10, 12), instrument="BTC-PERPETUAL",
                    filled=100.0, price=83000.0, commission=0.0000012)
    gateway = FakeGateway(history={("BTC", True): [btc]})

    orders = coinbase_derivatives.closed_orders(gateway, since=SINCE)

    assert [o["symbol"] for o in orders] == ["BTC/USD:BTC"]


def test_paging_stops_early_only_when_the_data_proves_newest_first():
    HOUR = 3_600_000
    newest_first = [raw_order(f"USDC-{i}", ms(2026, 10, 14) - i * 4 * HOUR)
                    for i in range(6)]
    gateway = FakeGateway(history={("USDC", True): newest_first})

    # Page one is 0h, -4h, -8h. A resume point at -1h reads from -7h (the
    # re-read window), so page one's last order is already older than that
    # and nothing on later pages can qualify.
    since = ms(2026, 10, 14) - HOUR
    orders = coinbase_derivatives.closed_orders(gateway, since=since, page_size=3)

    usdc_historical = [p for p in gateway.history_calls()
                       if p["currency"] == "USDC" and p["historical"] == "true"]
    assert len(usdc_historical) == 1
    assert {o["id"] for o in orders} == {"USDC-0", "USDC-1"}


def test_a_resume_point_is_widened_by_the_reread_window():
    """
    The seam between the two history modes: an order older than 30 minutes
    but not yet indexed is in neither at sync time. If a newer order is stored
    in that run, the resume point moves past the missed one - so every run
    re-reads a window behind it.
    """
    HOUR = 3_600_000
    resume = ms(2026, 10, 14, 12)
    within = raw_order("USDC-in", resume - 3 * HOUR)
    beyond = raw_order("USDC-out", resume - 7 * HOUR)
    gateway = FakeGateway(history={("USDC", True): [within, beyond]})

    orders = coinbase_derivatives.closed_orders(gateway, since=resume)

    assert [o["id"] for o in orders] == ["USDC-in"]


def test_without_a_resume_point_the_read_starts_at_the_competition():
    """Nothing to widen: a first read starts at COMPETITION_START exactly."""
    start = venue_common.resolve_since(FakeGateway(), None)
    before = raw_order("USDC-before", start - 3_600_000)
    after = raw_order("USDC-after", start + 3_600_000)
    gateway = FakeGateway(history={("USDC", True): [before, after]})

    orders = coinbase_derivatives.closed_orders(gateway)

    assert [o["id"] for o in orders] == ["USDC-after"]


def test_ascending_pages_are_read_to_the_end():
    """If the order is oldest-first, stopping early would skip everything new."""
    oldest_first = [raw_order(f"USDC-{i}", ms(2026, 10, 1) + i * 86_400_000)
                    for i in range(13)]
    gateway = FakeGateway(history={("USDC", True): oldest_first})

    orders = coinbase_derivatives.closed_orders(gateway, since=SINCE, page_size=3)

    assert {o["id"] for o in orders} == {"USDC-9", "USDC-10", "USDC-11", "USDC-12"}


def test_paging_has_a_hard_cap(monkeypatch, caplog):
    class Endless(FakeGateway):
        def private_get_get_order_history_by_currency(self, params={}):
            self.calls.append(("history", dict(params)))
            return {"result": [raw_order("USDC-same", ms(2026, 10, 12))] * int(params["count"])}

    monkeypatch.setattr(coinbase_derivatives, "_MAX_ORDER_PAGES", 4)
    gateway = Endless()
    with caplog.at_level(logging.WARNING):
        coinbase_derivatives._order_history(gateway, "USDC", True, SINCE, 2)
    assert len(gateway.history_calls()) == 4
    assert "history may be incomplete" in caplog.text


# ---------------------------------------------------------------------------
# The 1 October migration
# ---------------------------------------------------------------------------

MIGRATED_AT = ms(2026, 10, 1, 9, 28, 36) + 802          # the observed correction
CORRECTION = {"type": "correction", "timestamp": str(MIGRATED_AT),
              "info": {"reason": "coinbase_migration"}, "change": "22.482337"}


def test_positions_recreated_at_the_migration_are_dropped():
    artifact = raw_order("USDC-mig", MIGRATED_AT + 757, commission=None)
    later_same_day = raw_order("USDC-real", MIGRATED_AT + 2 * 3_600_000)
    gateway = FakeGateway(history={("USDC", True): [artifact, later_same_day]},
                          logs={"USDC": [CORRECTION]})

    orders = coinbase_derivatives.closed_orders(gateway, since=ms(2026, 9, 1))

    assert [o["id"] for o in orders] == ["USDC-real"]
    log_calls = [p for name, p in gateway.calls if name == "log"]
    assert len(log_calls) == 1
    assert log_calls[0]["query"] == "correction"


def test_an_ordinary_correction_is_not_mistaken_for_the_migration():
    other = {**CORRECTION, "info": {"reason": "manual_adjustment"}}
    order = raw_order("USDC-x", MIGRATED_AT + 757)
    gateway = FakeGateway(history={("USDC", True): [order]}, logs={"USDC": [other]})

    assert [o["id"] for o in coinbase_derivatives.closed_orders(gateway, since=ms(2026, 9, 1))] \
        == ["USDC-x"]


def test_no_migration_lookup_once_the_resume_point_is_past_it():
    """The extra request is paid once per credential, not every hour forever."""
    gateway = FakeGateway(history={("USDC", True): [raw_order("USDC-1", ms(2026, 10, 12))]})
    coinbase_derivatives.closed_orders(gateway, since=SINCE)
    assert not [c for c in gateway.calls if c[0] == "log"]


def test_no_migration_lookup_without_orders_on_migration_day():
    gateway = FakeGateway(history={("USDC", True): [raw_order("USDC-1", ms(2026, 10, 5))]})
    coinbase_derivatives.closed_orders(gateway, since=ms(2026, 9, 1))
    assert not [c for c in gateway.calls if c[0] == "log"]


# ---------------------------------------------------------------------------
# The Coinbase adapter routes perps here
# ---------------------------------------------------------------------------

def test_a_perp_credential_builds_a_gateway_client(monkeypatch):
    monkeypatch.setattr(coinbase, "load_shared_markets", lambda exchange: None)
    fernet = venue_common.get_fernet()
    credential = {"exchange": "coinbase", "account_type": "perp",
                  "api_key": fernet.encrypt(KEY_NAME.encode()).decode(),
                  "api_secret": fernet.encrypt(ED25519_SECRET.encode()).decode(),
                  "portfolio_uuid": "portfolio-1"}

    exchange = coinbase.build_from_credential(credential)

    assert coinbase_derivatives.is_gateway(exchange)
    assert exchange.apiKey == KEY_NAME                  # decrypted
    assert exchange.expected_portfolio_uuid == "portfolio-1"


def test_a_spot_credential_still_builds_advanced_trade(monkeypatch):
    monkeypatch.setattr(coinbase, "load_shared_markets", lambda exchange: None)
    fernet = venue_common.get_fernet()
    credential = {"exchange": "coinbase", "account_type": "spot",
                  "api_key": fernet.encrypt(KEY_NAME.encode()).decode(),
                  "api_secret": fernet.encrypt(ED25519_SECRET.encode()).decode()}

    exchange = coinbase.build_from_credential(credential)

    assert exchange.id == "coinbase"
    assert not coinbase_derivatives.is_gateway(exchange)


def test_perp_valuation_goes_to_the_gateway():
    value = coinbase.get_account_totals_usdc(FakeGateway(summaries=SUMMARIES), "perp")
    assert value["total_usdc"] == pytest.approx(19.850429434)


def test_perp_valuation_refuses_an_advanced_trade_client():
    """INTX REST still answers, as a lagging mirror - it must not be used."""
    import ccxt
    with pytest.raises(ValueError, match="derivatives gateway"):
        coinbase.get_account_totals_usdc(
            ccxt.coinbase({"apiKey": KEY_NAME, "secret": ED25519_SECRET}), "perp")


def test_perp_orders_are_cleaned_like_spot_ones():
    gateway = FakeGateway(history={("USDC", True): [
        raw_order("USDC-1", ms(2026, 10, 12), commission=None)]})

    rows = coinbase.closed_orders(gateway, since=SINCE)

    assert len(rows) == 1
    row = rows[0]
    assert row["account_type"] == "perp"
    assert row["symbol"] == "PUMP/USDC:USDC"
    assert row["order_id"] == "USDC-1"
    # No fee currency on the order: derived from the symbol, without the
    # ':USDC' settle suffix, which is not a currency.
    assert row["fee_currency"] == "USDC"


def test_a_perp_key_reports_no_cash_flows_and_makes_no_requests():
    gateway = FakeGateway()
    assert coinbase.get_cash_flows(gateway) == []
    assert gateway.calls == []


def test_perp_signup_proves_the_gateway_and_records_its_scope(monkeypatch):
    gateway = FakeGateway(summaries=SUMMARIES)
    gateway.granted_scope = "rat#view"
    gateway.gateway_portfolio_uuid = "portfolio-1"
    monkeypatch.setattr(coinbase_derivatives, "build_exchange", lambda *a, **k: gateway)

    granted = coinbase.verify_derivatives_access(KEY_NAME, ED25519_SECRET, "portfolio-1")

    assert granted == {"gateway_scope": "rat#view", "gateway_portfolio_uuid": "portfolio-1"}


def test_perp_signup_fails_if_the_gateway_refuses_the_key(monkeypatch):
    class Refused(FakeGateway):
        def private_get_get_account_summaries(self, params={}):
            raise RuntimeError("invalid_credentials")

    monkeypatch.setattr(coinbase_derivatives, "build_exchange", lambda *a, **k: Refused())
    with pytest.raises(ValueError, match="could not read the derivatives gateway"):
        coinbase.verify_derivatives_access(KEY_NAME, ED25519_SECRET)


def test_a_non_view_scope_is_logged(monkeypatch, caplog):
    gateway = FakeGateway(summaries=SUMMARIES)
    gateway.granted_scope = "rat#view rat#trade"
    monkeypatch.setattr(coinbase_derivatives, "build_exchange", lambda *a, **k: gateway)
    with caplog.at_level(logging.WARNING):
        coinbase.verify_derivatives_access(KEY_NAME, ED25519_SECRET)
    assert "expected view-only" in caplog.text


def test_markets_are_loaded_for_futures_only():
    """
    ~5,900 instruments unfiltered, almost all options the gateway does not
    trade yet; ~220 with kind=future, and every perpetual still among them.
    """
    gateway = FakeGateway()
    gateway.load_markets()

    requested = [p for name, p in gateway.calls if name == "instruments"]
    assert requested == [{"kind": "future"}]
    assert sum(1 for m in gateway.markets.values() if m.get("swap")) == 3
