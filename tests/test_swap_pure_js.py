# tests/test_swap_pure_js.py
# Trait Swapper before/after preview logic, kept in the pure module
# webapp/client/swap_pure.js and executed here under Node — same harness as
# tests/test_build_pure_js.py. The preview is only worth showing if it draws
# what the swap will actually compose, so beyond the unit cases every rule is
# checked for PARITY against the server code it mirrors:
#   swapAllowed      <- trait_config.TraitConfig.swap_allowed
#   swapResults      <- swap_meta.swap_traits
#   withApeExtras    <- ape_face.inject_and_mask (nose + melt/xray masking),
#                       stacked by build_pure.orderedLayers <- swap_compose._canonical
#   rolledFaceSlots  <- traits.fill_missing_face_traits' "empty" rule
import asyncio
import itertools
import json
import os
import random
import re
import shutil
import subprocess

import pytest

from lfg_core import ape_face, swap_compose, swap_meta, trait_config, traits
from lfg_service import app as server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/swap_pure.js"
BUILD_REL = "./webapp/client/build_pure.js"
BODIES = ["ape", "female", "male", "milady", "skeleton"]

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    """Run `expr` with the module imported as `M` (and build_pure as `B`)."""
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"import * as B from {json.dumps(BUILD_REL)};\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert proc.returncode == 0, f"node script failed:\n{script}\n--- stderr ---\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(autouse=True)
def _real_trait_config():
    # Parity is against the checked-in trait_config.yaml, never a fixture a
    # previous module left loaded.
    trait_config.reset_config()
    yield
    trait_config.reset_config()


def _preview_cfg():
    return server._swap_preview_config(trait_config.get_config())


def _attrs(values):
    """A normalized attribute list (every TRAIT_ORDER slot, in order)."""
    return [{"trait_type": t, "value": values.get(t, "None")} for t in swap_meta.TRAIT_ORDER]


# ---------------------------------------------------------------------------
# swapAllowed / offeredTraits <- TraitConfig.swap_allowed
# ---------------------------------------------------------------------------


def test_swap_allowed_matches_the_server_for_every_body_pair_and_layer():
    cfg = trait_config.get_config()
    matrix = server._swap_matrix(cfg)
    cases = [[a, b, layer] for a in BODIES for b in BODIES for layer in swap_meta.SWAPPABLE_TRAITS]
    got = run_js(
        f"{json.dumps(cases)}.map(([a, b, l]) => M.swapAllowed({json.dumps(matrix)}, a, b, l))"
    )
    want = [cfg.swap_allowed(a, b, layer) for a, b, layer in cases]
    assert got == want
    # Sanity: the matrix is not trivially all-True / all-False.
    assert True in want and False in want


def test_offered_traits_filters_to_the_pair_and_keeps_order():
    matrix = server._swap_matrix(trait_config.get_config())
    got = run_js(
        f"M.offeredTraits({json.dumps(swap_meta.SWAPPABLE_TRAITS)}, "
        f"{json.dumps(matrix)}, 'ape', 'skeleton')"
    )
    # ape/skeleton: Head + Clothing, plus the universal Accessory/Back.
    assert got == ["Back", "Clothing", "Head", "Accessory"]


def test_offered_traits_without_a_matrix_offers_everything():
    got = run_js(f"M.offeredTraits({json.dumps(swap_meta.SWAPPABLE_TRAITS)}, null, 'ape', 'male')")
    assert got == swap_meta.SWAPPABLE_TRAITS


# ---------------------------------------------------------------------------
# swapResults <- swap_meta.swap_traits
# ---------------------------------------------------------------------------


def test_swap_results_exchanges_only_the_picked_slots():
    a = _attrs({"Body": "Straight Tan", "Head": "Crown", "Eyes": "Blue"})
    b = _attrs({"Body": "Curved Pale", "Head": "Cap", "Eyes": "Green"})
    got = run_js(
        f"M.swapResults({json.dumps(swap_meta.TRAIT_ORDER)}, {json.dumps(a)}, "
        f"{json.dumps(b)}, ['Head'])"
    )
    assert got[0]["Head"] == "Cap" and got[1]["Head"] == "Crown"
    assert got[0]["Eyes"] == "Blue" and got[1]["Eyes"] == "Green"
    assert got[0]["Body"] == "Straight Tan" and got[1]["Body"] == "Curved Pale"


def test_swap_results_match_the_server_swap():
    rng = random.Random(1762)
    cases = []
    for _ in range(60):

        def pick(tag):
            return {
                t: rng.choice(["None", "", f"{t}-{tag}-{rng.randint(0, 3)}"])
                for t in swap_meta.TRAIT_ORDER
            }

        a, b = _attrs(pick("a")), _attrs(pick("b"))
        if rng.random() < 0.2:  # a slot missing from the list entirely
            a = [x for x in a if x["trait_type"] != "Head"]
        chosen = rng.sample(swap_meta.SWAPPABLE_TRAITS, rng.randint(0, 4))
        cases.append([a, b, chosen])
    order = json.dumps(swap_meta.TRAIT_ORDER)
    got = run_js(f"{json.dumps(cases)}.map(([a, b, t]) => M.swapResults({order}, a, b, t))")
    for (a, b, chosen), (va, vb) in zip(cases, got, strict=True):
        na, nb = swap_meta.swap_traits(a, b, chosen)
        assert va == {x["trait_type"]: x["value"] for x in na}
        assert vb == {x["trait_type"]: x["value"] for x in nb}


