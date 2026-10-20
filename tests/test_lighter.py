"""
Tests for lighter.py.

All offline - nothing here touches Lighter's API.
"""

import time

import pytest

lighter = pytest.importorskip("lighter")
ccxt = pytest.importorskip("ccxt")

# A syntactically valid L1 key for a wallet that owns nothing. Never a real
# key - tests must never depend on one. Only ever used to prove it is refused.
FAKE_L1 = "0x" + "a" * 64


# ---------------------------------------------------------------------------
# build_exchange
# ---------------------------------------------------------------------------

def test_missing_key_is_rejected():
    with pytest.raises(ValueError, match="required"):
        lighter.build_exchange("", encrypted=False)


def test_undecryptable_credential_names_the_likely_cause():
    with pytest.raises(ValueError, match="FERNET_KEY"):
        lighter.build_exchange("not-a-fernet-token", encrypted=True)


# ---------------------------------------------------------------------------
# Wallet private keys are refused
#
# A read-only token is scoped and expires; a wallet private key controls every
# asset in the wallet on every chain and cannot be revoked. Accepting one makes
# this service the custodian of a competitor's wallet, permanently, for a
# competition that only ever needed to read a balance. It is the one mistake
# here that cannot be undone afterwards.
# ---------------------------------------------------------------------------

def test_a_wallet_private_key_is_refused():
    with pytest.raises(ValueError, match="WALLET PRIVATE KEY"):
        lighter.build_exchange(FAKE_L1, encrypted=False)


def test_the_refusal_says_what_to_do_instead():
    """An error a participant can act on, not just a rejection."""
    with pytest.raises(ValueError, match="read-only token"):
        lighter.build_exchange(FAKE_L1, encrypted=False)


def test_a_stored_wallet_key_stops_working_too():
    """
    The check lives in build_exchange, not only in verify_credential, because
    signup validation does nothing about a wallet key ALREADY sitting in
    participant_api_keys. Without this the sync would keep using it forever.
    """
    from venue_common import get_fernet
    stored = get_fernet().encrypt(FAKE_L1.encode()).decode()
    with pytest.raises(ValueError, match="WALLET PRIVATE KEY"):
        lighter.build_exchange(stored, encrypted=True)


def test_tokens_are_unaffected_by_the_refusal():
    """The refusal must not cost the credential type we actually want."""
    ex = lighter.build_exchange(token(), encrypted=False)
    assert ex.id == "lighter"


def test_a_wallet_address_is_refused_like_a_key():
    address = "0x" + "3c03630c49b74c481d8458fa31e05828bf170bc3"
    with pytest.raises(ValueError, match="WALLET PRIVATE KEY"):
        lighter.build_exchange(address, encrypted=False)


def test_the_old_operator_flag_no_longer_opens_anything(monkeypatch):
    """
    ALLOW_WALLET_KEYS used to let the operator authenticate with their own
    wallet key. It is gone - a flag that must never be set on the deployed
    service is a flag that eventually is. Setting it now must change nothing,
    at build time or at signup.
    """
    monkeypatch.setenv("ALLOW_WALLET_KEYS", "1")
    with pytest.raises(ValueError, match="WALLET PRIVATE KEY"):
        lighter.build_exchange(FAKE_L1, encrypted=False)
    with pytest.raises(ValueError, match="not a Lighter read-only token"):
        lighter.verify_credential({"api_key": FAKE_L1, "account_type": "perp"})


# ---------------------------------------------------------------------------
# Venue facts
#
# These assert against ccxt itself. If a future ccxt release gives Lighter a
# spot product, or moves it off privateKey auth, this module's assumptions
# break and these tests are what says so.
# ---------------------------------------------------------------------------

def test_ccxt_still_says_lighter_is_perps_only():
    ex = ccxt.lighter()
    assert ex.has["spot"] is False
    assert ex.has["swap"] is True


def test_ccxt_still_wants_a_private_key():
    ex = ccxt.lighter()
    required = {k for k, v in ex.requiredCredentials.items() if v}
    assert required == {"privateKey"}


def test_ccxt_still_has_no_combined_transfer_endpoint():
    """get_cash_flows merges two calls because of this."""
    ex = ccxt.lighter()
    assert not ex.has.get("fetchDepositsWithdrawals")
    assert ex.has["fetchDeposits"] and ex.has["fetchWithdrawals"]


