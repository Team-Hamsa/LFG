# Tests for scripts/simulate_rarity.py (#494): the Monte Carlo rarity
# simulator must see exactly what the engine sees -- same candidate rows, same
# weights as rarity.weighted_pick -- or its projections are fiction.
import os
import sqlite3
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import config, rarity, trait_config  # noqa: E402
from scripts import simulate_rarity as sim  # noqa: E402

NET = "mainnet"


def _cfg(affinity=None, exclusions=()):
    return trait_config.TraitConfig(
        layers=(),
        z_overrides=(),
        affinity=affinity or {},
        universal_layers=frozenset(),
        swap_pairs=(),
        exclusions=tuple(exclusions),
    )


def _touch(root, *parts):
    path = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()


@pytest.fixture
def world(tmp_path):
    """App DB with 6 live males + 2 live apes, and a matching layer tree."""
    db = str(tmp_path / "app.db")
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE LFG (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        owner_address TEXT, metadata_url TEXT, image_url TEXT,
        Background TEXT, Back TEXT, Body TEXT, Clothing TEXT, Eyes TEXT,
        Eyebrows TEXT, Mouth TEXT, Hat TEXT, Accessory TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    rarity.ensure_schema(c)
    mints = [
        ("male", "Red", "Robe"),
        ("male", "Red", "Robe"),
        ("male", "Red", "Robe"),
        ("male", "Blue", "Robe"),
        ("male", "Blue", "Suit"),
        ("male", "Legacy", "Suit"),  # value with no layer art
        ("ape", "Red", "Robe"),
        ("ape", "Red", "Robe"),
    ]
    for n, (body, bg, cl) in enumerate(mints, start=1):
        c.execute(
            "INSERT INTO LFG (nft_number, Background, Clothing, network, body_type)"
            " VALUES (?, ?, ?, ?, ?)",
            (n, bg, cl, NET, body),
        )
    c.commit()
    rarity.recalculate_rarity(c, network=NET)
    # Robe is parked for males; the ape Body Type row exists via recount.
    rarity.set_enabled(c, "male", "Clothing", "Robe", False, network=NET)
    c.close()

    layers = str(tmp_path / "layers")
    for body in ("male", "ape"):
        for v in ("Red", "Blue", "Green"):
            _touch(layers, body, "Background", f"{v}.png")
    for v in ("Robe", "Suit", "Tux"):
        _touch(layers, "male", "Clothing", f"{v}.png")
    _touch(layers, "shared", "Clothing", "Robe.png")  # shared/ feeds every body
    return db, layers


def test_snapshot_mirrors_ensure_rows_and_candidates(world):
    db, layers = world
    cfg = _cfg(affinity={"Background": {"Green": ["ape"]}})
    snap = sim.load_snapshot(db, NET, layers, cfg, target_size=20)

    assert snap.live == 8
    assert snap.bodies == ["ape", "male"]
    male_bg = snap.cats[("male", "Background")]
    # Legacy row kept in the population; Green not allowed for males -> no row added.
    assert set(male_bg.traits) == {"Red", "Blue", "Legacy"}
    assert {male_bg.traits[i] for i in male_bg.candidates} == {"Red", "Blue"}
    male_cl = snap.cats[("male", "Clothing")]
    assert "Tux" in male_cl.traits  # _ensure_rows twin: floor row for new art
    assert male_cl.counts[male_cl.index["Tux"]] == 0
    assert male_cl.floors[male_cl.index["Tux"]] == config.RARITY_FLOOR
    assert {male_cl.traits[i] for i in male_cl.candidates} == {"Suit", "Tux"}  # Robe parked
    ape_bg = snap.cats[("ape", "Background")]
    assert "Green" in ape_bg.traits
    assert snap.slots_by_body["ape"] == ["Background", "Clothing"]  # shared/ Clothing


@pytest.mark.parametrize("cap", [0.0, 2.0])
def test_weights_match_real_weighted_pick(world, cap, monkeypatch):
    """Capture the weights the REAL weighted_pick hands to rng.choices and
    compare with the simulator's vectorized twin, row for row."""
    db, layers = world
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", cap)
    cfg = _cfg()
    snap = sim.load_snapshot(db, NET, layers, cfg, target_size=20)

    class Capture:
        def choices(self, traits, weights, k):
            self.got = dict(zip(traits, weights, strict=True))
            return [traits[0]]

    conn = sqlite3.connect(db)
    for body, slot in [("male", "Background"), ("male", "Clothing"), sim.BODY_KEY]:
        cat = snap.cats[(body, slot)]
        rng = Capture()
        rarity.weighted_pick(conn, body, slot, cat.allowed, network=NET, rng=rng)
        mine = dict(
            zip(
                [cat.traits[i] for i in cat.candidates],
                sim.engine_weights(cat, cat.candidates, cap),
                strict=True,
            )
        )
        assert mine.keys() == rng.got.keys()
        for t in mine:
            assert mine[t] == pytest.approx(rng.got[t], rel=1e-12)
    conn.close()


def test_parity_check_passes_on_snapshot(world):
    db, layers = world
    snap = sim.load_snapshot(db, NET, layers, _cfg(), target_size=20)
    assert sim.parity_check(snap) > 0


def _cat(counts, floors=None):
    n = len(counts)
    return sim.Category(
        traits=[f"t{i}" for i in range(n)],
        counts=np.array(counts, dtype=np.float64),
        floors=np.array(floors or [0.005] * n, dtype=np.float64),
        enabled=np.ones(n, dtype=bool),
        candidates=np.arange(n),
    )


def test_reconcile_weights_favor_deficits_and_match_targets_at_equilibrium():
    pol = sim.Policy("reconcile", alpha=1.0, horizon=100)
    targets = np.array([0.5, 0.3, 0.2])
    at_target = _cat([500, 300, 200])
    w = sim.policy_weights(pol, at_target, at_target.candidates, targets)
    assert w / w.sum() == pytest.approx(targets)

    behind = _cat([600, 300, 100])  # t2 is 100 short of target
    w = sim.policy_weights(pol, behind, behind.candidates, targets)
    p = w / w.sum()
    assert p[2] > 0.2 > p[0]
    # never below the floor, even with a surplus
    assert w.min() >= 0.005


def test_fixed_policy_is_normalized_targets():
    pol = sim.Policy("fixed", alpha=0.0)
    cat = _cat([90, 9, 1])
    w = sim.policy_weights(pol, cat, cat.candidates, sim.target_base(cat, 0.0))
    assert w == pytest.approx([1 / 3] * 3)


def test_parse_policy():
    assert sim.parse_policy("current", 0.0) == sim.Policy("cap", cap=0.0, label="current")
    assert sim.parse_policy("cap:3", 0.0).cap == 3.0
    rp = sim.parse_policy("reconcile:0.5:250", 0.0)
    assert (rp.kind, rp.alpha, rp.horizon) == ("reconcile", 0.5, 250)
    assert sim.parse_policy("reconcile:1", 0.0).horizon == sim.DEFAULT_HORIZON
    with pytest.raises(ValueError):
        sim.parse_policy("bogus", 0.0)


def test_simulate_run_is_deterministic_and_conserves_mints(world):
    db, layers = world
    snap = sim.load_snapshot(db, NET, layers, _cfg(), target_size=50)
    pol = sim.parse_policy("current", 0.0)
    a = sim.simulate_run(snap, pol, 7)
    b = sim.simulate_run(snap, pol, 7)
    for key in snap.cats:
        assert np.array_equal(a["counts"][key], b["counts"][key])
    assert a["failures"] == 0
    assert a["counts"][sim.BODY_KEY].sum() == 50
    # every minted character got exactly one Background
    minted = 50 - 8
    bg_new = sum(
        a["counts"][(b_, "Background")].sum() - snap.cats[(b_, "Background")].counts.sum()
        for b_ in snap.bodies
    )
    assert bg_new == minted
    # parked Robe never grows for males
    cl = snap.cats[("male", "Clothing")]
    assert a["counts"][("male", "Clothing")][cl.index["Robe"]] == cl.counts[cl.index["Robe"]]
    # snapshot itself untouched
    assert snap.cats[sim.BODY_KEY].counts.sum() == 8


def test_exclusions_filter_candidates_per_mint(world):
    db, layers = world
    cfg = _cfg(
        exclusions=[
            {
                "trait_type": "Background",
                "value": "Red",
                "excludes": [{"trait_type": "Clothing", "values": ["Suit"]}],
            }
        ]
    )
    snap = sim.load_snapshot(db, NET, layers, cfg, target_size=200)
    assert snap.has_exclusions
    pol = sim.Policy("fixed", alpha=0.0, label="fixed:0")
    res = sim.simulate_run(snap, pol, 3, cfg)
    # Under fixed-uniform odds, Red males can only take Tux: Suit growth must
    # come only from non-Red males, so Suit < Tux in expectation. Run to 200
    # makes a flip astronomically unlikely.
    cl = snap.cats[("male", "Clothing")]
    grown = res["counts"][("male", "Clothing")] - cl.counts
    assert grown[cl.index["Tux"]] > grown[cl.index["Suit"]]
