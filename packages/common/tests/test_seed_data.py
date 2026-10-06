from hastori_common.seed_data import derive_device_password, load_seed


def test_seed_shape() -> None:
    seed = load_seed()
    assert len(seed.sites) == 2
    assert len(seed.devices) == 7
    assert len(seed.users) == 5
    assert len(seed.alarm_rules) == 2
    assert len({seed.topic(d) for d in seed.devices}) == 7


def test_device_password_is_deterministic_and_per_device() -> None:
    seed = load_seed()
    a, b = seed.devices[0], seed.devices[1]
    assert derive_device_password("s" * 8, a.id) == derive_device_password("s" * 8, a.id)
    assert derive_device_password("s" * 8, a.id) != derive_device_password("s" * 8, b.id)
    assert derive_device_password("s" * 8, a.id) != derive_device_password("t" * 8, a.id)