# ---------------------------------------------------------------------------
# withApeExtras <- ape_face.inject_and_mask (over swap_compose._canonical)
# ---------------------------------------------------------------------------

APE_BODIES = ["Ape Gold", *sorted(ape_face.MASKED_BODY_VALUES)]
FACE_VALUES = {
    "Eyes": [
        "Blue",
        "None",
        "",
        *sorted(ape_face.NOSE_BELOW_EYES_VALUES),
        *[t["value"] for t in ape_face.TOP_TRAITS if t["trait_type"] == "Eyes"],
    ],
    "Mouth": ["Smile", "None", "Rainbow Puke"],
    "Eyebrows": ["Thick", "None"],
    "Head": ["Crown", "None"],
    "Accessory": ["Cigar", "Retardio", "None"],
}


def _server_plan(values, body, monkeypatch):
    """What compose_nft actually stacks: _canonical's z-sorted layers run
    through the REAL inject_and_mask, with the store and the pixel masking
    stubbed so no art is needed — a masked layer comes back tagged."""
    monkeypatch.setattr(ape_face, "apply_alpha_mask", lambda p, _m, _o: p + "#masked")

    class _Store:
        async def resolve_asset(self, rel_path):
            return rel_path

    attrs = _attrs(values)
    layers = [
        (a["trait_type"], a["value"], f"{a['trait_type']}/{a['value']}")
        for a in swap_compose._canonical(attrs)
    ]
    body_value = swap_meta.get_attr(attrs, "Body") or ""
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(
            ape_face.inject_and_mask(layers, body, body_value, _Store(), "unused")
        )
    finally:
        loop.close()
    return [[t, v, p.endswith("#masked")] for t, v, p in out]


def _js_plans(cases):
    cfg = _preview_cfg()
    expr = (
        f"{json.dumps(cases)}.map(([body, values]) => M.withApeExtras("
        f"{json.dumps(cfg['ape_face'])}, {json.dumps(cfg['trait_order'])}, "
        f"B.orderedLayers({json.dumps(cfg['trait_order'])}, values, "
        f"{json.dumps(cfg['z_order'])}), body, values.Body))"
    )
    return run_js(expr)


def test_ape_extras_match_the_server_compose_for_every_face_combo(monkeypatch):
    base = {
        "Background": "Sunset",
        "Back": "Cape",
        "Clothing": "Jacket",
    }
    cases = []
    for body_value, eyes, mouth, brows, head, acc in itertools.product(
        APE_BODIES,
        FACE_VALUES["Eyes"],
        FACE_VALUES["Mouth"],
        FACE_VALUES["Eyebrows"],
        FACE_VALUES["Head"],
        FACE_VALUES["Accessory"],
    ):
        values = {
            **base,
            "Body": body_value,
            "Eyes": eyes,
            "Mouth": mouth,
            "Eyebrows": brows,
            "Head": head,
            "Accessory": acc,
        }
        cases.append(["ape", values])
    plans = _js_plans(cases)
    assert len(plans) == len(cases)
    for (body, values), plan in zip(cases, plans, strict=True):
        got = [[x["slot"], x["value"], x["masked"]] for x in plan["layers"]]
        assert got == _server_plan(values, body, monkeypatch), values
        assert plan["mask"] == (values["Body"] in ape_face.MASKED_BODY_VALUES)
        (nose,) = [x for x in plan["layers"] if x["slot"] == "Nose"]
        assert nose["asset"] == "nose"


def test_non_ape_bodies_get_no_extras_and_match_the_server(monkeypatch):
    values = {
        "Background": "Sunset",
        "Body": "Straight Tan",
        "Eyes": "Wavy",
        "Mouth": "Smile",
        "Accessory": "Retardio",
    }
    cases = [[body, values] for body in BODIES if body != "ape"]
    for (body, _), plan in zip(cases, _js_plans(cases), strict=True):
        assert plan["mask"] is False
        got = [[x["slot"], x["value"], x["masked"]] for x in plan["layers"]]
        assert got == _server_plan(values, body, monkeypatch)
        assert all(x["slot"] != "Nose" for x in plan["layers"])


def test_nose_sits_above_plain_eyes_and_below_full_face_eyes():
    cfg = _preview_cfg()
    for eyes, want in (("Blue", ["Eyes", "Nose"]), ("Sticky Face", ["Nose", "Eyes"])):
        plan = run_js(
            f"M.withApeExtras({json.dumps(cfg['ape_face'])}, {json.dumps(cfg['trait_order'])}, "
            f"[{{slot: 'Body', value: 'Ape Gold'}}, {{slot: 'Eyes', value: {json.dumps(eyes)}}}], "
            "'ape', 'Ape Gold')"
        )
        assert [x["slot"] for x in plan["layers"]] == ["Body", *want]


