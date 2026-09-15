# lfg_core/log_hygiene.py — keep secrets out of process logs.
#
# httpx logs every request line ("HTTP Request: GET <url> ...") at INFO, and
# every long-running process calls logging.basicConfig(level=INFO), so those
# lines reach the pm2 logs. python-telegram-bot sends each Bot API call to
# https://api.telegram.org/bot<TOKEN>/<method>, so the Telegram surface wrote
# its full bot token to its error log on every getUpdates poll. Raising the
# HTTP-client loggers to WARNING drops the per-request lines while keeping
# their warnings/errors. httpcore only logs at DEBUG today; it is pinned too so
# a future root-level DEBUG can't reopen the leak one layer down.
import logging

HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def quiet_http_client_loggers() -> None:
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
