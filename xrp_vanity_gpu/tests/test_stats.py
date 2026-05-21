import time

from vanity.stats import StatsPrinter


def test_stats_records_and_formats():
    sp = StatsPrinter(interval_sec=0.05)
    sp.tick(processed=1_000_000)
    sp.tick(processed=1_000_000)
    time.sleep(0.06)
    line = sp.tick(processed=1_000_000, force_emit=True)
    assert line is not None
    assert "M/s" in line
    assert "matches" in line
    assert "elapsed" in line


def test_stats_skip_within_interval():
    sp = StatsPrinter(interval_sec=10.0)
    sp.tick(processed=1_000_000)
    line = sp.tick(processed=1_000_000)
    assert line is None  # too soon to emit
