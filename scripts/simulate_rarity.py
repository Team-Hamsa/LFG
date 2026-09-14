#!/usr/bin/env python3
"""Monte Carlo "dice roller" for the variable-rarity engine (#494).

Mints the collection forward from today's live snapshot to
MAX_COLLECTION_SIZE, many times over, and reports where every trait's share
of the collection ends up. Each simulated mint follows the real
select_random_attributes shape: weighted Body Type pick, then one weighted
pick per TRAIT_ORDER slot among the layer-tree values the trait_config rules
allow, with counts fed back after every mint (what recalculate_rarity does).

Weights are a vectorized twin of rarity.effective_weight; the twin is
parity-checked against the real function on the loaded snapshot at startup
and the run aborts on any mismatch, so the simulation can't silently drift
from the engine.

READ-ONLY: the app DB is copied into memory (sqlite backup) and recounted
there. Nothing is written back.

Not modelled: boosts (time-based; armed-but-dormant boosts are counted and
reported), burns / harvests / swaps, future admin enable/disable, headroom
reservations. Shop mints don't feed mint odds, so they're irrelevant here.

Policies (--policy, repeatable; default: current, cap:2, reconcile:1, reconcile:0.5):
  current                    today's engine (share cap = RARITY_CAP_MULTIPLE)
  cap:<m>                    share ceiling at m x fair share (#198)
  fixed:<alpha>              fixed odds = target shares, no feedback (baseline)
  reconcile:<alpha>[:<h>]    target-based reconciliation (#494 option B):
                             weight = max(need / h, floor), need =
                             max(target x (M + h) - count, 0), M = candidate
                             count total, h = horizon in mints (default 500)
Targets for fixed/reconcile: target_i ∝ (today's smoothed share_i)^alpha over
the enabled candidates -- alpha=1 freezes today's mix, alpha=0 is uniform.

  python scripts/simulate_rarity.py --runs 200 \\
      --db ~/LFG/lfg_nfts.db --layers-dir ~/LFG/layers
  python scripts/simulate_rarity.py --detail male/Clothing --show-run 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from multiprocessing import get_context
from typing import Any

import numpy as np
import numpy.typing as npt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core.envload import load_dotenv_unless_skipped  # noqa: E402

load_dotenv_unless_skipped()

from lfg_core import config, db_path, rarity, trait_config  # noqa: E402
from lfg_core.swap_meta import TRAIT_ORDER  # noqa: E402

DEFAULT_POLICIES = ["current", "cap:2", "reconcile:1", "reconcile:0.5"]
DEFAULT_HORIZON = 500
BODY_KEY = (rarity.BODY_SENTINEL, rarity.BODY_CATEGORY)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class Category:
    """One (body, category) population, as weighted_pick sees it."""

    traits: list[str]
    counts: FloatArray  # live_count per row
    floors: FloatArray
    enabled: BoolArray
    # Row indices weighted_pick would consider: enabled AND in the allowed
    # layer-tree values. Static when trait_config has no exclusions.
    candidates: IntArray
    allowed: list[str] = field(default_factory=list)

    @property
    def index(self) -> dict[str, int]:
        return {t: i for i, t in enumerate(self.traits)}


@dataclass
class Snapshot:
    network: str
    live: int
    target_size: int
    cats: dict[tuple[str, str], Category]
    bodies: list[str]
    slots_by_body: dict[str, list[str]]  # slots that have layer values for the body
    dormant_boosts: int
    cap_multiple: float
    has_exclusions: bool


def _add_missing_rows(cat_rows: dict[str, list[Any]], allowed: list[str]) -> None:
    """Mirror rarity._ensure_rows: allowed values with no row get a live=0,
    floor=RARITY_FLOOR, enabled row before the first pick."""
    for v in allowed:
        if v not in cat_rows:
            cat_rows[v] = [0, config.RARITY_FLOOR, 1]


def _build_category(rows: dict[str, list[Any]], allowed: list[str]) -> Category:
    _add_missing_rows(rows, allowed)
    traits = list(rows)
    counts = np.array([rows[t][0] for t in traits], dtype=np.float64)
    floors = np.array([rows[t][1] for t in traits], dtype=np.float64)
    enabled = np.array([bool(rows[t][2]) for t in traits])
    allowed_set = set(allowed)
    cand = np.array(
        [i for i, t in enumerate(traits) if enabled[i] and t in allowed_set], dtype=np.int64
    )
    return Category(traits, counts, floors, enabled, cand, list(allowed))


def load_snapshot(
    db_file: str,
    network: str,
    layers_dir: str,
    cfg: trait_config.TraitConfig,
    target_size: int,
) -> Snapshot:
    src = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:")
    src.backup(mem)
    src.close()
    # Fresh counts, exactly as the engine would see them after its stale check.
    rarity.recalculate_rarity(mem, network=network)
    where, params = rarity._live_where(network)
    (live,) = mem.execute(f"SELECT COUNT(*) FROM LFG WHERE {where}", params).fetchone()
    by_key: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for body, cat, trait, n, floor, en in mem.execute(
        "SELECT body, category, trait, live_count, floor_weight, enabled"
        " FROM trait_rarity WHERE network=? ORDER BY body, category, trait",
        (network,),
    ):
        by_key.setdefault((body, cat), {})[trait] = [n, floor, en]
    (dormant,) = mem.execute(
        "SELECT COUNT(*) FROM trait_rarity WHERE network=? AND enabled=1"
        " AND boost_initial IS NOT NULL AND boost_started_at IS NULL",
        (network,),
    ).fetchone()
    mem.close()

    from lfg_core.layer_store import LocalLayerStore

    store = LocalLayerStore(layers_dir)
    bodies = asyncio.run(store.list_bodies())
    cats: dict[tuple[str, str], Category] = {}
    cats[BODY_KEY] = _build_category(by_key.get(BODY_KEY, {}), bodies)
    slots_by_body: dict[str, list[str]] = {}
    for body in bodies:
        slots_by_body[body] = []
        for slot in TRAIT_ORDER:
            raw = store.list_values_sync(body, slot)
            if not raw:
                continue  # layer absent on this body: slot omitted, as in mint
            allowed = [v for v in raw if cfg.value_allowed(body, slot, v)]
            cats[(body, slot)] = _build_category(by_key.get((body, slot), {}), allowed)
            slots_by_body[body].append(slot)
    return Snapshot(
        network=network,
        live=int(live),
        target_size=target_size,
        cats=cats,
        bodies=bodies,
        slots_by_body=slots_by_body,
        dormant_boosts=int(dormant),
        cap_multiple=config.RARITY_CAP_MULTIPLE,
        has_exclusions=bool(cfg.exclusions),
    )


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    kind: str  # current | cap | fixed | reconcile
    cap: float = 0.0
    alpha: float = 1.0
    horizon: int = DEFAULT_HORIZON
    label: str = ""


def parse_policy(spec: str, default_cap: float) -> Policy:
    parts = spec.split(":")
    kind = parts[0]
    if kind == "current" and len(parts) == 1:
        return Policy("cap", cap=default_cap, label="current")
    if kind == "cap" and len(parts) == 2:
        return Policy("cap", cap=float(parts[1]), label=spec)
    if kind == "fixed" and len(parts) == 2:
        return Policy("fixed", alpha=float(parts[1]), label=spec)
    if kind == "reconcile" and len(parts) in (2, 3):
        h = int(parts[2]) if len(parts) == 3 else DEFAULT_HORIZON
        if h < 1:
            raise ValueError(f"horizon must be >= 1 in {spec!r}")
        return Policy("reconcile", alpha=float(parts[1]), horizon=h, label=spec)
    raise ValueError(f"bad --policy {spec!r}")


def engine_weights(cat: Category, cand: IntArray, cap_multiple: float) -> FloatArray:
    """Vectorized rarity.effective_weight (boost = 1) for the candidate rows,
    with weighted_pick's arguments: category_total and population_size over
    the WHOLE (body, category) population, candidate_count = len(cand)."""
    total = cat.counts.sum()
    population = len(cat.traits)
    share = (cat.counts[cand] + 1.0) / (total + population)
    floors = cat.floors[cand]
    base = np.maximum(share, floors)
    k = len(cand)
    if cap_multiple > 0 and k > 0:
        ceiling = np.maximum(cap_multiple / k, floors)
        base = np.minimum(base, ceiling)
    out: FloatArray = base
    return out


def target_base(cat: Category, alpha: float) -> FloatArray:
    """Unnormalized per-row targets from the snapshot: smoothed share^alpha.
    Frozen at load time -- targets don't move as the simulation mints."""
    total = cat.counts.sum()
    share = (cat.counts + 1.0) / (total + len(cat.traits))
    out: FloatArray = np.power(share, alpha)
    return out


