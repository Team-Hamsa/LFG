# Trait Rules Engine + Body-Affinity System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the implicit per-body trait compatibility matrix explicit, validated, and enforced across mint, swap, and economy paths — closing #28 and #40, shipping #30's cross-body swapping, and satisfying #39's launch slice via an audit-report review instead of a UI.

**Architecture:** A one-time audit script derives per-value body affinity from the 3,535-edition mainnet mint history; a new `lfg_core/trait_config.py` engine loads a single `trait_config.yaml` (layer z-order, z-overrides, affinity, swap matrix, empty exclusion/inclusion machinery) and is consumed by mint selection, swap compose, the swap API, and economy ops. A final isolated PR physically moves audit-confirmed universal values to `layers/shared/`.

**Tech Stack:** Python 3.10+, PyYAML (new dep), sqlite3, aiohttp (existing service), pytest.

**Spec:** `docs/superpowers/specs/2026-07-04-trait-rules-body-affinity-design.md`

## Global Constraints

- Every new test file that imports `lfg_core` at module top MUST start with the env-guard preamble — copy it **verbatim** from `tests/test_seasons.py` lines 1–18 (`XUMM_API_KEY` … `BUNNY_PULL_ZONE`).
- All PRs open as **draft** (`gh pr create --draft`); CodeRabbit review before merge; ≤4 ready-for-review flips per hour.
- No XRPL transactions are built in this plan, so no SourceTag work — but if any task adds a tx path, `SourceTag = 2606160021` is mandatory.
- Run `.venv/bin/python -m pytest` (repo venv), not system python. Pre-commit runs ruff format.
- The ape-face compose rule (`lfg_core/ape_face.py`) stays code — do NOT try to make it declarative.
- Body names are exactly: `ape`, `female`, `male`, `skeleton`. Trait types are exactly the nine in `swap_meta.TRAIT_ORDER`.
- **PAUSE after PR-1 is merged and the report is generated: the user must review/correct `reports/body_affinity_report.md` before PR-2's config is committed. This is the only human gate.**

## File Structure

```
lfg_core/affinity_audit.py        # NEW  PR-1: pure derivation logic (testable, no I/O)
scripts/audit_body_affinity.py    # NEW  PR-1: CLI — reads onchain DB + layers/, writes reports/
lfg_core/trait_config.py          # NEW  PR-2: config load/validate/query engine
trait_config.yaml                 # NEW  PR-2: the committed, user-confirmed config
scripts/validate_trait_config.py  # NEW  PR-2: CI/pre-commit validation CLI
lfg_core/traits.py                # MOD  PR-3: affinity filtering in select_random_attributes
lfg_core/swap_compose.py          # MOD  PR-3: z-order from config; PR-4: cross-body resolution
lfg_service/app.py                # MOD  PR-4: handle_swap_start matrix enforcement; handle_nfts matrix payload
webapp/static/…(swap JS)          # MOD  PR-4: filter trait choices per selected pair
webapp/economy_api.py             # MOD  PR-4: affinity gate on equip/assemble/deposit
lfg_core/layer_store.py           # MOD  PR-5: shared-dir union lookup
scripts/migrate_shared_layers.py  # NEW  PR-5: verify-identical + move migration
lfg_core/seasons.py               # MOD  PR-5: get_season shared-key fallback
```

---

# PR-1 — Body-affinity audit (`feat/body-affinity-audit`)

### Task 1: Affinity counting + classification (`lfg_core/affinity_audit.py`)

**Files:**
- Create: `lfg_core/affinity_audit.py`
- Test: `tests/test_affinity_audit.py`

**Interfaces:**
- Produces: `count_affinities(rows) -> dict[tuple[str, str], Counter]` where rows are `(body, attributes_json)` tuples and keys are `(trait_type, value)`.
- Produces: `classify(counts: Counter) -> str` — one of `female-only`, `male-only`, `shared-MF`, `universal`, or `bodies:<a>+<b>…` for other subsets.
- Produces: `LOW_CONFIDENCE_THRESHOLD = 3`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_affinity_audit.py
# <env-guard preamble verbatim from tests/test_seasons.py lines 1-18>

import json  # noqa: E402
from collections import Counter  # noqa: E402

from lfg_core import affinity_audit  # noqa: E402


def _attrs(**kw):
    return json.dumps([{"trait_type": k, "value": v} for k, v in kw.items()])


def test_count_affinities_groups_by_type_value_and_body():
    rows = [
        ("female", _attrs(Body="Curved", Clothing="Summer Dress")),
        ("female", _attrs(Body="Curved 2", Clothing="Summer Dress")),
        ("male", _attrs(Body="Straight", Clothing="Hoodie")),
        ("female", _attrs(Body="Curved", Clothing="Hoodie")),
    ]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Clothing", "Summer Dress")] == Counter({"female": 2})
    assert counts[("Clothing", "Hoodie")] == Counter({"male": 1, "female": 1})


def test_count_affinities_derives_body_when_column_empty():
    rows = [(None, _attrs(Body="Ape Strong", Eyes="Hypno"))]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Eyes", "Hypno")] == Counter({"ape": 1})


def test_classify_labels():
    assert affinity_audit.classify(Counter({"female": 5})) == "female-only"
    assert affinity_audit.classify(Counter({"male": 2})) == "male-only"
    assert affinity_audit.classify(Counter({"male": 2, "female": 9})) == "shared-MF"
    assert (
        affinity_audit.classify(Counter({"male": 1, "female": 1, "ape": 1, "skeleton": 1}))
        == "universal"
    )
    assert affinity_audit.classify(Counter({"ape": 3, "skeleton": 1})) == "bodies:ape+skeleton"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lfg_core.affinity_audit'`

- [ ] **Step 3: Write minimal implementation**

```python
# lfg_core/affinity_audit.py
# Derive per-(trait_type, value) body affinity from historical mint data.
# Pure logic — no file or DB I/O — so scripts/audit_body_affinity.py stays a
# thin CLI and the derivation is unit-testable.

import json
from collections import Counter

from lfg_core.swap_meta import detect_body

LOW_CONFIDENCE_THRESHOLD = 3

BODIES = ["ape", "female", "male", "skeleton"]


def count_affinities(
    rows: list[tuple[str | None, str]],
) -> dict[tuple[str, str], Counter]:
    """rows: (body, attributes_json) per historical token (burned included).
    Body falls back to detect_body(attributes) when the column is empty."""
    counts: dict[tuple[str, str], Counter] = {}
    for body, attributes_json in rows:
        try:
            attributes = json.loads(attributes_json) if attributes_json else []
        except (TypeError, ValueError):
            attributes = []
        if not attributes:
            continue
        body = body or detect_body(attributes)
        for attr in attributes:
            trait_type, value = attr.get("trait_type"), attr.get("value")
            if not trait_type or not value or trait_type == "Body":
                continue
            counts.setdefault((trait_type, value), Counter())[body] += 1
    return counts


def classify(counts: Counter) -> str:
    bodies = {b for b, n in counts.items() if n > 0}
    if bodies == {"female"}:
        return "female-only"
    if bodies == {"male"}:
        return "male-only"
    if bodies == {"male", "female"}:
        return "shared-MF"
    if bodies == set(BODIES):
        return "universal"
    return "bodies:" + "+".join(sorted(bodies))
```

