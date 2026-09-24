from c2c.ferry.dedup import Dedup


def test_first_sight_false_then_true():
    clock = [1000]
    d = Dedup(window_ms=500, now_ms=lambda: clock[0])
    assert d.seen("a") is False
    assert d.seen("a") is True


def test_expires_after_window():
    clock = [1000]
    d = Dedup(window_ms=500, now_ms=lambda: clock[0])
    d.seen("a")
    clock[0] = 1600  # 600 > 500 window
    assert d.seen("a") is False  # forgotten


def test_distinct_ids_independent():
    d = Dedup(window_ms=500, now_ms=lambda: 1000)
    assert d.seen("a") is False
    assert d.seen("b") is False