def policy_weights(
    policy: Policy, cat: Category, cand: IntArray, targets: FloatArray | None
) -> FloatArray:
    if policy.kind == "cap":
        return engine_weights(cat, cand, policy.cap)
    assert targets is not None
    t: FloatArray = targets[cand]
    t = t / t.sum()
    if policy.kind == "fixed":
        return t
    n = cat.counts[cand]
    h = float(policy.horizon)
    need = np.maximum(t * (n.sum() + h) - n, 0.0)
    out: FloatArray = np.maximum(need / h, cat.floors[cand])
    return out


def parity_check(snap: Snapshot) -> int:
    """Assert the vectorized weights equal rarity.effective_weight on every
    category of the real snapshot, with and without a cap. Returns rows checked."""
    now = rarity.utcnow()
    checked = 0
    for cat in snap.cats.values():
        cand = cat.candidates
        if len(cand) == 0:
            continue
        total = int(cat.counts.sum())
        for cap in (0.0, 2.0):
            vec = engine_weights(cat, cand, cap)
            ref = [
                rarity.effective_weight(
                    int(cat.counts[i]),
                    total,
                    float(cat.floors[i]),
                    None,
                    None,
                    None,
                    now,
                    population_size=len(cat.traits),
                    candidate_count=len(cand),
                    cap_multiple=cap,
                )
                for i in cand
            ]
            if not np.allclose(vec, ref, rtol=1e-12, atol=1e-15):
                raise AssertionError(f"weight parity failed for {cat.traits[:3]}… cap={cap}")
            checked += len(cand)
    return checked


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def _pick(rng: np.random.Generator, cand: IntArray, w: FloatArray) -> int:
    cum = np.cumsum(w)
    return int(cand[min(np.searchsorted(cum, rng.random() * cum[-1], side="right"), len(cand) - 1)])


