"""Unit tests for the funnel health-check monitor's transition logic.

Only the pure state machine (Monitor) and result classification are exercised
here — no network. The probe itself is a thin urllib wrapper tested in the wild.
"""

import importlib

hc = importlib.import_module("scripts.funnel_healthcheck")

ok = hc.ProbeResult(ok=True, category="ok", detail="200", elapsed_ms=42.0)


def _fail(category="tls", detail="handshake failed"):
    return hc.ProbeResult(ok=False, category=category, detail=detail, elapsed_ms=1.0)


def test_single_failure_below_threshold_does_not_trip_down():
    m = hc.Monitor(fail_threshold=2)
    # One isolated failure (the :48 SSL blip) must NOT declare the funnel down.
    assert m.record(_fail()) is None
    assert m.down is False


def test_two_consecutive_failures_trip_down_once():
    m = hc.Monitor(fail_threshold=2)
    assert m.record(_fail()) is None
    event = m.record(_fail())
    assert event is not None
    assert "DOWN" in event
    assert m.down is True
    # A third consecutive failure while already down does not re-alert.
    assert m.record(_fail()) is None


def test_recovery_emits_event_and_resets():
    m = hc.Monitor(fail_threshold=2)
    m.record(_fail())
    m.record(_fail())
    assert m.down is True
    event = m.record(ok)
    assert event is not None
    assert "RECOVERED" in event
    assert m.down is False
    assert m.consecutive_failures == 0


def test_success_before_threshold_clears_streak():
    m = hc.Monitor(fail_threshold=2)
    assert m.record(_fail()) is None
    # A success resets the streak, so the next lone failure can't trip DOWN.
    assert m.record(ok) is None
    assert m.consecutive_failures == 0
    assert m.record(_fail()) is None
    assert m.down is False


def test_classify_status_ok_and_server_error():
    assert hc.classify_status(200)[0] is True
    ok_cat = hc.classify_status(204)
    assert ok_cat[0] is True
    down, cat = hc.classify_status(503)
    assert down is False
    assert cat == "http_5xx"
    down, cat = hc.classify_status(404)
    assert down is False
    assert cat == "http_4xx"
