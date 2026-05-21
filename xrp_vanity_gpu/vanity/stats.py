"""Throughput counter for the vanity search loop."""
import datetime as _dt
import time


def _fmt_count(n: float) -> str:
    if n >= 1e9:
        return f"{n/1e9:.1f}G"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}K"
    return f"{n:.0f}"


class StatsPrinter:
    def __init__(self, interval_sec: float):
        self.interval = interval_sec
        self.start = time.monotonic()
        self.last_emit = self.start
        self.last_total = 0
        self.total = 0
        self.matches = 0

    def add_match(self) -> None:
        self.matches += 1

    def tick(self, processed: int, force_emit: bool = False) -> str | None:
        self.total += processed
        now = time.monotonic()
        if not force_emit and (now - self.last_emit) < self.interval:
            return None

        window = now - self.last_emit
        window_count = self.total - self.last_total
        inst = window_count / window if window > 0 else 0.0
        elapsed = now - self.start
        avg = self.total / elapsed if elapsed > 0 else 0.0

        self.last_emit = now
        self.last_total = self.total

        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"[{ts}] {_fmt_count(inst)}/s  avg {_fmt_count(avg)}/s  "
            f"total {_fmt_count(self.total)}  "
            f"matches {self.matches}  elapsed {int(elapsed)}s"
        )