def simulate_run(
    snap: Snapshot,
    policy: Policy,
    seed: int | np.random.SeedSequence,
    cfg: trait_config.TraitConfig | None = None,
) -> dict[str, Any]:
    """One run to target_size. Returns final counts per category plus the
    number of mints that would have failed ('All traits disabled' / rules
    leave no legal value -- the engine raises on both)."""
    rng = np.random.default_rng(seed)
    cats = {
        k: Category(c.traits, c.counts.copy(), c.floors, c.enabled, c.candidates, c.allowed)
        for k, c in snap.cats.items()
    }
    targets = (
        {k: target_base(c, policy.alpha) for k, c in snap.cats.items()}
        if policy.kind != "cap"
        else {}
    )
    dynamic = snap.has_exclusions and cfg is not None
    failures = 0
    for _ in range(max(snap.target_size - snap.live, 0)):
        bcat = cats[BODY_KEY]
        if len(bcat.candidates) == 0:
            failures += 1
            continue
        bi = _pick(
            rng,
            bcat.candidates,
            policy_weights(policy, bcat, bcat.candidates, targets.get(BODY_KEY)),
        )
        body = bcat.traits[bi]
        picks: list[tuple[Category, int]] = []
        attrs: list[dict[str, str]] = []
        failed = False
        for slot in snap.slots_by_body.get(body, []):
            cat = cats[(body, slot)]
            cand = cat.candidates
            if dynamic:
                assert cfg is not None
                cand = np.array(
                    [i for i in cand if not cfg.conflicts(attrs, slot, cat.traits[i])],
                    dtype=np.int64,
                )
            if len(cand) == 0:
                failed = True
                break
            i = _pick(rng, cand, policy_weights(policy, cat, cand, targets.get((body, slot))))
            picks.append((cat, i))
            attrs.append({"trait_type": slot, "value": cat.traits[i]})
        if failed:
            failures += 1
            continue
        bcat.counts[bi] += 1
        for cat, i in picks:
            cat.counts[i] += 1
    return {"counts": {k: c.counts for k, c in cats.items()}, "failures": failures}