def test_spot_is_refused():
    """
    Returning an empty balance would read as a participant with no money.
    Refusing says the credential was registered wrong.
    """
    ex = lighter.build_exchange(token(), encrypted=False)
    with pytest.raises(ValueError, match="no 'spot' account"):
        lighter.get_account_totals_usdc(ex, account_type="spot")


def test_account_type_is_always_perp():
    assert lighter.account_type_from_order({"symbol": "ETH/USDC:USDC"}) == "perp"
    assert lighter.account_type_from_order({}) == "perp"


# ---------------------------------------------------------------------------
# closed_orders - fills aggregated back into orders
# ---------------------------------------------------------------------------

def fill(order_id, timestamp, price, amount, fee=0.0, symbol="ETH/USDC:USDC",
         side="buy"):
    return {"order_id": order_id, "trade_id": f"t{timestamp}",
            "timestamp": timestamp, "datetime": f"d{timestamp}",
            "symbol": symbol, "type": "limit", "side": side,
            "price": price, "amount": amount,
            "fee_cost": fee, "fee_currency": "USDC"}


@pytest.fixture
def fills(monkeypatch):
    def install(rows):
        monkeypatch.setattr(lighter, "closed_trades", lambda *a, **k: rows)
    return install


def test_single_fill_becomes_one_order(fills):
    fills([fill("A", 1000, 100.0, 2.0, fee=0.5)])
    order = lighter.closed_orders(None)[0]
    assert order["order_id"] == "A"
    assert order["amount"] == 2.0
    assert order["price"] == 100.0
    assert order["fee_cost"] == 0.5


def test_partial_fills_use_a_size_weighted_price(fills):
    """(1 x 100 + 3 x 200) / 4 = 175, not the 150 a plain mean would give."""
    fills([fill("A", 1000, 100.0, 1.0), fill("A", 2000, 200.0, 3.0)])
    order = lighter.closed_orders(None)[0]
    assert order["amount"] == 4.0
    assert order["price"] == pytest.approx(175.0)


def test_fees_accumulate_across_fills(fills):
    fills([fill("A", 1000, 100.0, 1.0, fee=0.1),
           fill("A", 2000, 100.0, 1.0, fee=0.3)])
    assert lighter.closed_orders(None)[0]["fee_cost"] == pytest.approx(0.4)


def test_order_timestamp_is_the_last_fill(fills):
    """An order is closed when it last filled, matching Coinbase semantics."""
    fills([fill("A", 5000, 100.0, 1.0), fill("A", 1000, 100.0, 1.0)])
    assert lighter.closed_orders(None)[0]["timestamp"] == 5000


def test_orders_come_back_chronological(fills):
    fills([fill("A", 3000, 1.0, 1.0), fill("B", 1000, 1.0, 1.0),
           fill("C", 2000, 1.0, 1.0)])
    assert [o["order_id"] for o in lighter.closed_orders(None)] == ["B", "C", "A"]


def test_internal_notional_key_never_reaches_the_database(fills):
    """_notional is scratch space; trade_metrics has no such column."""
    fills([fill("A", 1000, 100.0, 1.0)])
    assert "_notional" not in lighter.closed_orders(None)[0]


def test_rows_match_the_trade_metrics_columns(fills):
    """
    A key with no column makes PostgREST reject the whole upsert, losing
    every order in the batch rather than just the offending field.
    """
    import re
    from pathlib import Path

    sql = Path(__file__).resolve().parent.parent / "migrations" / "schema.sql"
    block = sql.read_text(encoding="utf-8").split(
        "create table if not exists public.trade_metrics")[1].split(");")[0]
    columns = set(re.findall(r"^\s{2}(\w+)", block, re.M))

    fills([fill("A", 1000, 100.0, 1.0)])
    assert set(lighter.closed_orders(None)[0]) <= columns


def test_fill_without_an_order_id_is_skipped_not_fatal(fills):
    orphan = {**fill("X", 2000, 1.0, 1.0), "order_id": None, "trade_id": None}
    fills([fill("A", 1000, 100.0, 1.0), orphan])
    assert [o["order_id"] for o in lighter.closed_orders(None)] == ["A"]


def test_no_fills_gives_no_orders(fills):
    fills([])
    assert lighter.closed_orders(None) == []