def test_without_ape_rules_nothing_is_injected():
    # A client newer than the API it talks to: no ape_face block -> the plan
    # passes through unchanged rather than guessing at the rules.
    plan = run_js(
        "M.withApeExtras(null, [], [{slot: 'Body', value: 'Ape Gold'}], 'ape', 'Ape Gold')"
    )
    assert plan == {
        "layers": [{"slot": "Body", "value": "Ape Gold", "masked": False}],
        "mask": False,
    }


# ---------------------------------------------------------------------------
# rolledFaceSlots <- traits.fill_missing_face_traits
# ---------------------------------------------------------------------------


def test_rolled_face_slots_are_the_empty_ape_face_slots_in_server_order():
    face = json.dumps(traits.FACE_TRAITS)
    got = run_js(
        f"M.rolledFaceSlots('ape', {{Eyes: 'None', Mouth: '', Eyebrows: 'Thick'}}, {face})"
    )
    assert got == [t for t in traits.FACE_TRAITS if t in ("Eyes", "Mouth")]
    assert run_js(f"M.rolledFaceSlots('ape', {{}}, {face})") == traits.FACE_TRAITS


def test_only_apes_get_face_rolls():
    face = json.dumps(traits.FACE_TRAITS)
    assert run_js(f"M.rolledFaceSlots('male', {{Eyes: 'None'}}, {face})") == []
    assert run_js("M.rolledFaceSlots('ape', {Eyes: 'None'}, null)") == []


# ---------------------------------------------------------------------------
# previewStatus: the note under the preview + whether Swap is blocked
# ---------------------------------------------------------------------------


def _status(**kw):
    args = {"picked": 1, "missing": [], "unchecked": 0, "rolls": [], "blanks": []}
    args.update(kw)
    return run_js(f"M.previewStatus({json.dumps(args)})")


def test_missing_art_blocks_the_swap_and_names_it():
    out = _status(
        missing=[
            {"who": "#12", "what": 'Head "Crown"'},
            {"who": "#12", "what": 'Head "Crown"'},  # deduped
            {"who": "#40", "what": "the ape nose"},
        ]
    )
    assert out["blocked"] is True
    assert out["tone"] == "error"
    assert out["text"].count('Head "Crown" on #12') == 1
    assert "the ape nose on #40" in out["text"]


def test_a_blank_side_blocks_the_swap():
    out = _status(blanks=["#7"])
    assert out["blocked"] is True
    assert "#7" in out["text"] and "blank" in out["text"].lower()


def test_unloaded_art_warns_but_never_blocks():
    out = _status(unchecked=2)
    assert out["blocked"] is False
    assert out["tone"] == "warn"
    assert "incomplete" in out["text"]


def test_face_rolls_are_announced():
    out = _status(rolls=[{"who": "#9", "slots": ["Mouth", "Eyes"]}])
    assert out["blocked"] is False
    assert "#9" in out["text"] and "Mouth" in out["text"] and "Eyes" in out["text"]
    assert "random" in out["text"]


def test_nothing_picked_invites_a_pick():
    out = _status(picked=0)
    assert out["blocked"] is False
    assert "Tick" in out["text"]


def test_clean_preview_has_no_note():
    assert _status() == {"blocked": False, "tone": "info", "text": ""}


# ---------------------------------------------------------------------------
# wiring: the served config, app.js import, index.html panel + cache busters
# ---------------------------------------------------------------------------


def test_preview_config_is_the_compose_rules():
    cfg = _preview_cfg()
    assert cfg["trait_order"] == swap_meta.TRAIT_ORDER
    assert cfg["z_order"] == trait_config.get_config().z_table()
    assert cfg["face_traits"] == traits.FACE_TRAITS
    af = cfg["ape_face"]
    assert af["top_traits"] == ape_face.TOP_TRAITS
    assert af["no_mask"] == ape_face.NO_MASK_VALUES
    assert set(af["nose_below_eyes"]) == ape_face.NOSE_BELOW_EYES_VALUES
    assert set(af["masked_bodies"]) == ape_face.MASKED_BODY_VALUES
    assert set(af["masked_traits"]) == ape_face.MASKED_TRAITS
    json.dumps(cfg)  # JSON-safe (no sets)


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as fh:
        return fh.read()


def test_app_js_imports_swap_pure_with_a_cache_buster():
    assert re.search(r"from '\./swap_pure\.js\?v=\d+'", _read("webapp", "client", "app.js"))


def test_app_js_uses_the_pure_swap_matrix_only():
    # One swapAllowed, not two: app.js must not keep its own copy.
    assert "function swapAllowed" not in _read("webapp", "client", "app.js")


def test_index_html_has_the_preview_and_serves_the_bumped_app_js():
    html = _read("webapp", "client", "index.html")
    for el_id in ("swap-preview", "swap-after1", "swap-after2", "swap-preview-note"):
        assert f'id="{el_id}"' in html
    m = re.search(r'src="app\.js\?v=(\d+)"', html)
    assert m and int(m.group(1)) >= 113, "app.js?v= must move past the pre-preview 112"