_WORKER: dict[str, Any] = {}


def _worker(args: tuple[int, int, np.random.SeedSequence]) -> tuple[int, int, dict[str, Any]]:
    p_idx, run_idx, seed = args
    snap, policies, cfg = _WORKER["snap"], _WORKER["policies"], _WORKER["cfg"]
    return p_idx, run_idx, simulate_run(snap, policies[p_idx], seed, cfg)


def run_all(
    snap: Snapshot,
    policies: list[Policy],
    runs: int,
    seed: int,
    workers: int,
    cfg: trait_config.TraitConfig,
) -> list[list[dict[str, Any]]]:
    seeds = np.random.SeedSequence(seed).spawn(runs)
    # Same seed per run index across policies: common random numbers make the
    # policy comparison sharper.
    jobs = [(p, r, seeds[r]) for p in range(len(policies)) for r in range(runs)]
    out: list[list[dict[str, Any]]] = [[{} for _ in range(runs)] for _ in policies]
    _WORKER.update(snap=snap, policies=policies, cfg=cfg)
    if workers <= 1:
        for job in jobs:
            p, r, res = _worker(job)
            out[p][r] = res
        return out
    with get_context("fork").Pool(workers) as pool:
        for p, r, res in pool.imap_unordered(_worker, jobs, chunksize=1):
            out[p][r] = res
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def shares(counts: FloatArray) -> FloatArray:
    total = counts.sum(axis=-1, keepdims=True)
    out: FloatArray = np.divide(counts, total, out=np.zeros_like(counts), where=total > 0)
    return out


def eff_n(p: FloatArray) -> FloatArray:
    """Effective number of traits, 1 / Σp² (higher = more even)."""
    s = (p * p).sum(axis=-1)
    out: FloatArray = np.divide(1.0, s, out=np.zeros_like(s), where=s > 0)
    return out


def pct(x: float) -> str:
    return f"{x * 100:5.2f}%"


def bar(x: float, scale: float, width: int = 30) -> str:
    n = int(round(width * x / scale)) if scale > 0 else 0
    return "█" * min(n, width)


def summarize(
    snap: Snapshot, policies: list[Policy], results: list[list[dict[str, Any]]]
) -> dict[str, Any]:
    """Per policy, per category: today's shares, final shares matrix (runs × rows)."""
    summary: dict[str, Any] = {"policies": {}}
    for p, pol in enumerate(policies):
        per_cat = {}
        for key, cat in snap.cats.items():
            final = np.stack([res["counts"][key] for res in results[p]])
            per_cat[key] = {
                "today": shares(cat.counts),
                "final": shares(final),
                "final_counts": final,
            }
        summary["policies"][pol.label] = {
            "cats": per_cat,
            "failures": [res["failures"] for res in results[p]],
        }
    return summary


