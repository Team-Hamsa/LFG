"""Drain/restart posture (#223): prod refuses on drain failure, staging
restarts anyway. No lfg_core imports."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import deployer


def _cfg(refuse, max_wait=30):
    return deployer.StackConfig(
        name="t",
        checkout="/nonexistent",
        branch="main",
        health_url="http://127.0.0.1:9/api/health",
        drain_max_wait=max_wait,
        drain_poll=10,
        refuse_on_drain_failure=refuse,
        restart_processes=("p1", "p2"),
        pip="pip",
    )


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, n):
        self.t += n


def _fetcher_seq(values):
    """Each call yields the next canned /api/health body (or raises)."""
    it = iter(values)

    def fetch(url):
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v

    return fetch


def test_active_sessions_parses_count():
    body = b'{"ok": true, "active_sessions": 3, "detail": {}}'
    assert deployer.active_sessions("u", fetcher=lambda u: body) == 3


def test_active_sessions_none_on_unreachable_or_malformed():
    def boom(u):
        raise OSError("down")

    assert deployer.active_sessions("u", fetcher=boom) is None
    assert deployer.active_sessions("u", fetcher=lambda u: b"not json") is None
    assert deployer.active_sessions("u", fetcher=lambda u: b'{"ok": true}') is None


def test_drain_immediate_when_zero():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 0}'])
    assert deployer.drain(_cfg(True), fetcher=f, sleeper=clock.sleep, clock=clock) == "drained"


def test_drain_waits_then_drains():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 2}', b'{"active_sessions": 0}'])
    assert deployer.drain(_cfg(True), fetcher=f, sleeper=clock.sleep, clock=clock) == "drained"
    assert clock.t == 10  # slept one poll interval


def test_drain_timeout():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 1}'] * 100)
    assert (
        deployer.drain(_cfg(True, max_wait=25), fetcher=f, sleeper=clock.sleep, clock=clock)
        == "timeout"
    )


def test_drain_unreachable_first_probe():
    clock = FakeClock()
    f = _fetcher_seq([OSError("down")])
    assert deployer.drain(_cfg(True), fetcher=f, sleeper=clock.sleep, clock=clock) == "unreachable"


def _jlist(names_status):
    return json.dumps([{"name": n, "pm2_env": {"status": s}} for n, s in names_status.items()])


def test_restart_stack_runs_pm2_per_process():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return 0

    lister = lambda: _jlist({"p1": "online", "p2": "online"})  # noqa: E731
    assert deployer.restart_stack(_cfg(True), runner=runner, lister=lister) is True
    assert calls == [
        ["pm2", "restart", "p1", "--update-env"],
        ["pm2", "restart", "p2", "--update-env"],
    ]


def test_restart_stack_reports_failure():
    lister = lambda: _jlist({"p1": "online", "p2": "online"})  # noqa: E731
    assert deployer.restart_stack(_cfg(True), runner=lambda c: 1, lister=lister) is False


def test_pm2_online_parses_status():
    data = _jlist({"p1": "online", "p2": "stopped"})
    assert deployer.pm2_online(data) == {"p1"}


def test_restart_stack_skips_stopped_processes(caplog=None):
    calls = []
    lister = lambda: _jlist({"p1": "online", "p2": "stopped"})  # noqa: E731
    ok = deployer.restart_stack(_cfg(True), runner=lambda c: calls.append(c) or 0, lister=lister)
    assert ok is True
    assert calls == [["pm2", "restart", "p1", "--update-env"]]


def test_restart_stack_all_offline_is_honest_noop(capsys):
    calls = []
    lister = lambda: _jlist({"p1": "stopped", "p2": "stopped"})  # noqa: E731
    ok = deployer.restart_stack(_cfg(True), runner=lambda c: calls.append(c) or 0, lister=lister)
    assert ok is True
    assert calls == []
    out = capsys.readouterr().out
    assert "no configured processes online" in out
    assert "nothing restarted" in out


def test_restart_stack_falls_back_to_full_list_on_jlist_failure():
    calls = []

    def boom():
        raise OSError("no pm2")

    ok = deployer.restart_stack(_cfg(True), runner=lambda c: calls.append(c) or 0, lister=boom)
    assert ok is True
    assert calls == [
        ["pm2", "restart", "p1", "--update-env"],
        ["pm2", "restart", "p2", "--update-env"],
    ]


def test_prod_refuses_on_timeout_and_unreachable():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 1}'] * 100)
    calls = []
    out = deployer.drain_and_restart(
        _cfg(True, max_wait=25),
        fetcher=f,
        runner=lambda c: calls.append(c) or 0,
        sleeper=clock.sleep,
        clock=clock,
    )
    assert out == "refused" and calls == []
    out = deployer.drain_and_restart(
        _cfg(True),
        fetcher=_fetcher_seq([OSError()]),
        runner=lambda c: calls.append(c) or 0,
        sleeper=clock.sleep,
        clock=clock,
    )
    assert out == "refused" and calls == []


def test_staging_restarts_anyway_on_timeout():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 1}'] * 100)
    calls = []

    def boom():
        raise OSError("no pm2")

    out = deployer.drain_and_restart(
        _cfg(False, max_wait=25),
        fetcher=f,
        runner=lambda c: calls.append(c) or 0,
        sleeper=clock.sleep,
        clock=clock,
        lister=boom,
    )
    assert out == "restarted" and len(calls) == 2