def test_orders_are_stamped_for_the_sync_to_fill_in(fills):
    fills([fill("A", 1000, 100.0, 1.0)])
    order = lighter.closed_orders(None)[0]
    assert order["participant_id"] is None      # sync populates
    assert order["account_type"] == "perp"


# ---------------------------------------------------------------------------
# Read-only tokens
#
# The preferred credential: unlike a wallet private key it cannot move funds
# and expires on its own, so collecting one per participant carries no
# custody risk.
# ---------------------------------------------------------------------------

SIG = "d" * 64


def token(account_index=741152, scope="all", deadline=None, prefix="ro"):
    deadline = deadline if deadline is not None else int(time.time()) + 86400
    return f"{prefix}:{account_index}:{scope}:{deadline}:{SIG}"


def test_token_fields_are_parsed():
    parsed = lighter.parse_readonly_token(token(account_index=741152))
    assert parsed["account_index"] == 741152
    assert parsed["scope"] == "all"
    assert isinstance(parsed["deadline"], int)


def test_deadline_is_an_int_not_a_string():
    """ccxt compares the deadline numerically before reusing a cached token."""
    parsed = lighter.parse_readonly_token(token())
    assert isinstance(parsed["deadline"], int)


@pytest.mark.parametrize("bad", [
    "",
    "not-a-token",
    "ro:741152:all",                      # too few segments
    "xx:741152:all:9999999999:" + SIG,    # wrong prefix
])
def test_malformed_tokens_are_rejected(bad):
    with pytest.raises(ValueError):
        lighter.parse_readonly_token(bad)


def test_non_numeric_fields_are_rejected():
    with pytest.raises(ValueError, match="numeric"):
        lighter.parse_readonly_token(f"ro:abc:all:9999999999:{SIG}")


def test_token_builds_an_exchange_with_no_private_key():
    """The whole point: authenticate without a credential that can move money."""
    ex = lighter.build_exchange_from_token(token(), encrypted=False)
    assert ex.privateKey is None
    assert ex.options["accountIndex"] == 741152


def test_token_is_cached_where_ccxt_looks_for_it():
    """
    create_auth() reads options['auths'][accountIndex][apiKeyIndex]. If this
    layout ever changes, ccxt falls back to signing and the token path breaks.
    """
    raw = token()
    ex = lighter.build_exchange_from_token(raw, encrypted=False)
    cached = ex.options["auths"]["741152"][str(lighter._DEFAULT_API_KEY_INDEX)]
    assert cached["token"] == raw
    assert cached["signer"] is None


def test_api_key_index_is_within_the_range_ccxt_accepts():
    """ccxt validates apiKeyIndex into 4..254 and rejects 0."""
    assert 4 <= lighter._DEFAULT_API_KEY_INDEX <= 254


def test_expired_token_is_refused_up_front():
    """
    Better than letting Lighter answer 'invalid auth string' mid-sync, which
    gives no hint that the fix is to reissue the token.
    """
    with pytest.raises(ValueError, match="expired"):
        lighter.build_exchange_from_token(
            token(deadline=int(time.time()) - 86400), encrypted=False)


def test_expiring_token_warns(caplog):
    soon = int(time.time()) + 3 * 86400
    with caplog.at_level("WARNING"):
        lighter.build_exchange_from_token(token(deadline=soon), encrypted=False)
    assert "expires in" in caplog.text


def test_build_exchange_routes_a_token_to_the_token_path():
    """
    Both credential types share one column, so the sync must not need to know
    which a participant registered.
    """
    ex = lighter.build_exchange(token(), encrypted=False)
    assert ex.privateKey is None
    assert ex.options["accountIndex"] == 741152


def test_build_exchange_routes_an_encrypted_token_too():
    from venue_common import get_fernet
    sealed = get_fernet().encrypt(token().encode()).decode()
    ex = lighter.build_exchange(sealed, encrypted=True)
    assert ex.privateKey is None
    assert ex.options["accountIndex"] == 741152


def test_a_private_key_is_refused_where_a_token_is_accepted():
    """
    Both credential types share one column, so build_exchange routes on the
    'ro:' prefix. This asserts the routing now REFUSES the branch it used to
    accept, while the token branch is untouched - the two must not have been
    broken together.
    """
    assert lighter.build_exchange(token(), encrypted=False).id == "lighter"

    with pytest.raises(ValueError, match="WALLET PRIVATE KEY"):
        lighter.build_exchange(FAKE_L1, encrypted=False)