def print_report(
    snap: Snapshot,
    policies: list[Policy],
    summary: dict[str, Any],
    runs: int,
    detail: str | None,
    show_run: int | None,
    movers: int,
    min_live: int,
) -> None:
    labels = [p.label for p in policies]
    mints = max(snap.target_size - snap.live, 0)
    print(f"\n=== Rarity simulation — {snap.network} ===")
    print(
        f"live today {snap.live}  →  {snap.target_size}  ({mints} simulated mints)  ×  {runs} runs"
        f"  ×  policies: {', '.join(labels)}"
    )
    print(
        f"env share cap (RARITY_CAP_MULTIPLE) = {snap.cap_multiple:g}   "
        f"armed-but-dormant boosts not modelled: {snap.dormant_boosts}"
    )
    for lab in labels:
        f = summary["policies"][lab]["failures"]
        if any(f):
            print(
                f"  ⚠ {lab}: {np.mean(f):.1f} mints/run would FAIL (all candidates disabled / rules)"
            )

    # Body Type
    key = BODY_KEY
    cat = snap.cats[key]
    print("\n── Body Type (share of whole collection; mean [p5–p95] at final size) ──")
    head = f"{'body':12} {'today':>7}"
    for lab in labels:
        head += f" │ {lab:^24}"
    print(head)
    for i in np.argsort(-cat.counts):
        line = f"{cat.traits[i]:12} {pct(summary['policies'][labels[0]]['cats'][key]['today'][i])}"
        for lab in labels:
            fin = summary["policies"][lab]["cats"][key]["final"][:, i]
            line += f" │ {pct(fin.mean())} [{pct(np.percentile(fin, 5)).strip()}–{pct(np.percentile(fin, 95)).strip()}]"
        print(line)

    # Evenness per slot
    print(
        f"\n── Evenness per body/slot (effective # of traits = 1/Σshare²; higher = more even; "
        f"slots with ≥{min_live} live today) ──"
    )
    head = f"{'body/slot':22} {'traits':>6} {'top today':>22} {'effN':>5}"
    for lab in labels:
        head += f" │ {lab[:14]:>14} effN  top"
    print(head)
    for (body, slot), cat in snap.cats.items():
        if (body, slot) == BODY_KEY or cat.counts.sum() < min_live:
            continue
        today = shares(cat.counts)
        ti = int(np.argmax(today))
        line = (
            f"{body + '/' + slot:22} {len(cat.candidates):>3}/{len(cat.traits):<3}"
            f"{cat.traits[ti][:13]:>14} {pct(today[ti])} {eff_n(today):5.1f}"
        )
        for lab in labels:
            fin = summary["policies"][lab]["cats"][(body, slot)]["final"]
            line += f" │ {'':>14} {eff_n(fin).mean():4.1f} {pct(fin.max(axis=1).mean())}"
        print(line)

    # Movers
    for lab in labels:
        rows = []
        for (body, slot), c in summary["policies"][lab]["cats"].items():
            if snap.cats[(body, slot)].counts.sum() < min_live:
                continue
            delta = c["final"].mean(axis=0) - c["today"]
            for i in range(len(delta)):
                rows.append((abs(delta[i]), body, slot, i, delta[i], c))
        rows.sort(key=lambda r: -r[0])
        print(
            f"\n── Biggest movers: {lab} (share within body/slot, today → final mean [p5–p95]) ──"
        )
        for _, body, slot, i, d, c in rows[:movers]:
            cat = snap.cats[(body, slot)]
            fin = c["final"][:, i]
            state = "" if cat.enabled[i] else "  (disabled)"
            print(
                f"  {body + '/' + slot:22} {cat.traits[i][:24]:24} {pct(c['today'][i])} → "
                f"{pct(fin.mean())} [{pct(np.percentile(fin, 5)).strip()}–"
                f"{pct(np.percentile(fin, 95)).strip()}]  {d * 100:+.2f}pp{state}"
            )

    # Snowball flags: wide run-to-run spread = luck decides the outcome
    print("\n── Luck-dominated outcomes (p95 − p5 spread ≥ 10pp across runs) ──")
    any_flag = False
    for lab in labels:
        for (body, slot), c in summary["policies"][lab]["cats"].items():
            fin = c["final"]
            spread = np.percentile(fin, 95, axis=0) - np.percentile(fin, 5, axis=0)
            for i in np.nonzero(spread >= 0.10)[0]:
                any_flag = True
                cat = snap.cats[(body, slot)]
                print(
                    f"  {lab:16} {body + '/' + slot:22} {cat.traits[i][:24]:24} today "
                    f"{pct(c['today'][i])}  final [{pct(np.percentile(fin[:, i], 5)).strip()}–"
                    f"{pct(np.percentile(fin[:, i], 95)).strip()}]  (live today {int(cat.counts.sum())})"
                )
    if not any_flag:
        print("  none")

    if detail:
        body, _, slot = detail.partition("/")
        key = (body, slot)
        if key not in snap.cats:
            print(f"\n(no category {detail!r}; try e.g. male/Clothing or '*/Body Type')")
            return
        cat = snap.cats[key]
        print(f"\n── Detail: {detail} ({int(cat.counts.sum())} live today) ──")
        head = f"{'trait':26} {'today':>7}"
        for lab in labels:
            head += f" │ {lab[:22]:^22}"
        print(head)
        for i in np.argsort(-cat.counts):
            line = f"{cat.traits[i][:24] + ('' if cat.enabled[i] else ' ⏸'):26} {pct(shares(cat.counts)[i])}"
            for lab in labels:
                fin = summary["policies"][lab]["cats"][key]["final"][:, i]
                line += f" │ {pct(fin.mean())} [{pct(np.percentile(fin, 5)).strip()}–{pct(np.percentile(fin, 95)).strip()}]"
            print(line)
        if show_run is not None:
            r = show_run % runs
            for lab in labels:
                fin = summary["policies"][lab]["cats"][key]["final"][r]
                counts = summary["policies"][lab]["cats"][key]["final_counts"][r]
                scale = max(fin.max(), shares(cat.counts).max())
                print(f"\n  🎲 run #{r} · {lab} · {detail}  (▒ today, █ this run's final)")
                for i in np.argsort(-fin)[:25]:
                    t = shares(cat.counts)[i]
                    print(
                        f"    {cat.traits[i][:24]:24} {pct(fin[i])} {int(counts[i]):5}  "
                        f"{bar(fin[i], scale):30}\n    {'':24} {pct(t)} {int(cat.counts[i]):5}  "
                        f"{bar(t, scale).replace('█', '▒')}"
                    )


