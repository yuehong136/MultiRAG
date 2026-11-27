"""Model gateway key format: issue, parse and verify."""

import pytest

from api.model_gateway import keys


def test_issued_key_round_trips_and_stores_only_a_digest():
    issued = keys.issue_key()

    assert issued.plaintext.startswith(keys.KEY_PREFIX)
    assert keys.parse_key_id(issued.plaintext) == issued.key_id
    assert keys.verify_key(issued.plaintext, issued.secret_hash)
    assert len(issued.secret_hash) == 64
    assert issued.plaintext not in issued.secret_hash
    # A stray log line or traceback must not print the secret.
    assert issued.plaintext not in repr(issued)


def test_issued_keys_are_unique():
    first, second = keys.issue_key(), keys.issue_key()

    assert first.key_id != second.key_id
    assert first.plaintext != second.plaintext


def test_secret_containing_the_separator_still_parses():
    key_id = "0123456789abcdef0123456789abcdef"
    secret = "-" * 20 + "_" * 3 + "A" * 20

    assert keys.parse_key_id(f"{keys.KEY_PREFIX}{key_id}-{secret}") == key_id


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "sk-mrag-",
        "multirag-" + "a" * 43,
        "sk-other-0123456789abcdef0123456789abcdef-" + "a" * 43,
        "sk-mrag-0123456789ABCDEF0123456789ABCDEF-" + "a" * 43,
        "sk-mrag-0123456789abcdef0123456789abcdef-" + "a" * 42,
        "sk-mrag-0123456789abcdef0123456789abcdef-" + "a" * 44,
        "sk-mrag-0123456789abcdef0123456789abcdef_" + "a" * 43,
        "sk-mrag-0123456789abcdef0123456789abcdef-" + "a" * 42 + "!",
        "sk-mrag-0123456789abcdef0123456789abcdef-" + "a" * 42 + "é",
    ],
)
def test_malformed_keys_have_no_key_id(presented):
    assert keys.parse_key_id(presented) is None


def test_another_secret_for_the_same_key_id_is_rejected():
    issued = keys.issue_key()
    forged = issued.plaintext[:-1] + ("A" if issued.plaintext[-1] != "A" else "B")

    assert keys.parse_key_id(forged) == issued.key_id
    assert not keys.verify_key(forged, issued.secret_hash)


def test_redacted_form_shows_no_secret():
    issued = keys.issue_key()
    secret = issued.plaintext.rsplit(issued.key_id + "-", 1)[1]

    redacted = keys.redact_key(issued.key_id)

    assert redacted.startswith(keys.KEY_PREFIX + issued.key_id[:8])
    assert secret not in redacted
    assert issued.key_id not in redacted


def test_import_lookup_accepts_only_external_bearer_credentials() -> None:
    external = "sk-" + "a" * 45
    assert keys.import_lookup_digest(external) == keys.hash_key(external)
    for value in (keys.issue_key().plaintext, keys.KEY_PREFIX + "a" * 48, "multirag-" + "a" * 48, "short", "a" * 257, external + " ", external + "\n"):
        assert keys.import_lookup_digest(value) is None
