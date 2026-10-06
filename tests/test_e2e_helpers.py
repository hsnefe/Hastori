"""The pure helpers of scripts/e2e.py (the checks themselves need the running stack)."""

import math

from conftest import load_script

e2e = load_script("e2e")

EXPOSITION = """
# TYPE alarm_eval_lag_seconds histogram
alarm_eval_lag_seconds_bucket{le="0.1"} 10.0
alarm_eval_lag_seconds_bucket{le="0.25"} 40.0
alarm_eval_lag_seconds_bucket{le="1.0"} 90.0
alarm_eval_lag_seconds_bucket{le="2.0"} 96.0
alarm_eval_lag_seconds_bucket{le="5.0"} 99.0
alarm_eval_lag_seconds_bucket{le="+Inf"} 100.0
alarm_eval_lag_seconds_sum 54.2
alarm_eval_lag_seconds_count 100.0
other_metric_bucket{le="1.0"} 5.0
"""


def test_buckets_are_read_from_the_exposition_format() -> None:
    buckets = e2e.histogram_buckets(EXPOSITION, "alarm_eval_lag_seconds")
    assert buckets[0.1] == 10.0 and buckets[2.0] == 96.0 and buckets[math.inf] == 100.0
    assert len(buckets) == 6  # the other metric is not mixed in


def test_the_share_within_a_limit_uses_only_what_happened_between_two_scrapes() -> None:
    before = {1.0: 50.0, 2.0: 60.0, 5.0: 70.0, math.inf: 100.0}
    after = {1.0: 130.0, 2.0: 150.0, 5.0: 190.0, math.inf: 200.0}
    # 100 new observations, 90 of them within 2 s
    assert e2e.share_within(before, after, 2.0) == 0.9


def test_no_new_observations_is_not_a_pass() -> None:
    same = {1.0: 5.0, math.inf: 5.0}
    assert e2e.share_within(same, same, 2.0) is None


def test_a_limit_below_every_bucket_edge_counts_nothing() -> None:
    assert e2e.share_within({0.5: 0.0, math.inf: 0.0}, {0.5: 4.0, math.inf: 10.0}, 0.1) == 0.0


def test_the_first_point_above_a_threshold() -> None:
    points = [
        {"time": "2026-10-06T10:00:00Z", "value": 70.0},
        {"time": "2026-10-06T10:00:02+00:00", "value": 80.0},  # equal is not above
        {"time": "2026-10-06T10:00:04.500000Z", "value": 80.1},
        {"time": "2026-10-06T10:00:06Z", "value": 90.0},
    ]
    hot = e2e.first_above(points, 80.0)
    assert hot is not None and hot.second == 4 and hot.microsecond == 500000
    assert e2e.first_above(points, 95.0) is None


def test_event_types_can_be_filtered_by_alarm() -> None:
    events = [
        {"type": "alarm.opened", "alarm_id": "a"},
        {"type": "alarm.opened", "alarm_id": "b"},
        {"type": "alarm.cleared", "alarm_id": "a"},
    ]
    assert e2e.event_types(events, "a") == ["alarm.opened", "alarm.cleared"]
    assert len(e2e.event_types(events)) == 3


async def test_wait_for_polls_until_the_probe_answers() -> None:
    calls = {"n": 0}

    async def probe() -> str | None:
        calls["n"] += 1
        return "ready" if calls["n"] == 3 else None

    assert await e2e.wait_for(probe, seconds=5, every=0.01) == "ready" and calls["n"] == 3

    async def never() -> None:
        return None

    assert await e2e.wait_for(never, seconds=0.05, every=0.01) is None