def write_json(path: str, snap: Snapshot, summary: dict[str, Any]) -> None:
    doc: dict[str, Any] = {
        "network": snap.network,
        "live": snap.live,
        "target_size": snap.target_size,
        "policies": {},
    }
    for lab, pol in summary["policies"].items():
        cats = {}
        for (body, slot), c in pol["cats"].items():
            fin = c["final"]
            cats[f"{body}/{slot}"] = {
                t: {
                    "today": float(c["today"][i]),
                    "mean": float(fin[:, i].mean()),
                    "p5": float(np.percentile(fin[:, i], 5)),
                    "p95": float(np.percentile(fin[:, i], 95)),
                }
                for i, t in enumerate(snap.cats[(body, slot)].traits)
            }
        doc["policies"][lab] = {"failures_mean": float(np.mean(pol["failures"])), "cats": cats}
    with open(path, "w") as f:
        json.dump(doc, f, indent=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--network", default=config.XRPL_NETWORK)
    ap.add_argument("--db", default=None, help="app DB (default: db_path.app_db_path(network))")
    ap.add_argument("--layers-dir", default=config.LAYERS_DIR)
    ap.add_argument("--trait-config", default=None, help="trait_config.yaml (default: repo copy)")
    ap.add_argument("--target-size", type=int, default=config.MAX_COLLECTION_SIZE)
    ap.add_argument("--runs", type=int, default=100)
    ap.add_argument("--seed", type=int, default=494)
    ap.add_argument("--policy", action="append", default=None)
    ap.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) - 2, 1))
    ap.add_argument("--detail", default=None, help="body/slot to print in full, e.g. male/Clothing")
    ap.add_argument(
        "--show-run", type=int, default=None, help="with --detail: bar chart of one run"
    )
    ap.add_argument("--movers", type=int, default=12)
    ap.add_argument("--min-live", type=int, default=100, help="hide slots smaller than this")
    ap.add_argument("--json", default=None, help="write per-trait results here")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.layers_dir):
        ap.error(f"layers dir not found: {args.layers_dir} (pass --layers-dir ~/LFG/layers)")
    db_file = os.path.expanduser(args.db or db_path.app_db_path(args.network))
    if not os.path.exists(db_file):
        ap.error(f"app DB not found: {db_file}")
    cfg = trait_config.load_config(args.trait_config or trait_config.DEFAULT_CONFIG_PATH)

    t0 = time.monotonic()
    snap = load_snapshot(db_file, args.network, args.layers_dir, cfg, args.target_size)
    checked = parity_check(snap)
    policies = [parse_policy(s, snap.cap_multiple) for s in (args.policy or DEFAULT_POLICIES)]
    print(
        f"snapshot: {len(snap.cats)} categories, parity vs rarity.effective_weight OK "
        f"({checked} weights) — simulating…",
        file=sys.stderr,
    )
    results = run_all(snap, policies, args.runs, args.seed, args.workers, cfg)
    summary = summarize(snap, policies, results)
    print_report(
        snap, policies, summary, args.runs, args.detail, args.show_run, args.movers, args.min_live
    )
    if args.json:
        write_json(args.json, snap, summary)
        print(f"\nwrote {args.json}")
    print(f"\n({time.monotonic() - t0:.0f}s, seed {args.seed})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
