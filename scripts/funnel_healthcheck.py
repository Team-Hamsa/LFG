#!/usr/bin/env python3
"""Public-edge health-check monitor for the Tailscale funnel that fronts the LFG
Activity / web surface.

Why this exists: the funnel (`letseffinggo.tail82fcc6.ts.net`) is the API base
the standalone web surface and the Xaman sign-in flow hit. When it hiccups the
backend on :8176 looks perfectly healthy while real users get a blank site or an
SSL handshake failure (observed 2026-08-06). A localhost probe would miss that
entirely, so this probes the **public HTTPS URL through the funnel** — a full
TLS handshake — the same path a browser takes.

Behaviour (all knobs are env-overridable):
  - Probe FUNNEL_HEALTH_URL every FUNNEL_HEALTH_INTERVAL seconds.
  - Every failed probe is logged (so a lone transient — the class of blip that
    hit one user — is still captured for forensics).
  - After FUNNEL_HEALTH_FAIL_THRESHOLD *consecutive* failures the funnel is
    declared DOWN (logged once); the next success logs RECOVERED. No re-alert
    while it stays down.
  - Log-file only (reports/funnel_healthcheck.log, gitignored) + stdout, which
    pm2 captures. No push alerts by design.

Run standalone:  .venv/bin/python scripts/funnel_healthcheck.py
Or under pm2 as `lfg-funnel-health` (see README / ecosystem config).
"""

from __future__ import annotations

import logging
import math
import os
import signal
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

DEFAULT_URL = "https://letseffinggo.tail82fcc6.ts.net/lfg/api/health"


@dataclass
class ProbeResult:
    ok: bool
    category: str  # ok | tls | conn | timeout | http_5xx | http_4xx | http_other
    detail: str
    elapsed_ms: float


def classify_status(status: int) -> tuple[bool, str]:
    """Map an HTTP status code to (is_ok, category)."""
    if 200 <= status < 400:
        return True, "ok"
    if 500 <= status < 600:
        return False, "http_5xx"
    if 400 <= status < 500:
        return False, "http_4xx"
    return False, "http_other"


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves HTTPS.

    The probe exists to prove the public TLS path works; silently following a
    plaintext redirect would report the funnel healthy without ever completing
    the handshake this monitor was built to watch.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "non-HTTPS redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HTTPSOnlyRedirectHandler)


def probe(url: str, timeout: float) -> ProbeResult:
    """Do one full HTTPS request and classify the outcome.

    TLS handshake failures surface as ssl.SSLError (category "tls"), which is the
    exact failure mode this monitor was built to catch.
    """
    start = time.monotonic()

    def elapsed() -> float:
        return (time.monotonic() - start) * 1000.0

    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "lfg-funnel-health/1"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 (https-pinned URL)
            ok, category = classify_status(resp.status)
            return ProbeResult(ok, category, f"HTTP {resp.status}", elapsed())
    except urllib.error.HTTPError as e:
        ok, category = classify_status(e.code)
        return ProbeResult(ok, category, f"HTTP {e.code}", elapsed())
    except ssl.SSLError as e:
        return ProbeResult(False, "tls", f"SSL handshake failure: {e}", elapsed())
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLError):
            return ProbeResult(False, "tls", f"SSL handshake failure: {reason}", elapsed())
        if isinstance(reason, (TimeoutError, TimeoutError)) or "timed out" in str(reason).lower():
            return ProbeResult(False, "timeout", f"timeout: {reason}", elapsed())
        return ProbeResult(False, "conn", f"connection failure: {reason}", elapsed())
    except TimeoutError as e:
        return ProbeResult(False, "timeout", f"timeout: {e}", elapsed())
    except Exception as e:  # pragma: no cover - defensive catch-all
        return ProbeResult(False, "conn", f"unexpected: {e!r}", elapsed())


class Monitor:
    """Consecutive-failure state machine.

    record() returns a human-readable event string on a DOWN/RECOVERED
    transition, else None. It never emits the same transition twice.
    """

    def __init__(self, fail_threshold: int = 2) -> None:
        self.fail_threshold = fail_threshold
        self.consecutive_failures = 0
        self.down = False
        self._down_since = 0.0
        self._down_streak = 0

    def record(self, result: ProbeResult) -> str | None:
        if result.ok:
            self.consecutive_failures = 0
            if self.down:
                self.down = False
                streak = self._down_streak
                return f"RECOVERED — funnel healthy again ({result.detail}, {result.elapsed_ms:.0f}ms) after {streak} failed probe(s)"
            return None

        self.consecutive_failures += 1
        if self.down:
            # Keep counting while down so RECOVERED reports the true outage
            # length, not just the probes it took to trip the threshold.
            self._down_streak = self.consecutive_failures
            return None
        if self.consecutive_failures >= self.fail_threshold:
            self.down = True
            self._down_streak = self.consecutive_failures
            return (
                f"DOWN — {self.consecutive_failures} consecutive failed probes "
                f"[{result.category}] {result.detail}"
            )
        return None


def _build_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("funnel_health")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    fh = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def _positive(name: str, raw: str, cast: Callable[[str], float]) -> float:
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {raw!r}")
    return value


def main() -> None:
    # The pm2 entry supplies no env block, so the operator's `.env` is the only
    # place FUNNEL_HEALTH_* overrides live — without this they were dead knobs.
    load_dotenv()

    url = os.environ.get("FUNNEL_HEALTH_URL", DEFAULT_URL)
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"FUNNEL_HEALTH_URL must be an https:// URL, got {url!r}")
    interval = _positive(
        "FUNNEL_HEALTH_INTERVAL", os.environ.get("FUNNEL_HEALTH_INTERVAL", "60"), float
    )
    timeout = _positive(
        "FUNNEL_HEALTH_TIMEOUT", os.environ.get("FUNNEL_HEALTH_TIMEOUT", "15"), float
    )
    threshold = int(
        _positive(
            "FUNNEL_HEALTH_FAIL_THRESHOLD", os.environ.get("FUNNEL_HEALTH_FAIL_THRESHOLD", "2"), int
        )
    )
    log_path = os.environ.get("FUNNEL_HEALTH_LOG", "reports/funnel_healthcheck.log")

    logger = _build_logger(log_path)
    monitor = Monitor(fail_threshold=threshold)

    stop = {"now": False}

    def _handle(signum: int, _frame: object) -> None:
        stop["now"] = True
        logger.info("received signal %s, shutting down", signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    logger.info(
        "funnel health-check started url=%s interval=%.0fs timeout=%.0fs fail_threshold=%d",
        url,
        interval,
        timeout,
        threshold,
    )

    while not stop["now"]:
        result = probe(url, timeout)
        if not result.ok:
            logger.warning(
                "probe FAIL [%s] %s (%.0fms)", result.category, result.detail, result.elapsed_ms
            )
        event = monitor.record(result)
        if event:
            level = logging.ERROR if monitor.down else logging.INFO
            logger.log(level, event)

        # Sleep in short slices so SIGTERM is honoured promptly.
        slept = 0.0
        while slept < interval and not stop["now"]:
            time.sleep(min(1.0, interval - slept))
            slept += 1.0

    logger.info("funnel health-check stopped")


if __name__ == "__main__":
    main()
