from uuid import UUID

from hastori_common.seed_data import derive_device_password, load_seed


def test_seed_shape() -> None:
    seed = load_seed()
    assert len(seed.sites) == 2
    assert len(seed.devices) == 7
    assert len(seed.users) == 5
    assert len(seed.alarm_rules) == 5
    assert len({seed.topic(d) for d in seed.devices}) == 7


def test_device_password_is_deterministic_and_per_device() -> None:
    seed = load_seed()
    a, b = seed.devices[0], seed.devices[1]
    assert derive_device_password("s" * 8, a.id) == derive_device_password("s" * 8, a.id)
    assert derive_device_password("s" * 8, a.id) != derive_device_password("s" * 8, b.id)
    assert derive_device_password("s" * 8, a.id) != derive_device_password("t" * 8, a.id)


def test_seed_rules_satisfy_the_database_constraints() -> None:
    """The same rules the CHECK constraints and the unique index of migration 0005 enforce, so a
    bad seed fails here instead of at `make seed`."""
    seed = load_seed()
    seen: set[tuple[str, str, str]] = set()
    for r in seed.alarm_rules:
        if r.operator == ">":
            assert r.clear_threshold <= r.threshold, r.name
        else:
            assert r.clear_threshold >= r.threshold, r.name
        if r.kind == "reactive_ratio":
            assert r.metric == "reactive_power_kvar" and r.operator == ">", r.name
            assert r.window_s is not None and 60 <= r.window_s <= 3600, r.name
            assert seed.device_by_key(r.device).type == "energy_analyzer", r.name
        else:
            assert r.window_s is None, r.name
        if r.kind == "no_data":  # migration 0008: no thresholds, 10 to 600 seconds
            assert (r.operator, r.threshold, r.clear_threshold) == (">", 0, 0), r.name
            assert 10 <= r.duration_s <= 600, r.name
        key = (r.device, r.metric, r.kind)
        assert key not in seen, f"two rules for {key}"
        seen.add(key)
    assert len({r.id for r in seed.alarm_rules}) == len(seed.alarm_rules)


def test_an_unknown_time_zone_in_the_seed_is_refused() -> None:
    import pytest
    from pydantic import ValidationError

    from hastori_common.seed_data import SiteSeed

    with pytest.raises(ValidationError):
        SiteSeed(id=UUID(int=1), key="k", name="n", timezone="Mars/Olympus")