# ---------------------------------------------------------------------------
# verify_credential — the signup gate
#
# Everything asserted here fails BEFORE any network call, which is what makes
# it testable offline. The live fetch_balance() that follows is not exercised.
# ---------------------------------------------------------------------------

def test_signup_rejects_a_wallet_private_key():
    """
    The gate that matters. Rejected on the SHAPE of the value, not on whether
    it works - a wallet key authenticates perfectly well, which is exactly the
    problem.
    """
    with pytest.raises(ValueError, match="not a Lighter read-only token"):
        lighter.verify_credential({"api_key": FAKE_L1, "account_type": "perp"})


def test_signup_rejects_a_wallet_address():
    address = "0x" + "3c03630c49b74c481d8458fa31e05828bf170bc3"
    with pytest.raises(ValueError, match="not a Lighter read-only token"):
        lighter.verify_credential({"api_key": address, "account_type": "perp"})


def test_signup_rejects_spot_before_touching_the_network():
    with pytest.raises(ValueError, match="no 'spot' market"):
        lighter.verify_credential({"api_key": token(), "account_type": "spot"})


def test_signup_rejects_an_expired_token():
    import time
    expired = token(deadline=int(time.time()) - 86400)
    with pytest.raises(ValueError, match="expired"):
        lighter.verify_credential({"api_key": expired, "account_type": "perp"})


def test_signup_rejects_a_malformed_token():
    with pytest.raises(ValueError, match="read-only token"):
        lighter.verify_credential({"api_key": "ro:nope", "account_type": "perp"})


# ---------------------------------------------------------------------------
# Token scope
# ---------------------------------------------------------------------------

def test_unrecognised_scope_is_warned_about_not_rejected(caplog):
    """
    Lighter's scope vocabulary is unconfirmed - only 'all' has been observed.
    Enforcing an allowlist built on one observation would reject valid tokens,
    so this warns until the real values are known.
    """
    details = lighter.parse_readonly_token(token(scope="something-new"))
    with caplog.at_level("WARNING"):
        lighter._check_token_scope(details)
    assert "unrecognised scope" in caplog.text


def test_scope_can_be_enforced_once_the_vocabulary_is_known(monkeypatch):
    monkeypatch.setenv("LIGHTER_ENFORCE_SCOPE", "1")
    details = lighter.parse_readonly_token(token(scope="something-new"))
    with pytest.raises(ValueError, match="scope"):
        lighter._check_token_scope(details)


def test_an_allowed_scope_passes_under_enforcement(monkeypatch):
    monkeypatch.setenv("LIGHTER_ENFORCE_SCOPE", "1")
    monkeypatch.setenv("LIGHTER_ALLOWED_SCOPES", "all,read")
    lighter._check_token_scope(lighter.parse_readonly_token(token(scope="read")))


def test_absent_fee_stays_none_rather_than_becoming_zero(fills):
    """
    Lighter's trade payload has no fee field. Storing 0.0 would assert the
    trade was free when the truth is unknown - and the leaderboard scores
    people on net performance.
    """
    row = fill("A", 1000, 100.0, 1.0)
    row["fee_cost"] = None
    fills([row])
    assert lighter.closed_orders(None)[0]["fee_cost"] is None


