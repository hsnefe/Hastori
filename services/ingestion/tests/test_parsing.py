import contextlib
import json
import uuid

import pytest

from hastori_ingestion.batching import BatchPolicy
from hastori_ingestion.parsing import (
    MAX_AGE_S,
    Rejected,
    message_id,
    parse_payload,
    parse_topic,
)

NOW = 1_700_000_000.0
SITE = uuid.uuid4()
DEV = uuid.uuid4()


def payload(**over: object) -> bytes:
    body: dict[str, object] = {
        "ts": NOW,
        "metrics": {"active_power_kw": 12.5, "temperature_c": 61.0},
    }
    body.update(over)
    return json.dumps(body).encode()


def reason(raw: bytes | str) -> str:
    with pytest.raises(Rejected) as exc:
        parse_payload(raw, NOW)
    return exc.value.reason


def test_topic_ok() -> None:
    assert parse_topic(f"sites/{SITE}/devices/{DEV}/telemetry") == (SITE, DEV)


@pytest.mark.parametrize(
    "topic",
    [
        "sites/x/devices/y/telemetry",
        f"sites/{SITE}/devices/{DEV}/other",
        f"sites/{SITE}/devices/{DEV}",
        f"x/{SITE}/devices/{DEV}/telemetry",
    ],
)
def test_topic_rejected(topic: str) -> None:
    with pytest.raises(Rejected) as exc:
        parse_topic(topic)
    assert exc.value.reason == "bad_topic"


def test_valid_payload() -> None:
    ts, metrics = parse_payload(payload(), NOW)
    assert ts == NOW
    assert metrics == {"active_power_kw": 12.5, "temperature_c": 61.0}


def test_unknown_metric() -> None:
    assert reason(payload(metrics={"voltage": 230})) == "unknown_metric"


def test_nan_and_inf() -> None:
    assert reason('{"ts": 1700000000, "metrics": {"current_a": NaN}}') == "bad_value"
    assert reason('{"ts": 1700000000, "metrics": {"current_a": Infinity}}') == "bad_value"


def test_out_of_range() -> None:
    assert reason(payload(metrics={"temperature_c": 900})) == "out_of_range"
    assert reason(payload(metrics={"active_power_kw": -1})) == "out_of_range"


def test_capacitive_reactive_power_is_valid() -> None:
    _, metrics = parse_payload(payload(metrics={"reactive_power_kvar": -12.5}), NOW)
    assert metrics == {"reactive_power_kvar": -12.5}


def test_stale_and_future_ts() -> None:
    assert reason(payload(ts=NOW - MAX_AGE_S - 1)) == "stale_ts"
    assert reason(payload(ts=NOW + 31)) == "future_ts"
    parse_payload(payload(ts=NOW - 299), NOW)
    parse_payload(payload(ts=NOW + 29), NOW)


def test_backlog_replayed_after_an_outage_is_not_stale() -> None:
    """The broker keeps a session's queue for SESSION_EXPIRY_S; whatever it replays must pass."""
    from hastori_ingestion.service import SESSION_EXPIRY_S

    assert MAX_AGE_S > SESSION_EXPIRY_S
    parse_payload(payload(ts=NOW - SESSION_EXPIRY_S + 10), NOW)


def test_bad_shapes() -> None:
    assert reason(b"not json") == "invalid_json"
    assert reason(b"[1,2]") == "invalid_payload"
    assert reason(b'{"ts": 1700000000}') == "invalid_payload"
    assert reason(payload(ts="now")) == "invalid_payload"
    assert reason(payload(metrics={})) == "invalid_payload"
    assert reason(payload(metrics={"current_a": "3"})) == "bad_value"


def test_unknown_extra_fields_are_ignored() -> None:
    ts, metrics = parse_payload(payload(extra=1, firmware="2.0"), NOW)
    assert ts == NOW
    assert metrics == {"active_power_kw": 12.5, "temperature_c": 61.0}


HOSTILE = [
    b'{"ts": ' + b"9" * 400 + b', "metrics": {"current_a": 1}}',  # int too big for float
    b'{"ts": 1700000000, "metrics": {"current_a": ' + b"9" * 400 + b"}}",
    b"[" * 100_000,  # deep nesting
    b'{"ts": 1700000000, "metrics": ' + b'{"a":' * 50_000 + b"1" + b"}" * 50_000 + b"}",
    bytes([0xFF, 0xFE, 0x00]),  # not valid UTF-8, contains NUL
    b'{"ts": 1700000000, "metrics": {"current_a": 1e999}}',
    b'{"ts": 1e999, "metrics": {"current_a": 1}}',
    b"x" * 5000,  # over the size limit
    b"",
    b"null",
    b'{"ts": null, "metrics": null}',
]


@pytest.mark.parametrize("raw", HOSTILE, ids=[f"hostile-{i}" for i in range(len(HOSTILE))])
def test_hostile_payloads_are_rejected_not_raised(raw: bytes) -> None:
    """Anything but Rejected would drop the MQTT connection and be redelivered forever."""
    with pytest.raises(Rejected):
        parse_payload(raw, NOW)


def test_random_bytes_never_raise_anything_but_rejected() -> None:
    import random

    rng = random.Random(7)
    alphabet = b'{}[]":,. 0123456789eE+-truefalsenullts"metrics"current_a'
    for _ in range(3000):
        raw = bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        with contextlib.suppress(Rejected):
            parse_payload(raw, NOW)


def test_fractional_timestamps_keep_millisecond_resolution() -> None:
    ts, _ = parse_payload(payload(ts=NOW + 0.1234), NOW)
    assert ts == NOW + 0.123
    assert message_id(DEV, NOW + 0.1) != message_id(DEV, NOW + 0.6)


def test_message_id_is_deterministic() -> None:
    assert message_id(DEV, 100) == message_id(DEV, 100)
    assert message_id(DEV, 100) != message_id(DEV, 102)
    assert message_id(DEV, 100) != message_id(uuid.uuid4(), 100)


def test_batch_policy_size_and_age_with_fake_clock() -> None:
    policy = BatchPolicy(max_size=500, max_age_s=1.0)
    assert not policy.should_flush(0, None, 10.0)
    assert not policy.should_flush(499, 10.0, 10.5)
    assert policy.should_flush(500, 10.0, 10.1)  # size threshold
    assert policy.should_flush(3, 10.0, 11.0)  # age threshold
    assert policy.wait_s(None, 10.0) is None
    assert policy.wait_s(10.0, 10.25) == pytest.approx(0.75)
    assert policy.wait_s(10.0, 12.0) == 0.0
