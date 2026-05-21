#!/usr/bin/env python
"""GPU-accelerated XRPL vanity address search.

Usage:
    python xrp_vanity_gpu.py PATTERN [options]

PATTERN matches the prefix of the address immediately after the leading 'r'.
"""
import argparse
import datetime as dt
import signal
import sys

import numpy as np

from vanity import encoding, gpu, sieve, stats


def _legal_pattern_chars(case_sensitive: bool) -> set[str]:
    alpha = encoding.XRPL_ALPHABET.decode("ascii")
    if case_sensitive:
        return set(alpha)
    return set(alpha.lower()) | set(alpha.upper())


def _validate_pattern(pattern: str, case_sensitive: bool) -> None:
    legal = _legal_pattern_chars(case_sensitive)
    bad = [c for c in pattern if c not in legal]
    if bad:
        legal_sorted = "".join(sorted(legal))
        sys.exit(
            f"error: illegal character(s) in PATTERN: {bad!r}\n"
            f"legal characters (alphabet, case-{'sensitive' if case_sensitive else 'insensitive'}):\n"
            f"  {legal_sorted}"
        )


def _emit_match(m: sieve.Match, out_fh) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] MATCH  {m.address}  seed={m.seed_b58}  (attempt {m.attempt:,})"
    print(line, flush=True)
    if out_fh is not None:
        out_fh.write(line + "\n")
        out_fh.flush()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pattern")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--batch-size", type=int, default=1_048_576)
    p.add_argument("--out", type=str, default=None)
    p.add_argument("--max-matches", type=int, default=0,
                   help="0 = run until Ctrl-C")
    p.add_argument("--stats-interval", type=float, default=5.0)
    p.add_argument("--seed-rng-seed", type=int, default=None)
    p.add_argument("--workers", type=int, default=0,
                   help="sieve worker processes (0 = all CPU cores)")
    args = p.parse_args()

    _validate_pattern(args.pattern, args.case_sensitive)

    rng = np.random.default_rng(args.seed_rng_seed)
    g = gpu.VanityGpu(batch_size=args.batch_size)
    ps = sieve.ParallelSieve(n_workers=args.workers or None)
    sp = stats.StatsPrinter(interval_sec=args.stats_interval)

    out_fh = open(args.out, "a") if args.out else None
    stopping = False

    def _on_sigint(_signo, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, _on_sigint)

    attempt = 0
    matches_found = 0
    try:
        while not stopping:
            seeds = rng.bytes(args.batch_size * 16)
            pubkeys = g.run_batch(seeds)
            hits = ps.sieve_batch(
                pubkeys=pubkeys,
                seeds=seeds,
                pattern=args.pattern,
                case_sensitive=args.case_sensitive,
                first_attempt_index=attempt,
            )
            for h in hits:
                _emit_match(h, out_fh)
                sp.add_match()
                matches_found += 1
                if args.max_matches and matches_found >= args.max_matches:
                    stopping = True
                    break
            attempt += args.batch_size
            line = sp.tick(processed=args.batch_size)
            if line is not None:
                print(line, flush=True)
    finally:
        ps.close()
        if out_fh is not None:
            out_fh.close()
        final = sp.tick(processed=0, force_emit=True)
        if final:
            print(final, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