Note: `Body` attributes are skipped — Body IS the body shape; affinity is meaningless for it.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_affinity_audit.py lfg_core/affinity_audit.py
git commit -m "feat(affinity): count + classify per-value body affinity from mint history"
```

### Task 2: Dir cross-checks (misplacements + coverage gaps)

**Files:**
- Modify: `lfg_core/affinity_audit.py`
- Test: `tests/test_affinity_audit.py`

**Interfaces:**
- Produces: `cross_check(counts, dir_tree) -> tuple[list, list]` where `dir_tree: dict[str, dict[str, set[str]]]` is `{body: {trait_type: {values}}}`. Returns `(misplacements, coverage_gaps)`; each entry is `(body, trait_type, value)`.

- [ ] **Step 1: Write the failing test**

```python
def test_cross_check_flags_never_minted_dir_values_and_missing_files():
    counts = {
        ("Clothing", "Summer Dress"): Counter({"female": 4}),
        ("Clothing", "Retired Coat"): Counter({"male": 2}),
    }
    dir_tree = {
        "female": {"Clothing": {"Summer Dress"}},
        "male": {"Clothing": {"Summer Dress"}},  # present but never minted on male
        "ape": {"Clothing": set()},
        "skeleton": {"Clothing": set()},
    }
    misplacements, gaps = affinity_audit.cross_check(counts, dir_tree)
    assert ("male", "Clothing", "Summer Dress") in misplacements
    assert ("male", "Clothing", "Retired Coat") in gaps  # minted on male, no file
    assert ("female", "Clothing", "Summer Dress") not in misplacements
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py::test_cross_check_flags_never_minted_dir_values_and_missing_files -v`
Expected: FAIL with `AttributeError: … has no attribute 'cross_check'`

- [ ] **Step 3: Write minimal implementation** (append to `lfg_core/affinity_audit.py`)

```python
def cross_check(
    counts: dict[tuple[str, str], Counter],
    dir_tree: dict[str, dict[str, set[str]]],
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """misplacements: value present in a body dir but never minted on that
    body (candidate misplacement OR intentionally-new — human decides).
    coverage_gaps: value minted on a body historically but absent from its
    dir today."""
    misplacements = []
    for body, types in dir_tree.items():
        for trait_type, values in types.items():
            for value in values:
                if value == "None":
                    continue
                if counts.get((trait_type, value), Counter()).get(body, 0) == 0:
                    misplacements.append((body, trait_type, value))
    coverage_gaps = []
    for (trait_type, value), body_counts in counts.items():
        for body, n in body_counts.items():
            if n > 0 and value not in dir_tree.get(body, {}).get(trait_type, set()):
                coverage_gaps.append((body, trait_type, value))
    return sorted(misplacements), sorted(coverage_gaps)
```

`None` values are exempt from misplacement flags: `None.png` placeholders (e.g. all skeleton facial dirs) are structural, not errors.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_affinity_audit.py lfg_core/affinity_audit.py
git commit -m "feat(affinity): cross-check mint history against layer dirs"
```

### Task 3: Report + draft-YAML emitters

**Files:**
- Modify: `lfg_core/affinity_audit.py`
- Test: `tests/test_affinity_audit.py`

**Interfaces:**
- Produces: `render_report_md(counts, misplacements, gaps) -> str` and `render_affinity_yaml(counts) -> str` (draft `affinity:` section, values grouped by trait type, alphabetical, low-confidence entries commented with their counts).

- [ ] **Step 1: Write the failing test**

```python
def test_render_affinity_yaml_lists_bodies_and_flags_low_confidence():
    counts = {
        ("Clothing", "Summer Dress"): Counter({"female": 4}),
        ("Eyes", "Rare Glint"): Counter({"male": 1}),  # < LOW_CONFIDENCE_THRESHOLD
    }
    out = affinity_audit.render_affinity_yaml(counts)
    assert '"Summer Dress": [female]' in out
    assert "LOW CONFIDENCE" in out and "Rare Glint" in out


def test_render_report_md_sections():
    counts = {("Clothing", "Summer Dress"): Counter({"female": 4})}
    out = affinity_audit.render_report_md(counts, [("male", "Clothing", "X")], [])
    assert "## Candidate misplacements" in out
    assert "male/Clothing/X" in out
    assert "female-only" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py -v -k render`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation** (append)

```python
def render_affinity_yaml(counts: dict[tuple[str, str], Counter]) -> str:
    by_type: dict[str, list[str]] = {}
    for (trait_type, value), body_counts in sorted(counts.items()):
        bodies = sorted(b for b, n in body_counts.items() if n > 0)
        total = sum(body_counts.values())
        line = f'    "{value}": [{", ".join(bodies)}]'
        if total < LOW_CONFIDENCE_THRESHOLD:
            line += f"  # LOW CONFIDENCE: only {total} mint(s) — verify by eye"
        by_type.setdefault(trait_type, []).append(line)
    out = ["affinity:"]
    for trait_type in sorted(by_type):
        out.append(f"  {trait_type}:")
        out.extend(by_type[trait_type])
    return "\n".join(out) + "\n"


def render_report_md(
    counts: dict[tuple[str, str], Counter],
    misplacements: list[tuple[str, str, str]],
    coverage_gaps: list[tuple[str, str, str]],
) -> str:
    lines = ["# Body-affinity audit report", ""]
    lines.append("## Per-value affinity (from mint history, burned included)")
    lines.append("")
    lines.append("| Trait type | Value | Classification | Counts |")
    lines.append("|---|---|---|---|")
    for (trait_type, value), body_counts in sorted(counts.items()):
        label = classify(body_counts)
        detail = ", ".join(f"{b}:{n}" for b, n in sorted(body_counts.items()) if n)
        flag = " ⚠️" if sum(body_counts.values()) < LOW_CONFIDENCE_THRESHOLD else ""
        lines.append(f"| {trait_type} | {value} | {label}{flag} | {detail} |")
    lines += ["", "## Candidate misplacements (in dir, never minted there)", ""]
    lines += [f"- {b}/{t}/{v}" for b, t, v in misplacements] or ["- none"]
    lines += ["", "## Coverage gaps (minted historically, missing from dir)", ""]
    lines += [f"- {b}/{t}/{v}" for b, t, v in coverage_gaps] or ["- none"]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_affinity_audit.py lfg_core/affinity_audit.py
git commit -m "feat(affinity): markdown report + draft affinity-YAML emitters"
```

### Task 4: CLI (`scripts/audit_body_affinity.py`)

**Files:**
- Create: `scripts/audit_body_affinity.py`
- Test: `tests/test_affinity_audit.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: CLI `.venv/bin/python scripts/audit_body_affinity.py --network mainnet` writing `reports/body_affinity_report.md`, `reports/body_affinity.json`, `reports/body_affinity_draft.yaml`. Exposes `run(db_path, layers_dir, out_dir) -> dict` for tests.

- [ ] **Step 1: Write the failing test**

```python
def test_run_end_to_end(tmp_path):
    import sqlite3

    db = tmp_path / "onchain.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, body TEXT,"
        " attributes_json TEXT, is_burned INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO onchain_nfts VALUES ('A', 'female', ?, 0)",
        (_attrs(Body="Curved", Clothing="Summer Dress"),),
    )
    conn.execute(  # burned tokens still count — history is the point
        "INSERT INTO onchain_nfts VALUES ('B', 'male', ?, 1)",
        (_attrs(Body="Straight", Clothing="Hoodie"),),
    )
    conn.commit()
    conn.close()
    layers = tmp_path / "layers"
    (layers / "female" / "Clothing").mkdir(parents=True)
    (layers / "female" / "Clothing" / "Summer Dress.png").write_bytes(b"x")
    (layers / "male" / "Clothing").mkdir(parents=True)

    from scripts.audit_body_affinity import run

    result = run(str(db), str(layers), str(tmp_path / "reports"))
    assert (tmp_path / "reports" / "body_affinity_report.md").exists()
    assert (tmp_path / "reports" / "body_affinity_draft.yaml").exists()
    assert result["values"] == 2
    assert ("male", "Clothing", "Hoodie") in result["coverage_gaps"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py::test_run_end_to_end -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/audit_body_affinity.py
# One-time (re-runnable) audit: derive per-value body affinity from the
# on-chain index (burned included) and cross-check against layers/.
# Output feeds the human review gate before trait_config.yaml is committed.
#
#   .venv/bin/python scripts/audit_body_affinity.py --network mainnet

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import affinity_audit  # noqa: E402


def _dir_tree(layers_dir: str) -> dict[str, dict[str, set[str]]]:
    tree: dict[str, dict[str, set[str]]] = {}
    for body in sorted(os.listdir(layers_dir)):
        body_path = os.path.join(layers_dir, body)
        if not os.path.isdir(body_path) or body.startswith("."):
            continue
        tree[body] = {}
        for trait_type in sorted(os.listdir(body_path)):
            type_path = os.path.join(body_path, trait_type)
            if not os.path.isdir(type_path) or trait_type.startswith("."):
                continue
            tree[body][trait_type] = {
                os.path.splitext(f)[0]
                for f in os.listdir(type_path)
                if not f.startswith(".")
                and os.path.splitext(f)[1].lower() in (".png", ".gif", ".mp4")
            }
    return tree


def run(db_path: str, layers_dir: str, out_dir: str) -> dict:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT body, attributes_json FROM onchain_nfts").fetchall()
    conn.close()
    counts = affinity_audit.count_affinities(rows)
    misplacements, gaps = affinity_audit.cross_check(counts, _dir_tree(layers_dir))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "body_affinity_report.md"), "w") as f:
        f.write(affinity_audit.render_report_md(counts, misplacements, gaps))
    with open(os.path.join(out_dir, "body_affinity_draft.yaml"), "w") as f:
        f.write(affinity_audit.render_affinity_yaml(counts))
    with open(os.path.join(out_dir, "body_affinity.json"), "w") as f:
        json.dump(
            {
                "counts": {
                    f"{t}/{v}": dict(c) for (t, v), c in sorted(counts.items())
                },
                "misplacements": misplacements,
                "coverage_gaps": gaps,
            },
            f,
            indent=2,
        )
    return {
        "values": len(counts),
        "misplacements": misplacements,
        "coverage_gaps": gaps,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network", choices=["testnet", "mainnet"], default="mainnet")
    p.add_argument("--layers-dir", default="layers")
    p.add_argument("--out-dir", default="reports")
    args = p.parse_args()
    result = run(f"onchain_{args.network}.db", args.layers_dir, args.out_dir)
    print(
        f"{result['values']} values audited; "
        f"{len(result['misplacements'])} candidate misplacements; "
        f"{len(result['coverage_gaps'])} coverage gaps -> {args.out_dir}/"
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_affinity_audit.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the real audit + commit**

```bash
.venv/bin/python scripts/audit_body_affinity.py --network mainnet
git add scripts/audit_body_affinity.py tests/test_affinity_audit.py reports/body_affinity_report.md reports/body_affinity_draft.yaml reports/body_affinity.json
git commit -m "feat(affinity): body-affinity audit CLI + mainnet report (#28)"
```

- [ ] **Step 6: Open draft PR-1**

```bash
git push -u origin feat/body-affinity-audit
gh pr create --draft --title "feat: body-affinity audit — derive historical trait/body matrix (#28)" --body "Phase 1 of the trait-rules plan (spec: docs/superpowers/specs/2026-07-04-trait-rules-body-affinity-design.md). Derives per-value body affinity from all 3,535 editions (burned included), cross-checks layers/, emits report + draft config.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

**⛔ HUMAN GATE: after merge, the user reviews `reports/body_affinity_report.md` — especially LOW CONFIDENCE rows and candidate misplacements — and corrects `reports/body_affinity_draft.yaml`. Do not start Task 9 (committing `trait_config.yaml`) until the user has confirmed the draft.**

---

# PR-2 — Rules engine core (`feat/trait-config-engine`)

### Task 5: Config schema + structural loader (`lfg_core/trait_config.py`)

**Files:**
- Modify: `requirements.txt` (add `PyYAML`)
- Create: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

**Interfaces:**
- Produces: `load_config(path: str) -> TraitConfig` (raises `TraitConfigError` on structural problems), dataclasses `LayerSpec(name, z, shared)`, `ZOverride(trait_type, value, z)`, `SwapPair(bodies, layers, layers_except)`, `TraitConfig(layers, z_overrides, affinity, universal_layers, swap_pairs, exclusions, inclusions)`.
- Produces: `get_config(path=None) -> TraitConfig` process-wide singleton; `reset_config()` for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trait_config.py
# <env-guard preamble verbatim from tests/test_seasons.py lines 1-18>

import pytest  # noqa: E402

from lfg_core import trait_config  # noqa: E402

GOOD = """
version: 1
layers:
  - {name: Background, z: 10, shared: true}
  - {name: Back, z: 20, shared: true}
  - {name: Body, z: 30}
  - {name: Clothing, z: 40}
  - {name: Mouth, z: 50}
  - {name: Eyebrows, z: 60}
  - {name: Eyes, z: 70}
  - {name: Head, z: 80}
  - {name: Accessory, z: 90}
z_overrides:
  - {trait_type: Eyes, value: Wavy, z: 95}
affinity:
  Clothing:
    "Summer Dress": [female]
swap_matrix:
  universal_layers: [Accessory, Back]
  pairs:
    - {bodies: [ape, skeleton], layers: [Head, Clothing]}
    - {bodies: [male, female], layers_except: [Clothing]}
exclusions: []
inclusions: []
"""


def _write(tmp_path, text):
    p = tmp_path / "trait_config.yaml"
    p.write_text(text)
    return str(p)


def test_load_config_parses_all_sections(tmp_path):
    cfg = trait_config.load_config(_write(tmp_path, GOOD))
    assert [layer.name for layer in cfg.layers][:3] == ["Background", "Back", "Body"]
    assert cfg.layers[0].shared is True
    assert cfg.z_overrides[0].z == 95
    assert cfg.affinity["Clothing"]["Summer Dress"] == ["female"]
    assert "Accessory" in cfg.universal_layers


def test_load_config_rejects_duplicate_layers(tmp_path):
    bad = GOOD.replace("{name: Body, z: 30}", "{name: Background, z: 30}")
    with pytest.raises(trait_config.TraitConfigError, match="duplicate layer"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_unknown_body_in_affinity(tmp_path):
    bad = GOOD.replace("[female]", "[mermaid]")
    with pytest.raises(trait_config.TraitConfigError, match="unknown body"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_pair_with_both_layer_forms(tmp_path):
    bad = GOOD.replace(
        "layers_except: [Clothing]}", "layers_except: [Clothing], layers: [Eyes]}"
    )
    with pytest.raises(trait_config.TraitConfigError, match="layers or layers_except"):
        trait_config.load_config(_write(tmp_path, bad))


def test_get_config_singleton(tmp_path):
    trait_config.reset_config()
    path = _write(tmp_path, GOOD)
    assert trait_config.get_config(path) is trait_config.get_config()
    trait_config.reset_config()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```bash
echo 'PyYAML' >> requirements.txt && .venv/bin/pip install PyYAML
```

```python
# lfg_core/trait_config.py
# Declarative trait rules: layer z-order, per-value z-overrides, per-value
# body affinity, cross-body swap matrix, and (empty-at-launch) exclusion /
# inclusion machinery. Single source: trait_config.yaml at the repo root.
# The layer *stores* stay the authority on which files exist; this config is
# the authority on which combinations are legal.

import os
from dataclasses import dataclass, field

import yaml

VALID_BODIES = frozenset({"ape", "female", "male", "skeleton"})

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trait_config.yaml"
)


class TraitConfigError(Exception):
    pass


@dataclass(frozen=True)
class LayerSpec:
    name: str
    z: float
    shared: bool = False


@dataclass(frozen=True)
class ZOverride:
    trait_type: str
    value: str
    z: float


@dataclass(frozen=True)
class SwapPair:
    bodies: frozenset[str]
    layers: frozenset[str] | None = None
    layers_except: frozenset[str] | None = None


@dataclass(frozen=True)
class TraitConfig:
    layers: tuple[LayerSpec, ...]
    z_overrides: tuple[ZOverride, ...]
    affinity: dict[str, dict[str, list[str]]]
    universal_layers: frozenset[str]
    swap_pairs: tuple[SwapPair, ...]
    exclusions: tuple = ()
    inclusions: tuple = ()


def _check_bodies(bodies, where: str) -> None:
    unknown = set(bodies) - VALID_BODIES
    if unknown:
        raise TraitConfigError(f"unknown body {sorted(unknown)} in {where}")


def load_config(path: str) -> TraitConfig:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if raw.get("version") != 1:
        raise TraitConfigError("trait_config version must be 1")

    layers = tuple(
        LayerSpec(entry["name"], float(entry["z"]), bool(entry.get("shared", False)))
        for entry in raw.get("layers", [])
    )
    names = [layer.name for layer in layers]
    if len(names) != len(set(names)):
        raise TraitConfigError("duplicate layer name in layers")
    if not layers:
        raise TraitConfigError("layers section is required")

    z_overrides = tuple(
        ZOverride(o["trait_type"], o["value"], float(o["z"]))
        for o in raw.get("z_overrides", [])
    )
    for o in z_overrides:
        if o.trait_type not in names:
            raise TraitConfigError(f"z_override for unknown layer {o.trait_type!r}")

    affinity: dict[str, dict[str, list[str]]] = raw.get("affinity", {}) or {}
    for trait_type, values in affinity.items():
        if trait_type not in names:
            raise TraitConfigError(f"affinity for unknown layer {trait_type!r}")
        for value, bodies in values.items():
            _check_bodies(bodies, f"affinity {trait_type}/{value}")

    matrix = raw.get("swap_matrix", {}) or {}
    universal = frozenset(matrix.get("universal_layers", []))
    if not universal <= set(names):
        raise TraitConfigError("universal_layers contains unknown layer")
    pairs = []
    for p in matrix.get("pairs", []):
        _check_bodies(p.get("bodies", []), "swap_matrix pair")
        if ("layers" in p) == ("layers_except" in p):
            raise TraitConfigError("swap pair needs exactly one of layers or layers_except")
        pairs.append(
            SwapPair(
                bodies=frozenset(p["bodies"]),
                layers=frozenset(p["layers"]) if "layers" in p else None,
                layers_except=(
                    frozenset(p["layers_except"]) if "layers_except" in p else None
                ),
            )
        )

    return TraitConfig(
        layers=layers,
        z_overrides=z_overrides,
        affinity=affinity,
        universal_layers=universal,
        swap_pairs=tuple(pairs),
        exclusions=tuple(raw.get("exclusions", []) or ()),
        inclusions=tuple(raw.get("inclusions", []) or ()),
    )


_config: TraitConfig | None = None


def get_config(path: str | None = None) -> TraitConfig:
    global _config
    if _config is None:
        _config = load_config(path or DEFAULT_CONFIG_PATH)
    return _config


def reset_config() -> None:
    global _config
    _config = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add requirements.txt lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(rules): trait_config schema + structural loader (#40)"
```

### Task 6: Query API (order, z, affinity, swap matrix)

**Files:**
- Modify: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

**Interfaces:**
- Produces (methods on `TraitConfig`):
  - `layer_order() -> list[str]` — names sorted by z ascending.
  - `z_for(trait_type: str, value: str) -> float` — override if present, else the layer z.
  - `sort_attributes(attrs: list[dict]) -> list[dict]` — metadata-style attrs sorted by `z_for` (stable). This is what replaces the TOP_TRAITS reorder in compose.
  - `allowed_bodies(trait_type: str, value: str) -> frozenset[str] | None` — `None` = no entry, dirs decide.
  - `value_allowed(body: str, trait_type: str, value: str) -> bool` — `True` when no entry.
  - `swap_allowed(body_a: str, body_b: str, layer: str) -> bool` — same body always True; universal layers always True; else any pair whose bodies ⊇ {a,b} and whose layers include (or layers_except exclude) the layer.
  - `conflicts(selected: list[dict], trait_type: str, value: str) -> bool` — pairwise exclusion machinery (authored directionally, enforced symmetrically). Exclusion entry shape: `{trait_type, value, excludes: [{trait_type, values: [...] | "*"}]}`. Empty at launch, so this returns False everywhere on the shipped config.

- [ ] **Step 1: Write the failing test**

```python
def _cfg(tmp_path):
    return trait_config.load_config(_write(tmp_path, GOOD))


def test_layer_order_sorted_by_z(tmp_path):
    assert _cfg(tmp_path).layer_order() == [
        "Background", "Back", "Body", "Clothing", "Mouth",
        "Eyebrows", "Eyes", "Head", "Accessory",
    ]


def test_z_for_override_beats_layer_z(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.z_for("Eyes", "Wavy") == 95
    assert cfg.z_for("Eyes", "Hypno") == 70


def test_sort_attributes_moves_override_on_top(tmp_path):
    cfg = _cfg(tmp_path)
    attrs = [
        {"trait_type": "Eyes", "value": "Wavy"},
        {"trait_type": "Body", "value": "Straight"},
        {"trait_type": "Background", "value": "Sunset"},
    ]
    assert [a["value"] for a in cfg.sort_attributes(attrs)] == [
        "Sunset", "Straight", "Wavy",
    ]


def test_affinity_queries(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.allowed_bodies("Clothing", "Summer Dress") == frozenset({"female"})
    assert cfg.allowed_bodies("Clothing", "Hoodie") is None
    assert cfg.value_allowed("female", "Clothing", "Summer Dress")
    assert not cfg.value_allowed("male", "Clothing", "Summer Dress")
    assert cfg.value_allowed("male", "Clothing", "Hoodie")  # no entry -> dirs decide


def test_swap_allowed_matrix(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.swap_allowed("male", "male", "Clothing")          # same body
    assert cfg.swap_allowed("ape", "female", "Accessory")        # universal layer
    assert cfg.swap_allowed("ape", "skeleton", "Head")           # pair layers
    assert not cfg.swap_allowed("ape", "skeleton", "Eyes")       # not in pair layers
    assert cfg.swap_allowed("male", "female", "Eyes")            # layers_except
    assert not cfg.swap_allowed("male", "female", "Clothing")    # excepted
    assert not cfg.swap_allowed("ape", "male", "Head")           # no pair


EXCL = GOOD.replace(
    "exclusions: []",
    """exclusions:
  - trait_type: Eyes
    value: Laser
    excludes:
      - {trait_type: Head, values: [Crown]}
""",
)


def test_conflicts_enforced_symmetrically(tmp_path):
    cfg = trait_config.load_config(_write(tmp_path, EXCL))
    laser = [{"trait_type": "Eyes", "value": "Laser"}]
    crown = [{"trait_type": "Head", "value": "Crown"}]
    assert cfg.conflicts(laser, "Head", "Crown")        # authored direction
    assert cfg.conflicts(crown, "Eyes", "Laser")        # symmetric direction
    assert not cfg.conflicts(laser, "Head", "Beanie Black")
    assert not cfg.conflicts([], "Head", "Crown")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v -k "order or z_for or sort or affinity_q or swap_allowed"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation** — add methods to `TraitConfig`:

```python
    def layer_order(self) -> list[str]:
        return [layer.name for layer in sorted(self.layers, key=lambda s: s.z)]

    def z_for(self, trait_type: str, value: str) -> float:
        for o in self.z_overrides:
            if o.trait_type == trait_type and o.value == value:
                return o.z
        for layer in self.layers:
            if layer.name == trait_type:
                return layer.z
        raise TraitConfigError(f"unknown layer {trait_type!r}")

    def sort_attributes(self, attrs: list[dict]) -> list[dict]:
        return sorted(attrs, key=lambda a: self.z_for(a["trait_type"], a["value"]))

    def allowed_bodies(self, trait_type: str, value: str) -> frozenset[str] | None:
        entry = self.affinity.get(trait_type, {}).get(value)
        return frozenset(entry) if entry is not None else None

    def value_allowed(self, body: str, trait_type: str, value: str) -> bool:
        allowed = self.allowed_bodies(trait_type, value)
        return allowed is None or body in allowed

    def swap_allowed(self, body_a: str, body_b: str, layer: str) -> bool:
        if body_a == body_b or layer in self.universal_layers:
            return True
        for pair in self.swap_pairs:
            if not {body_a, body_b} <= pair.bodies:
                continue
            if pair.layers is not None and layer in pair.layers:
                return True
            if pair.layers_except is not None and layer not in pair.layers_except:
                return True
        return False

    def conflicts(self, selected: list[dict], trait_type: str, value: str) -> bool:
        def _hits(rule: dict, t: str, v: str) -> bool:
            values = rule.get("values", "*")
            return rule["trait_type"] == t and (values == "*" or v in values)

        for entry in self.exclusions:
            src_t, src_v = entry["trait_type"], entry["value"]
            for rule in entry.get("excludes", []):
                for sel in selected:
                    # authored direction: candidate is the excluded side
                    if (
                        sel["trait_type"] == src_t
                        and sel["value"] == src_v
                        and _hits(rule, trait_type, value)
                    ):
                        return True
                    # symmetric direction: candidate is the authoring side
                    if (
                        trait_type == src_t
                        and value == src_v
                        and _hits(rule, sel["trait_type"], sel["value"])
                    ):
                        return True
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(rules): trait_config query API — order, z, affinity, swap matrix"
```

### Task 7: Store-consistency validation

**Files:**
- Modify: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

**Interfaces:**
- Consumes: layer store protocol (`list_bodies`, `list_trait_types`, `list_values`) from `lfg_core/layer_store.py`.
- Produces: `async validate_against_store(cfg, store) -> tuple[list[str], list[str]]` — `(errors, warnings)`. Errors: affinity value missing from every dir it claims. Warnings: dir value with no affinity entry (dir-derived default applies); a config layer with no directory under any body.

- [ ] **Step 1: Write the failing test**

```python
def test_validate_against_store(tmp_path):
    import asyncio

    from lfg_core.layer_store import LocalLayerStore

    layers = tmp_path / "layers"
    (layers / "female" / "Clothing").mkdir(parents=True)
    (layers / "female" / "Clothing" / "Summer Dress.png").write_bytes(b"x")
    (layers / "female" / "Background").mkdir()
    (layers / "female" / "Background" / "Sunset.png").write_bytes(b"x")
    (layers / "female" / "Body").mkdir()
    (layers / "female" / "Body" / "Curved.png").write_bytes(b"x")
    (layers / "female" / "Eyes").mkdir()
    (layers / "female" / "Eyes" / "Hypno.png").write_bytes(b"x")

    cfg = trait_config.load_config(
        _write(
            tmp_path,
            GOOD.replace(
                '"Summer Dress": [female]',
                '"Summer Dress": [female]\n    "Ghost Coat": [female]',
            ),
        )
    )
    store = LocalLayerStore(str(layers))
    errors, warnings = asyncio.run(trait_config.validate_against_store(cfg, store))
    assert any("Ghost Coat" in e for e in errors)            # claimed, no file
    assert any("Hypno" in w for w in warnings)               # file, no entry
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py::test_validate_against_store -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Write minimal implementation** (module-level, after the dataclasses)

```python
async def validate_against_store(cfg: TraitConfig, store) -> tuple[list[str], list[str]]:
    """Cross-check config claims against what the layer store actually has.
    Errors block; warnings are dir values falling back to dir-derived affinity."""
    errors: list[str] = []
    warnings: list[str] = []
    bodies = await store.list_bodies()
    tree: dict[str, dict[str, set[str]]] = {}
    for body in bodies:
        tree[body] = {}
        for trait_type in await store.list_trait_types(body):
            tree[body][trait_type] = set(await store.list_values(body, trait_type))

    layer_names = {layer.name for layer in cfg.layers}
    seen_types = {t for types in tree.values() for t in types}
    for name in sorted(layer_names - seen_types):
        # warning, not error: the dirs are the authority on what exists; a
        # config layer with no directory anywhere just never yields a trait
        warnings.append(f"config layer {name!r} has no directory under any body")

    for trait_type, values in cfg.affinity.items():
        for value, claimed in values.items():
            present = {b for b in claimed if value in tree.get(b, {}).get(trait_type, set())}
            if not present:
                errors.append(
                    f"affinity {trait_type}/{value} claims {sorted(claimed)} "
                    "but no such file exists in any claimed body dir"
                )
    for body, types in tree.items():
        for trait_type, values in types.items():
            if trait_type not in layer_names:
                continue
            for value in values:
                if value != "None" and cfg.allowed_bodies(trait_type, value) is None:
                    warnings.append(
                        f"{body}/{trait_type}/{value} has no affinity entry "
                        "(dir-derived default applies)"
                    )
    return errors, warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(rules): validate trait_config claims against the layer store"
```

### Task 8: Validation CLI + CI hook

**Files:**
- Create: `scripts/validate_trait_config.py`
- Modify: `.pre-commit-config.yaml`
- Test: `tests/test_trait_config.py`

**Interfaces:**
- Produces: CLI exiting 0 (valid), 1 (errors); warnings print but don't fail. `main(argv) -> int` for tests.

- [ ] **Step 1: Write the failing test**

```python
def test_validate_cli_exit_codes(tmp_path, capsys):
    from scripts.validate_trait_config import main

    layers = tmp_path / "layers"
    (layers / "female" / "Background").mkdir(parents=True)
    (layers / "female" / "Background" / "Sunset.png").write_bytes(b"x")
    (layers / "female" / "Body").mkdir()
    (layers / "female" / "Body" / "Curved.png").write_bytes(b"x")
    (layers / "female" / "Eyes").mkdir()
    (layers / "female" / "Eyes" / "Wavy.png").write_bytes(b"x")
    (layers / "female" / "Clothing").mkdir()
    (layers / "female" / "Clothing" / "Summer Dress.png").write_bytes(b"x")

    good = _write(tmp_path, GOOD)
    assert main(["--config", good, "--layers-dir", str(layers)]) == 0

    bad = tmp_path / "bad.yaml"
    bad.write_text(GOOD.replace("[female]", "[male]"))  # claims male, file is female-only
    assert main(["--config", str(bad), "--layers-dir", str(layers)]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py::test_validate_cli_exit_codes -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/validate_trait_config.py
# CI / pre-commit gate: structural + store-consistency validation of
# trait_config.yaml. Exit 1 on errors; warnings are informational.

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import trait_config  # noqa: E402
from lfg_core.layer_store import LocalLayerStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=trait_config.DEFAULT_CONFIG_PATH)
    p.add_argument("--layers-dir", default="layers")
    args = p.parse_args(argv)
    try:
        cfg = trait_config.load_config(args.config)
    except trait_config.TraitConfigError as e:
        print(f"ERROR: {e}")
        return 1
    errors, warnings = asyncio.run(
        trait_config.validate_against_store(cfg, LocalLayerStore(args.layers_dir))
    )
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Add to `.pre-commit-config.yaml` (local hooks section, alongside the existing pytest hook — match its structure exactly):

```yaml
      - id: validate-trait-config
        name: validate trait_config.yaml
        entry: .venv/bin/python scripts/validate_trait_config.py
        language: system
        files: ^(trait_config\.yaml|layers/)
        pass_filenames: false
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/validate_trait_config.py .pre-commit-config.yaml tests/test_trait_config.py
git commit -m "feat(rules): trait_config validation CLI + pre-commit hook"
```

### Task 9: Commit the confirmed default config + parity assertions

**⛔ Requires the human gate after PR-1: use the user-corrected `reports/body_affinity_draft.yaml`, not the raw draft.**

**Files:**
- Create: `trait_config.yaml` (repo root)
- Test: `tests/test_trait_config.py`

**Interfaces:**
- Produces: the shipped default config. Layer z-values: Background 10, Back 20, Body 30, Clothing 40, Mouth 50, Eyebrows 60, Eyes 70, Head 80, Accessory 90 (matches `swap_meta.TRAIT_ORDER`). `z_overrides` reproduce `ape_face.TOP_TRAITS` (Eyes/Wavy, Mouth/Rainbow Puke, Eyes/Laser Eyes, Eyes/Laser → z 95). `swap_matrix` per #30's table (universal: Accessory, Back; ape+skeleton: Head, Clothing; male+female: layers_except [Clothing]).

- [ ] **Step 1: Write the failing parity test**

```python
def test_default_config_parity_with_legacy_constants():
    from lfg_core import ape_face
    from lfg_core.swap_meta import TRAIT_ORDER

    trait_config.reset_config()
    cfg = trait_config.get_config()  # loads repo-root trait_config.yaml
    assert cfg.layer_order() == TRAIT_ORDER
    for top in ape_face.TOP_TRAITS:
        assert cfg.z_for(top["trait_type"], top["value"]) > max(
            layer.z for layer in cfg.layers
        ), f"{top} must render above all layers"
    assert cfg.universal_layers == frozenset({"Accessory", "Back"})
    assert cfg.swap_allowed("ape", "skeleton", "Clothing")
    assert cfg.swap_allowed("male", "female", "Eyes")
    assert not cfg.swap_allowed("male", "female", "Clothing")
    trait_config.reset_config()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py::test_default_config_parity_with_legacy_constants -v`
Expected: FAIL with `FileNotFoundError` (no trait_config.yaml yet)

- [ ] **Step 3: Build `trait_config.yaml`**

Assemble at repo root: the fixed header below + the **user-confirmed** affinity section pasted from the corrected `reports/body_affinity_draft.yaml`.

```yaml
# trait_config.yaml — declarative trait rules (spec:
# docs/superpowers/specs/2026-07-04-trait-rules-body-affinity-design.md)
# Validated by scripts/validate_trait_config.py (pre-commit + CI).
version: 1
layers:
  - {name: Background, z: 10, shared: true}
  - {name: Back,       z: 20, shared: true}
  - {name: Body,       z: 30}
  - {name: Clothing,   z: 40}
  - {name: Mouth,      z: 50}
  - {name: Eyebrows,   z: 60}
  - {name: Eyes,       z: 70}
  - {name: Head,       z: 80}
  - {name: Accessory,  z: 90}
z_overrides:
  - {trait_type: Eyes,  value: Wavy,         z: 95}
  - {trait_type: Mouth, value: Rainbow Puke, z: 95}
  - {trait_type: Eyes,  value: Laser Eyes,   z: 95}
  - {trait_type: Eyes,  value: Laser,        z: 95}
swap_matrix:
  universal_layers: [Accessory, Back]
  pairs:
    - {bodies: [ape, skeleton], layers: [Head, Clothing]}
    - {bodies: [male, female],  layers_except: [Clothing]}
# affinity: <paste the user-confirmed section from reports/body_affinity_draft.yaml>
exclusions: []
inclusions: []
```

Then validate for real: `.venv/bin/python scripts/validate_trait_config.py` — fix any errors (they mean the confirmed draft disagrees with the dirs; resolve with the user, do not silently edit).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v && .venv/bin/python scripts/validate_trait_config.py`
Expected: PASS (14 tests); CLI exit 0

- [ ] **Step 5: Commit + open draft PR-2**

```bash
git add trait_config.yaml tests/test_trait_config.py
git commit -m "feat(rules): ship user-confirmed default trait_config.yaml (#40, #28)"
git push -u origin feat/trait-config-engine
gh pr create --draft --title "feat: trait rules engine — config, queries, validation (#40)" --body "Phase 2. Engine + confirmed body-affinity config. Parity-tested against TRAIT_ORDER/TOP_TRAITS. Closes #28.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

# PR-3 — Mint/compose integration (`feat/rules-mint-integration`)

### Task 10: Affinity filtering in `select_random_attributes`

**Files:**
- Modify: `lfg_core/traits.py`
- Test: `tests/test_traits_affinity.py`

**Interfaces:**
- Consumes: `trait_config.get_config().value_allowed(body, trait_type, value)`.
- Produces: unchanged signature `select_random_attributes(store, body=None, *, conn=None, network=None, now=None, rng=random)`; candidates are dir values ∩ affinity before `rarity.weighted_pick`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_traits_affinity.py
# <env-guard preamble verbatim from tests/test_seasons.py lines 1-18>

import asyncio  # noqa: E402
import sqlite3  # noqa: E402

from lfg_core import trait_config, traits  # noqa: E402
from lfg_core.layer_store import LocalLayerStore  # noqa: E402

CFG = """
version: 1
layers:
  - {name: Background, z: 10}
  - {name: Back, z: 20}
  - {name: Body, z: 30}
  - {name: Clothing, z: 40}
  - {name: Mouth, z: 50}
  - {name: Eyebrows, z: 60}
  - {name: Eyes, z: 70}
  - {name: Head, z: 80}
  - {name: Accessory, z: 90}
affinity:
  Clothing:
    "Summer Dress": [female]
"""


def _mklayers(tmp_path):
    for body in ("male", "female"):
        for t, values in {
            "Background": ["Sunset"],
            "Body": ["Straight" if body == "male" else "Curved"],
            "Clothing": ["Summer Dress", "Hoodie"],
        }.items():
            d = tmp_path / "layers" / body / t
            d.mkdir(parents=True, exist_ok=True)
            for v in values:
                (d / f"{v}.png").write_bytes(b"x")
    return str(tmp_path / "layers")


def test_mint_selection_respects_affinity(tmp_path, monkeypatch):
    cfg_path = tmp_path / "trait_config.yaml"
    cfg_path.write_text(CFG)
    trait_config.reset_config()
    trait_config.get_config(str(cfg_path))
    store = LocalLayerStore(_mklayers(tmp_path))
    conn = sqlite3.connect(":memory:")

    class ForceDress:  # rng whose choices always favor Summer Dress if present
        def random(self):
            return 0.0

        def choices(self, population, weights=None, k=1):
            for p in population:
                if p == "Summer Dress":
                    return ["Summer Dress"]
            return [population[0]]

        def choice(self, population):
            return population[0]

        def shuffle(self, x):
            pass

    _, attrs = asyncio.run(
        traits.select_random_attributes(
            store, "male", conn=conn, network="testnet", rng=ForceDress()
        )
    )
    clothing = next(a["value"] for a in attrs if a["trait_type"] == "Clothing")
    assert clothing != "Summer Dress"  # female-only; filtered before the pick
    trait_config.reset_config()
```

(If `rarity.weighted_pick` uses different rng methods, adapt `ForceDress` to whatever `lfg_core/rarity.py:136` actually calls — read it first; the assertion is what matters.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_traits_affinity.py -v`
Expected: FAIL — `clothing == "Summer Dress"` (no filtering exists yet)

- [ ] **Step 3: Write minimal implementation** — in `lfg_core/traits.py`, inside the `for trait_type in TRAIT_ORDER:` loop (line 47), filter before the pick:

```python
from lfg_core import rarity, trait_config
...
        for trait_type in TRAIT_ORDER:
            values = await store.list_values(body, trait_type)
            cfg = trait_config.get_config()
            values = [
                v
                for v in values
                if cfg.value_allowed(body, trait_type, v)
                and not cfg.conflicts(attributes, trait_type, v)
            ]
            if values:
                ...
```

(`attributes` is the already-selected list the loop builds — filtering against it as we go means no re-roll loop is needed: an excluded value simply never enters the candidate set. Exclusions are empty at launch, so this is machinery-only.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_traits_affinity.py -v && .venv/bin/python -m pytest tests/ webapp/ -q`
Expected: new test PASS; full suite green (default config's affinity mirrors the dirs, so behavior is unchanged for existing tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/traits.py tests/test_traits_affinity.py
git commit -m "feat(rules): mint selection filters candidates by body affinity"
```

### Task 11: Compose z-order from config (TRAIT_ORDER / TOP_TRAITS become shims)

**Files:**
- Modify: `lfg_core/swap_compose.py` (the TOP_TRAITS reorder), `lfg_core/swap_meta.py:17` (TRAIT_ORDER), `lfg_core/ape_face.py:27` (TOP_TRAITS)
- Test: `tests/test_trait_config.py`

**Interfaces:**
- Consumes: `cfg.sort_attributes(attrs)` from Task 6.
- Produces: `swap_meta.TRAIT_ORDER` and `ape_face.TOP_TRAITS` still exist (importers unchanged) but carry a comment pointing at trait_config as the source of truth; compose ordering flows through `cfg.sort_attributes`.

- [ ] **Step 1: Write the failing test**

```python
def test_compose_ordering_uses_config_sort():
    import inspect

    from lfg_core import swap_compose

    src = inspect.getsource(swap_compose)
    assert "sort_attributes" in src, "compose must order layers via trait_config"
```

Plus a behavioral test: read `lfg_core/swap_compose.py:58` (`compose_nft`) first; find where it iterates attributes to resolve layer files, and assert (with a stub store recording resolution order) that a Wavy-Eyes attr resolves **after** Accessory. Write that test against the real signature — do not guess it.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_trait_config.py -v -k compose_ordering`
Expected: FAIL

- [ ] **Step 3: Implement** — in `swap_compose.compose_nft`, replace the existing TOP_TRAITS move-to-end logic with:

```python
from lfg_core import trait_config
...
    attrs = trait_config.get_config().sort_attributes(attrs)
```

Keep `ape_face.TOP_TRAITS` and `swap_meta.TRAIT_ORDER` defined as they are (importers in `swap_meta.normalize_attributes` etc. still use them) but add above each:

```python
# Source of truth is trait_config.yaml (z_overrides / layers). Keep in sync;
# test_default_config_parity_with_legacy_constants enforces the parity.
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/ webapp/ -q`
Expected: full suite green

- [ ] **Step 5: Commit**

```bash
git add lfg_core/swap_compose.py lfg_core/swap_meta.py lfg_core/ape_face.py tests/test_trait_config.py
git commit -m "refactor(rules): compose layer ordering flows through trait_config"
```

### Task 12: Property test — engine mints only legal combos

**Files:**
- Test: `tests/test_traits_affinity.py`

- [ ] **Step 1: Write the test** (it should pass immediately — it's a safety net, not TDD)

```python
def test_property_random_mints_are_affinity_valid(tmp_path):
    import random

    trait_config.reset_config()
    cfg = trait_config.get_config()  # real repo config
    store = LocalLayerStore("layers")  # real repo layers
    conn = sqlite3.connect(":memory:")
    rng = random.Random(1234)
    for _ in range(200):
        body, attrs = asyncio.run(
            traits.select_random_attributes(store, conn=conn, network="testnet", rng=rng)
        )
        for a in attrs:
            assert cfg.value_allowed(body, a["trait_type"], a["value"]), (
                f"illegal mint: {body}/{a['trait_type']}/{a['value']}"
            )
    trait_config.reset_config()
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_traits_affinity.py -v`
Expected: PASS. If it fails, a dir value contradicts the confirmed config — surface to the user; do not weaken the test.

- [ ] **Step 3: Commit + open draft PR-3**

```bash
git add tests/test_traits_affinity.py
git commit -m "test(rules): property test — 200 seeded mints are affinity-valid"
git push -u origin feat/rules-mint-integration
gh pr create --draft --title "feat: mint + compose consume the trait rules engine (#40)" --body "Phase 3: affinity-filtered selection, config-driven z-order, parity + property tests.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

# PR-4 — Cross-body swapping #30 (`feat/cross-body-swaps`)

### Task 13: Cross-body layer resolution in compose

**Files:**
- Modify: `lfg_core/swap_compose.py`
- Test: `tests/test_cross_body_resolve.py`

**Interfaces:**
- Produces: `async resolve_layer(store, cfg, body, trait_type, value) -> str | None` in `swap_compose` — resolution order: own body dir → (PR-5 adds shared here) → any matrix-permitted foreign body dir where `cfg.swap_allowed(body, foreign, trait_type)` and the value's affinity allows the foreign body. `compose_nft` resolves every layer through it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cross_body_resolve.py
# <env-guard preamble verbatim from tests/test_seasons.py lines 1-18>

import asyncio  # noqa: E402

from lfg_core import swap_compose, trait_config  # noqa: E402
from lfg_core.layer_store import LocalLayerStore  # noqa: E402

CFG = """
version: 1
layers:
  - {name: Background, z: 10}
  - {name: Back, z: 20}
  - {name: Body, z: 30}
  - {name: Clothing, z: 40}
  - {name: Mouth, z: 50}
  - {name: Eyebrows, z: 60}
  - {name: Eyes, z: 70}
  - {name: Head, z: 80}
  - {name: Accessory, z: 90}
swap_matrix:
  universal_layers: [Accessory, Back]
  pairs:
    - {bodies: [ape, skeleton], layers: [Head, Clothing]}
"""


def test_resolve_layer_falls_back_to_permitted_foreign_dir(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(CFG)
    cfg = trait_config.load_config(str(cfg_path))
    d = tmp_path / "layers" / "skeleton" / "Head"
    d.mkdir(parents=True)
    (d / "Crown.png").write_bytes(b"x")
    store = LocalLayerStore(str(tmp_path / "layers"))

    # ape has no Crown file; skeleton does, and ape<->skeleton Head is permitted
    path = asyncio.run(swap_compose.resolve_layer(store, cfg, "ape", "Head", "Crown"))
    assert path and path.endswith("skeleton/Head/Crown.png")
    # Eyes is not matrix-permitted for ape<->skeleton: no fallback
    (tmp_path / "layers" / "skeleton" / "Eyes").mkdir()
    (tmp_path / "layers" / "skeleton" / "Eyes" / "Hypno.png").write_bytes(b"x")
    assert asyncio.run(swap_compose.resolve_layer(store, cfg, "ape", "Eyes", "Hypno")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cross_body_resolve.py -v`
Expected: FAIL with `AttributeError: … no attribute 'resolve_layer'`

- [ ] **Step 3: Write minimal implementation** (in `swap_compose`, near the top)

```python
async def resolve_layer(store, cfg, body: str, trait_type: str, value: str) -> str | None:
    """Own dir first; else any matrix-permitted foreign dir (cross-body swaps
    render the source body's asset). Affinity narrower than the matrix wins."""
    path = await store.resolve(body, trait_type, value)
    if path:
        return path
    for foreign in await store.list_bodies():
        if foreign == body or not cfg.swap_allowed(body, foreign, trait_type):
            continue
        if not cfg.value_allowed(foreign, trait_type, value):
            continue
        path = await store.resolve(foreign, trait_type, value)
        if path:
            return path
    return None
```

Then route `compose_nft`'s per-attribute resolution through `resolve_layer` (read `compose_nft` first; replace its direct `store.resolve(body, …)` calls for trait layers — NOT the ape-structural `resolve_asset` calls).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_cross_body_resolve.py tests/ webapp/ -q`
Expected: green (same-body resolution short-circuits first, so existing behavior is unchanged)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/swap_compose.py tests/test_cross_body_resolve.py
git commit -m "feat(swap): cross-body layer resolution per swap matrix (#30)"
```

### Task 14: API enforcement in `handle_swap_start`

**Files:**
- Modify: `lfg_service/app.py` (the same-body gate inside `handle_swap_start`, currently `if nft1["gender"] != nft2["gender"]:` around line 803)
- Test: `tests/test_swap_cross_body_api.py`

**Interfaces:**
- Consumes: `trait_config.get_config().swap_allowed(a, b, layer)`.
- Produces: per-trait enforcement — 400 lists the blocked layers; permitted cross-body pairs proceed. Response error string: `"trait(s) <X, Y> cannot swap between <bodyA> and <bodyB> bodies"`.

- [ ] **Step 1: Write the failing test**

Copy the aiohttp app/client fixture pattern used by the existing swap tests in `webapp/test_smoke.py` (search it for `handle_swap_start` or `/api/swap` to find the fixture; reuse its auth/monkeypatch approach verbatim). Then:

```python
async def test_cross_body_swap_permitted_layers_pass(client, two_wallet_nfts):
    # two_wallet_nfts: one ape NFT + one skeleton NFT in the same wallet
    resp = await client.post("/api/swap", json={
        "nft1_id": "APE1", "nft2_id": "SKEL1", "traits": ["Head", "Clothing"],
    })
    assert resp.status == 200


async def test_cross_body_swap_blocked_layer_rejected(client, two_wallet_nfts):
    resp = await client.post("/api/swap", json={
        "nft1_id": "APE1", "nft2_id": "SKEL1", "traits": ["Head", "Eyes"],
    })
    assert resp.status == 400
    body = await resp.json()
    assert "Eyes" in body["error"] and "ape" in body["error"]


async def test_same_body_swap_unaffected(client, two_wallet_nfts_same_body):
    resp = await client.post("/api/swap", json={
        "nft1_id": "M1", "nft2_id": "M2", "traits": ["Clothing", "Eyes"],
    })
    assert resp.status == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_swap_cross_body_api.py -v`
Expected: FAIL — cross-body requests get 400 "NFTs must share the same body type"

- [ ] **Step 3: Implement** — replace the same-body gate in `handle_swap_start`:

```python
    cfg = trait_config.get_config()
    blocked = [
        t for t in traits_to_swap
        if not cfg.swap_allowed(nft1["gender"], nft2["gender"], t)
    ]
    if blocked:
        return web.json_response(
            {
                "error": (
                    f"trait(s) {', '.join(blocked)} cannot swap between "
                    f"{nft1['gender']} and {nft2['gender']} bodies"
                )
            },
            status=400,
        )
```

(`gender` is the normalized-NFT field name — `swap_meta.detect_body`'s value; do not rename it here.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_swap_cross_body_api.py tests/ webapp/ -q`
Expected: green — including existing same-body swap tests

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_swap_cross_body_api.py
git commit -m "feat(swap): enforce cross-body swap matrix at the API (#30)"
```

### Task 15: UI filtering (matrix in `/api/nfts` payload + JS filter)

**Files:**
- Modify: `lfg_service/app.py` (`handle_nfts`, line ~751)
- Modify: the swap-UI JS (find it: `grep -rn "swappable_traits" webapp/ surfaces/ --include='*.js' --include='*.html'`)
- Test: `tests/test_swap_cross_body_api.py`

**Interfaces:**
- Produces: `handle_nfts` response gains `"swap_matrix": {"universal_layers": [...], "pairs": [{"bodies": [...], "layers": [...] | null, "layers_except": [...] | null}]}`. Frontend disables/hides trait checkboxes not permitted for the selected NFT pair (mirror of `swap_allowed` in JS).

- [ ] **Step 1: Write the failing test**

```python
async def test_nfts_payload_includes_swap_matrix(client):
    resp = await client.get("/api/nfts")
    data = await resp.json()
    assert "swap_matrix" in data
    assert "universal_layers" in data["swap_matrix"]
```

- [ ] **Step 2: Run to verify it fails, then implement** — in `handle_nfts`, extend the existing `json_response`:

```python
    cfg = trait_config.get_config()
    matrix = {
        "universal_layers": sorted(cfg.universal_layers),
        "pairs": [
            {
                "bodies": sorted(p.bodies),
                "layers": sorted(p.layers) if p.layers is not None else None,
                "layers_except": (
                    sorted(p.layers_except) if p.layers_except is not None else None
                ),
            }
            for p in cfg.swap_pairs
        ],
    }
    return web.json_response(
        {"nfts": nfts, "swappable_traits": swap_meta.SWAPPABLE_TRAITS,
         "swap_fee": swap_fee, "swap_matrix": matrix}
    )
```

In the swap JS, add a mirror of `swap_allowed` and apply it wherever the trait checkboxes render (the code that reads `swappable_traits`):

```javascript
function swapAllowed(matrix, bodyA, bodyB, layer) {
  if (bodyA === bodyB || matrix.universal_layers.includes(layer)) return true;
  return matrix.pairs.some((p) => {
    if (!p.bodies.includes(bodyA) || !p.bodies.includes(bodyB)) return false;
    if (p.layers) return p.layers.includes(layer);
    return !p.layers_except.includes(layer);
  });
}
```

Also remove any front-end "same body type" pre-filter that hides cross-body NFT pairs entirely (search the JS for the same-body error string / gender comparison).

- [ ] **Step 3: Run tests + eyeball the UI**

Run: `.venv/bin/python -m pytest tests/ webapp/ -q`
Then `WEBAPP_DEV_MODE=1` local harness: select an ape + a skeleton, confirm only Head/Clothing/Accessory/Back are offered; male + female offers everything except Clothing.

- [ ] **Step 4: Commit**

```bash
git add lfg_service/app.py tests/test_swap_cross_body_api.py   # plus the JS file located in Step 2
git commit -m "feat(swap-ui): filter offered traits by cross-body matrix (#30)"
```

### Task 16: Economy-path gating (equip / assemble / deposit)

**Files:**
- Modify: `webapp/economy_api.py` (`start_equip` line ~170, `start_assemble` line ~202, `start_deposit` line ~247)
- Test: `webapp/test_economy_api.py` (existing file — follow its fixture style)

**Interfaces:**
- Consumes: `trait_config.get_config().value_allowed(body, slot, value)` + `swap_allowed`.
- Produces: each op validates *before* starting a session: the asset's `(slot, value)` must be legal on the target character's body — legal ≡ `value_allowed(char_body, slot, value)` AND (value exists in char body dir OR in a matrix-permitted foreign dir — reuse `swap_compose.resolve_layer` returning non-None). Illegal → the API's existing 4xx error shape with `"'<value>' does not fit a <body> body"`.

- [ ] **Step 1: Write the failing test** (in `webapp/test_economy_api.py`, copying its existing equip-test setup)

```python
async def test_equip_rejects_incompatible_body_value(client, equip_fixture):
    # equip_fixture: male character + a closet asset (Clothing, "Summer Dress")
    # where trait_config marks Summer Dress female-only
    resp = await client.post("/api/economy/equip", json={
        "nft_id": equip_fixture.char_id, "slot": "Clothing", "value": "Summer Dress",
    })
    assert resp.status == 400
    assert "does not fit" in (await resp.json())["error"]
```

- [ ] **Step 2: Run to verify it fails, then implement** — at the top of each of the three `start_*` functions, after the character/asset is loaded (each already loads the character via `_load_owned_character`; read the function first to place the check after body is known):

```python
    from lfg_core import swap_compose, trait_config
    from lfg_core.layer_store import get_layer_store

    cfg = trait_config.get_config()
    if not cfg.value_allowed(char_body, slot, value) or not await swap_compose.resolve_layer(
        get_layer_store(), cfg, char_body, slot, value
    ):
        raise EconomyRequestError(f"'{value}' does not fit a {char_body} body")
```

(Match the module's real error-raising convention — read how `start_equip` reports bad input today and use the same mechanism; `EconomyRequestError` above is illustrative, not gospel. For `start_assemble`, run the check per asset in the set; for `start_deposit`, check against the depositing owner's… nothing — deposit returns a trait to the Closet and is body-agnostic, so gate deposit ONLY if the implementation already requires a body match; otherwise skip deposit and note it in the PR body.)

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py tests/ webapp/ -q`
Expected: green

- [ ] **Step 4: Commit + open draft PR-4**

```bash
git add webapp/economy_api.py webapp/test_economy_api.py
git commit -m "feat(economy): gate equip/assemble on body affinity (#30)"
git push -u origin feat/cross-body-swaps
gh pr create --draft --title "feat: cross-body trait swapping per compatibility matrix (#30)" --body "Phase 4: matrix-permitted cross-body swaps (API-enforced, UI-filtered), cross-body layer resolution, economy gating. Closes #30.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

# PR-5 — Physical `layers/shared/` (`feat/shared-layer-dirs`) — LAST

### Task 17: Layer-store union lookup

**Files:**
- Modify: `lfg_core/layer_store.py` (`LocalLayerStore.list_bodies/list_values/resolve`, `CdnLayerStore` same)
- Test: `tests/test_shared_layers.py`

**Interfaces:**
- Produces: `SHARED_DIR = "shared"`; `list_bodies()` excludes it; `list_values(body, t)` = sorted union(body dir, shared dir); `resolve(body, t, v)` checks body dir then `shared/`; `resolve_layer` (from Task 13) gains the shared hop between own and foreign dirs automatically because it calls `store.resolve`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared_layers.py
# <env-guard preamble verbatim from tests/test_seasons.py lines 1-18>

import asyncio  # noqa: E402

from lfg_core.layer_store import LocalLayerStore  # noqa: E402


def test_shared_dir_union(tmp_path):
    (tmp_path / "male" / "Background").mkdir(parents=True)
    (tmp_path / "male" / "Background" / "Exclusive.png").write_bytes(b"x")
    (tmp_path / "shared" / "Background").mkdir(parents=True)
    (tmp_path / "shared" / "Background" / "Sunset.png").write_bytes(b"x")
    store = LocalLayerStore(str(tmp_path))
    assert asyncio.run(store.list_bodies()) == ["male"]  # shared is not a body
    assert asyncio.run(store.list_values("male", "Background")) == ["Exclusive", "Sunset"]
    path = asyncio.run(store.resolve("male", "Background", "Sunset"))
    assert path and "shared/Background" in path
    assert asyncio.run(store.resolve("male", "Background", "Exclusive")).endswith(
        "male/Background/Exclusive.png"
    )
```

- [ ] **Step 2: Run to verify FAIL, implement in `LocalLayerStore`:**

```python
SHARED_DIR = "shared"

    async def list_bodies(self) -> list[str]:
        return sorted(
            d
            for d in os.listdir(self.base_dir)
            if os.path.isdir(os.path.join(self.base_dir, d))
            and not d.startswith(".")
            and d != SHARED_DIR
        )

    async def list_values(self, body: str, trait_type: str) -> list[str]:
        values = set(self._list_values_one(body, trait_type))
        values |= set(self._list_values_one(SHARED_DIR, trait_type))
        return sorted(values)
```

(extract the current `list_values` body into `_list_values_one(dirname, trait_type)`; `resolve` tries `body` then `SHARED_DIR` via the existing loop). Mirror the same three changes in `CdnLayerStore`, tolerating a missing `shared/` CDN dir (catch the listing exception for `SHARED_DIR` only and treat as empty).

- [ ] **Step 3: Run full suite** — `.venv/bin/python -m pytest tests/ webapp/ -q` (no `shared/` dir exists yet, so union is a no-op for everything else)

- [ ] **Step 4: Commit**

```bash
git add lfg_core/layer_store.py tests/test_shared_layers.py
git commit -m "feat(layers): shared/ union lookup in both layer stores"
```

### Task 18: Seasons fallback for shared keys

**Files:**
- Modify: `lfg_core/seasons.py:43` (`get_season`)
- Test: `tests/test_seasons.py`

**Interfaces:**
- Produces: `get_season(manifest, body, category, value)` falls back to the `shared/<category>/<value>` key when the per-body key misses.

- [ ] **Step 1: Failing test**

```python
def test_get_season_falls_back_to_shared_key():
    manifest = {"shared/Background/Sunset": 2}
    assert seasons.get_season(manifest, "male", "Background", "Sunset") == 2
```

- [ ] **Step 2: Implement**

```python
def get_season(manifest, body, category, value):
    return manifest.get(f"{body}/{category}/{value}") or manifest.get(
        f"shared/{category}/{value}"
    )
```

(Read the real signature at `lfg_core/seasons.py:43` first and keep it — the point is the fallback, not a rewrite.)

- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/test_seasons.py -q` → green. **Commit.**

```bash
git add lfg_core/seasons.py tests/test_seasons.py
git commit -m "feat(seasons): shared/ key fallback in get_season"
```

### Task 19: Migration script + execution

**Files:**
- Create: `scripts/migrate_shared_layers.py`
- Test: `tests/test_shared_layers.py`

**Interfaces:**
- Produces: `migrate(layers_dir, trait_types, dry_run) -> dict` — for each value of each listed trait type present in ALL four body dirs with **byte-identical** files: move one copy to `shared/<type>/`, delete the per-body copies. Values present in fewer than 4 dirs or differing by bytes are skipped and reported. Idempotent. CLI defaults `--trait-types Background Back`, `--dry-run` default ON.

- [ ] **Step 1: Failing test**

```python
def test_migrate_moves_identical_and_skips_divergent(tmp_path):
    import hashlib

    for body in ("ape", "female", "male", "skeleton"):
        d = tmp_path / body / "Background"
        d.mkdir(parents=True)
        (d / "Sunset.png").write_bytes(b"same")
        (d / "City.png").write_bytes(body.encode())  # divergent bytes

    from scripts.migrate_shared_layers import migrate

    result = migrate(str(tmp_path), ["Background"], dry_run=False)
    assert (tmp_path / "shared" / "Background" / "Sunset.png").exists()
    assert not (tmp_path / "male" / "Background" / "Sunset.png").exists()
    assert (tmp_path / "male" / "Background" / "City.png").exists()  # skipped
    assert ("Background", "City", "divergent") in result["skipped"]
    # idempotent second run
    assert migrate(str(tmp_path), ["Background"], dry_run=False)["moved"] == []
```

- [ ] **Step 2: Implement**

```python
# scripts/migrate_shared_layers.py
# Move byte-identical universal layer values into layers/shared/<type>/.
# Verify-then-move: anything not identical across ALL body dirs is skipped
# and reported, never guessed.

import argparse
import hashlib
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BODIES = ["ape", "female", "male", "skeleton"]
EXTS = (".png", ".gif", ".mp4")


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def migrate(layers_dir: str, trait_types: list[str], dry_run: bool = True) -> dict:
    moved, skipped = [], []
    for trait_type in trait_types:
        values: set[str] = set()
        for body in BODIES:
            d = os.path.join(layers_dir, body, trait_type)
            if os.path.isdir(d):
                values |= {
                    os.path.splitext(f)[0]
                    for f in os.listdir(d)
                    if os.path.splitext(f)[1].lower() in EXTS
                }
        for value in sorted(values):
            paths = []
            for body in BODIES:
                for ext in EXTS:
                    p = os.path.join(layers_dir, body, trait_type, value + ext)
                    if os.path.isfile(p):
                        paths.append(p)
                        break
            if len(paths) < len(BODIES):
                skipped.append((trait_type, value, "not-in-all-bodies"))
                continue
            if len({_digest(p) for p in paths}) != 1:
                skipped.append((trait_type, value, "divergent"))
                continue
            dest_dir = os.path.join(layers_dir, "shared", trait_type)
            dest = os.path.join(dest_dir, os.path.basename(paths[0]))
            moved.append((trait_type, value))
            if not dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(paths[0], dest)
                for p in paths:
                    os.remove(p)
    return {"moved": moved, "skipped": skipped}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--layers-dir", default="layers")
    p.add_argument("--trait-types", nargs="+", default=["Background", "Back"])
    p.add_argument("--execute", action="store_true", help="default is dry-run")
    args = p.parse_args()
    result = migrate(args.layers_dir, args.trait_types, dry_run=not args.execute)
    for t, v in result["moved"]:
        print(f"{'would move' if not args.execute else 'moved'}: {t}/{v}")
    for t, v, why in result["skipped"]:
        print(f"skipped ({why}): {t}/{v}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run tests, then the real migration**

```bash
.venv/bin/python -m pytest tests/test_shared_layers.py -v
.venv/bin/python scripts/migrate_shared_layers.py                      # dry-run: review the list
.venv/bin/python scripts/migrate_shared_layers.py --execute
.venv/bin/python scripts/validate_trait_config.py                      # config still consistent
.venv/bin/python -m pytest tests/ webapp/ -q                           # full suite
```

Universal Accessory values (per the confirmed audit) may be added to `--trait-types Accessory` in the same run **only if** the audit classified them `universal` — otherwise leave Accessory per-body.

- [ ] **Step 4: Update tooling that walks the tree** — `scripts/audit_layer_coverage.py` and `scripts/upload_layers_cdn.py`: grep each for hardcoded body iteration and include `shared/` (read them; the change is "also walk `shared/`", nothing more). Also update the seasons manifest keys for moved values (`layers/seasons.json`): rewrite `<body>/<type>/<value>` → `shared/<type>/<value>` for moved values, collapsing the 4 duplicates to 1 (small script inline or manual `python -c`; the Task-18 fallback makes this non-breaking either way).

- [ ] **Step 5: Commit + open draft PR-5**

```bash
git add -A layers/ scripts/migrate_shared_layers.py scripts/audit_layer_coverage.py scripts/upload_layers_cdn.py tests/test_shared_layers.py layers/seasons.json
git commit -m "feat(layers): physical shared/ tree for universal values"
git push -u origin feat/shared-layer-dirs
gh pr create --draft --title "feat: physical layers/shared/ for universal trait values" --body "Phase 5 (final). Byte-identical universal values deduped into shared/; union lookup landed earlier keeps all consumers working. CDN re-upload required after merge (scripts/upload_layers_cdn.py).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## Post-merge ops checklist (not part of any PR)

- [ ] Re-run `scripts/upload_layers_cdn.py` so the CDN mirrors `shared/` (mint uploads still need Bunny).
- [ ] `pm2 restart lfg-activity lfg-bot` after each merge that touches serving code (post-merge hook covers lfg-activity).
- [ ] Comment on #40/#28/#30/#39 with what shipped; close #28 (PR-2), #40 (PR-3), #30 (PR-4); retitle #39 to the post-launch admin panel scope.
- [ ] `scripts/audit_layer_coverage.py` + `scripts/audit_history.py --network mainnet` after PR-5 as a final conservation check.
