#!/usr/bin/env python
"""Cancel open XUMM payloads by uuid — backlog cleanup for the open-payload
cap (2026-07-17 incident: ~95 expiry-less payloads accumulated and every
create was rejected with "Max payloads of N exceeded").

XUMM has no list endpoint, so uuids must be supplied. Every create logs
`XUMM payload <uuid>` (lfg_core/xumm_ops.py), so the usual source is the
service logs:

    grep -hoE "XUMM payload [0-9a-f-]{36}" ~/.pm2/logs/lfg-*-out.log \
      | awk '{print $3}' | sort -u \
      | .venv/bin/python scripts/cancel_xumm_payloads.py

Reads uuids from stdin (one per line) or from files passed as arguments.
Cancelling is safe to run blindly: already-signed/expired/opened payloads
report ALREADY_* and are left untouched.
"""

import argparse
import asyncio
import fileinput
import re
import sys

from lfg_core import xumm_ops

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="files of uuids (default: stdin)")
    args = parser.parse_args()

    uuids = []
    for line in fileinput.input(args.files):
        candidate = line.strip()
        if _UUID.match(candidate):
            uuids.append(candidate)
    uuids = list(dict.fromkeys(uuids))
    if not uuids:
        print("no uuids supplied", file=sys.stderr)
        return 1

    cancelled = 0
    for uuid in uuids:
        ok = await xumm_ops.cancel_xumm_payload(uuid)
        print(f"{uuid}: {'cancelled' if ok else 'skipped'}")
        cancelled += ok
        # Gentle pacing: the app may already be rate-limit-cooling.
        await asyncio.sleep(0.25)
    print(f"{cancelled}/{len(uuids)} cancelled")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