def test_partially_reported_fees_sum_what_is_known(fills):
    a = fill("A", 1000, 100.0, 1.0, fee=0.25)
    b = fill("A", 2000, 100.0, 1.0)
    b["fee_cost"] = None
    fills([a, b])
    assert lighter.closed_orders(None)[0]["fee_cost"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# One account fetch per run, not two
#
# Lighter allows a standard account 60 requests a minute, per IP as well as
# per account, so every Lighter credential in a run shares one 1 req/s
# budget. Valuation and the cash-flow L1-address lookup used to fetch the
# same account seconds apart; that second request cost a full second per
# Lighter participant on every run.
# ---------------------------------------------------------------------------

class _CountingAccount:
    """Just enough of a ccxt.lighter instance for valuation and cash flows."""

    def __init__(self):
        self.options = {}
        self.balance_calls = 0

    def fetch_balance(self, params=None):
        self.balance_calls += 1
        return {"info": {"accounts": [{
            "l1_address": "0x" + "1" * 40, "collateral": "14.58",
            "total_asset_value": "14.58", "available_balance": "14.58",
            "cross_asset_value": "14.58"}]}}

    def fetch_deposits(self, code=None, since=None, limit=None, params=None):
        assert params and params.get("address"), "fetch_deposits needs the L1 address"
        return []

    def fetch_withdrawals(self, code=None, since=None, limit=None, params=None):
        return []

    def milliseconds(self):
        return 1_790_000_000_000

    def iso8601(self, ms):
        return "2026-09-21T00:00:00.000Z"

    def parse8601(self, text):
        return 1_767_225_600_000


def test_valuation_then_cash_flows_fetches_the_account_once():
    exchange = _CountingAccount()

    lighter.get_account_totals_usdc(exchange, "perp")
    lighter.get_cash_flows(exchange)

    assert exchange.balance_calls == 1


def test_cash_flows_alone_still_find_the_address():
    """Called on its own, nothing is cached - it must fetch rather than fail."""
    exchange = _CountingAccount()

    assert lighter._l1_address(exchange) == "0x" + "1" * 40
    assert exchange.balance_calls == 1


# ---------------------------------------------------------------------------
# ccxt's own Lighter fee is off
#
# With builderFee on - ccxt's default - ccxt approves its integrator account
# (718718) to charge a maker and taker fee the first time it loads an account
# holding a wallet key, and attaches that fee to every order. Nothing here
# trades or holds a wallet key, so nothing pays it today; it is switched off
# so that nothing added later can start paying ccxt by default either.
#
# Asserted on BEHAVIOUR - the order ccxt would build, and whether it tries to
# approve - not just on the option, so a ccxt release that renames the switch
# fails here instead of leaving an override that no longer does anything.
# ---------------------------------------------------------------------------

# One real Lighter market, as ccxt parses it (public data, trimmed).
ETH_PERP = {
    "id": "0", "symbol": "ETH/USDC:USDC", "base": "ETH", "quote": "USDC",
    "settle": "USDC", "baseId": "ETH", "quoteId": "USDC", "settleId": "USDC",
    "type": "swap", "swap": True, "linear": True, "inverse": False,
    "contract": True, "contractSize": 1.0, "spot": False, "margin": False,
    "future": False, "option": False, "active": True,
    "precision": {"amount": 0.0001, "price": 0.01},
    "limits": {"amount": {"min": 0.002, "max": None},
               "price": {"min": None, "max": None},
               "cost": {"min": 10.0, "max": None},
               "leverage": {"min": None, "max": None}},
    "info": {"symbol": "ETH", "market_id": 0, "market_type": "perp",
             "supported_size_decimals": 4, "supported_price_decimals": 2,
             "size_decimals": 4, "price_decimals": 2},
}


def _fee_fields_on_an_order(exchange):
    """The integrator fields ccxt would put on a limit order. Builds it, never sends it."""
    exchange.set_markets([ETH_PERP])
    request = exchange.create_order_request(
        "ETH/USDC:USDC", "limit", "buy", 0.01, 2000.0,
        {"nonce": 1, "apiKeyIndex": 2, "accountIndex": 741152})
    return sorted(k for k in request[0] if k.startswith("integrator"))


def test_ccxt_charges_its_fee_by_default():
    """The control: proves the test can see the fee, so its absence below means something."""
    assert _fee_fields_on_an_order(ccxt.lighter()) == [
        "integrator_account_index", "integrator_maker_fee", "integrator_taker_fee"]


def test_our_clients_put_no_ccxt_fee_on_an_order():
    exchange = lighter.build_exchange_from_token(token(), encrypted=False)
    assert _fee_fields_on_an_order(exchange) == []


def test_a_stored_credential_gets_a_fee_free_client_too():
    from venue_common import get_fernet
    sealed = get_fernet().encrypt(token().encode()).decode()
    exchange = lighter.build_exchange(sealed, encrypted=True)
    assert _fee_fields_on_an_order(exchange) == []


def test_our_clients_never_approve_ccxts_integrator(monkeypatch):
    """
    The other half of the fee: the automatic on-chain approval. With the
    switch off ccxt must not even attempt it.
    """
    exchange = lighter.build_exchange_from_token(token(), encrypted=False)

    def approval_attempted(*args, **kwargs):
        raise AssertionError("ccxt tried to approve its integrator fee")

    monkeypatch.setattr(exchange, "approve_builder_fee", approval_attempted)
    assert exchange.handle_builder_fee_approval(741152, 2) is False
