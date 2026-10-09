"""Canonical hashing, parser/misread guard, and countersignature cryptography."""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from countersign.auth import virtual_authenticator as vauth
from countersign.core.approval_crypto import (
    CountersignError,
    challenge_bytes,
    payload_hash,
    server_attest,
    verify_server_attestation,
    verify_webauthn,
)
from countersign.core.canonical import canonical_hash, jcs
from countersign.core.parser import parse

orders = st.fixed_dictionaries(
    {
        "side": st.sampled_from(["BUY", "SELL"]),
        "quantity": st.integers(1, 100000),
        "limit_price_paise": st.integers(5, 10_00_000_00),
        "symbol": st.sampled_from(["INFY", "TCS", "RELIANCE"]),
    }
)


@given(orders)
def test_jcs_is_key_order_independent(o):
    reversed_o = dict(reversed(list(o.items())))
    assert jcs(o) == jcs(reversed_o)
    assert canonical_hash(o) == canonical_hash(reversed_o)


@given(orders, st.sampled_from(["side", "quantity", "limit_price_paise", "symbol"]))
def test_any_field_change_changes_hash(o, key):
    changed = dict(o)
    changed[key] = {"side": "SELL" if o["side"] == "BUY" else "BUY", "quantity": o["quantity"] + 1, "limit_price_paise": o["limit_price_paise"] + 5, "symbol": o["symbol"] + "X"}[key]
    assert canonical_hash(o) != canonical_hash(changed)


@pytest.mark.parametrize(
    "text,kind",
    [
        ("What is my P&L today?", "query"),
        ("Which positions are down more than 5%?", "query"),
        ("What is my average purchase price for Reliance?", "query"),
        ("Show NIFTY options near the money", "query"),
        ("buy 10 RELIANCE at 2800", "order"),
        ("Buy some Reliance", "clarify"),
        ("Buy around 500 INFY", "clarify"),
        ("Get me NIFTY calls", "clarify"),
        ("Sell my ABC", "clarify"),
        ("sell 20 INFY at 1850 and buy 5 TCS at 4100", "plan"),
        ("Buy 100 ABCCORP if the price falls below 500", "mandate"),
        ("Alert me if TATAMOTORS falls more than 5%", "mandate"),
        ("cancel my INFY order", "cancel"),
        ("modify my INFY order to 1850", "modify"),
    ],
)
def test_parser_routes_and_never_guesses(text, kind):
    assert parse(text).kind == kind


def test_misread_facts_capture_trader_values():
    p = parse("buy 100 INFY at 1850")
    f = p.facts
    assert f.quantity == 100 and str(f.price) == "1850" and f.sides == {"BUY"}
    assert not f.mentions_number(1000)


def _payload():
    return {"version": 1, "purpose": "ORDER", "proposal_id": "p1", "canonical_hash": "a" * 64, "account_id": "LAB1", "user_id": "u", "credential_ref": vauth.get().credential_id, "price_refs": [], "nonce": "n" * 32, "issued_at": "2026-10-08T00:00:00Z", "expires_at": "2026-10-08T00:01:00Z", "mandate_id": None}


def test_webauthn_assertion_verifies_only_for_the_signed_payload():
    va = vauth.get()
    payload = _payload()
    assertion = va.assert_challenge(challenge_bytes(payload), sign_count=10)
    assert verify_webauthn(assertion, payload, credential_public_key=va.cose_public_key(), sign_count_before=10, expected_credential_id=va.credential_id) == 11
    tampered = dict(payload, canonical_hash="b" * 64)
    with pytest.raises(CountersignError) as e:
        verify_webauthn(assertion, tampered, credential_public_key=va.cose_public_key(), sign_count_before=10, expected_credential_id=va.credential_id)
    assert e.value.code == "PAYLOAD_MISMATCH"


def test_webauthn_rejects_wrong_origin_and_wrong_device():
    va = vauth.get()
    payload = _payload()
    bad_origin = va.assert_challenge(challenge_bytes(payload), origin="https://evil.example", sign_count=20)
    with pytest.raises(CountersignError):
        verify_webauthn(bad_origin, payload, credential_public_key=va.cose_public_key(), sign_count_before=20, expected_credential_id=va.credential_id)
    good = va.assert_challenge(challenge_bytes(payload), sign_count=30)
    with pytest.raises(CountersignError):
        verify_webauthn(good, payload, credential_public_key=va.cose_public_key(), sign_count_before=30, expected_credential_id="someone-elses-device")


@settings(max_examples=20)
@given(st.text(min_size=1, max_size=20))
def test_server_attestation_binds_exact_payload(junk):
    h = payload_hash(_payload())
    sig = server_attest(h, "pin")
    verify_server_attestation(h, "pin", sig)
    other = payload_hash(dict(_payload(), nonce=junk))
    if other != h:
        with pytest.raises(CountersignError):
            verify_server_attestation(other, "pin", sig)
    with pytest.raises(CountersignError):
        verify_server_attestation(h, "totp", sig)


def test_audit_scrubs_secrets():
    from countersign.core.audit import _scrub

    out = _scrub({"pin": "1234", "nested": {"otp": "999999", "ok": 1}, "list": [{"api_key": "x"}]})
    assert json.dumps(out).count("[redacted]") == 3 and out["nested"]["ok"] == 1
