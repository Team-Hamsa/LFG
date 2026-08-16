# tests/economy_helpers.py
# Shared test helpers for the economy-flow suites (#107). Intentionally NO
# lfg_core import: this module only wraps sqlite3 connections, so it needs no
# env-guard preamble and never freezes config constants.


class FlakyMirrorConn:
    """Wraps a real sqlite3 connection; `execute` raises RuntimeError on any SQL
    containing `fail_on` (default: the SECOND statement of
    economy_store.set_closet_contents, so the failure lands AFTER the
    closet_assets delete executed — a genuinely half-applied, uncommitted
    transaction). Everything else, including rollback()/commit(), delegates to
    the real connection."""

    def __init__(self, real, fail_on: str = "DELETE FROM closet_bodies") -> None:
        self._real = real
        self._fail_on = fail_on

    def execute(self, sql, *args, **kwargs):
        if self._fail_on in sql:
            raise RuntimeError(f"injected mirror failure on: {self._fail_on}")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def flaky_mirror_conn(real_conn, fail_on: str = "DELETE FROM closet_bodies") -> FlakyMirrorConn:
    return FlakyMirrorConn(real_conn, fail_on)
