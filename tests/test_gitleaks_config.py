"""Regression tests for the secret-scan gate: .gitleaks.toml + scripts/gitleaks-scan.

Two holes, both verified 2026-09-22:

* gitleaks' default ruleset has no XRPL family-seed rule. A file holding a
  fresh ed25519 (`sEd…`, 31 chars) and secp256k1 (`s…`, 29 chars) seed
  produced no finding. This repo is public, has leaked a seed into history
  before, and the backend keeps hot seeds in .env.
* The upstream hook entry is `gitleaks git --pre-commit --staged`. The gate
  runs at pre-push and in CI, where nothing is staged, so it scanned 0 bytes
  and passed a committed GitHub token.

Every seed here is generated at runtime (unfunded, never persisted outside
tmp_path), and every scan runs with --redact, so a failing assertion can't
print one. The tests run the gitleaks binary the gate itself runs: the rev
pinned in .pre-commit-config.yaml, as pre-commit built it into its store.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml
from xrpl import CryptoAlgorithm
from xrpl.core.addresscodec import classic_address_to_xaddress
from xrpl.core.keypairs import derive_classic_address, derive_keypair, generate_seed

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / ".gitleaks.toml"
GATE = REPO_ROOT / "scripts" / "gitleaks-scan"
GITLEAKS_REPO = "https://github.com/gitleaks/gitleaks"
SEED_RULE = "xrpl-family-seed"


def _pinned_rev() -> str:
    cfg = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    for repo in cfg["repos"]:
        if repo["repo"] == GITLEAKS_REPO:
            return str(repo["rev"])
    raise AssertionError("no gitleaks repo in .pre-commit-config.yaml")


def _gitleaks_hook() -> dict[str, object]:
    cfg = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    for repo in cfg["repos"]:
        if repo["repo"] == GITLEAKS_REPO:
            (hook,) = [h for h in repo["hooks"] if h["id"] == "gitleaks"]
            return dict(hook)
    raise AssertionError("no gitleaks repo in .pre-commit-config.yaml")


def _store_binary() -> Path | None:
    """The pinned gitleaks pre-commit built for the gate, if it has built it.

    `pre-commit run` installs every hook environment before running any hook,
    so inside the gate (locally and in CI) this is always populated.
    """
    home = os.environ.get("PRE_COMMIT_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "pre-commit"
    )
    db = Path(home) / "db.db"
    if not db.is_file():
        return None
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT path FROM repos WHERE repo = ? AND ref = ?", (GITLEAKS_REPO, _pinned_rev())
        ).fetchone()
    if row is None:
        return None
    return next(iter(sorted(Path(row[0]).glob("golangenv-*/bin/gitleaks"))), None)


def _gitleaks() -> str:
    found = os.environ.get("GITLEAKS_BIN") or _store_binary() or shutil.which("gitleaks")
    if found:
        return str(found)
    # In CI the gate has always built it, so a miss there is a broken lookup,
    # never a reason to skip the only test of the seed rule.
    if os.environ.get("CI"):
        pytest.fail(f"no gitleaks binary for rev {_pinned_rev()} (pre-commit store / PATH)")
    pytest.skip("no gitleaks binary: run the pre-push gate once, or set GITLEAKS_BIN")


def _clean_env(**extra: str) -> dict[str, str]:
    """os.environ minus what the outer pre-push hook exports.

    Inside the gate git exports GIT_DIR & co. and pre-commit exports
    PRE_COMMIT_FROM_REF/TO_REF, which would point the temp repos' git (and the
    gate's range mode) at the outer push instead of the repo under test.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_", "PRE_COMMIT_")) and k != "GITLEAKS_CONFIG"
    }
    env.update(extra)
    return env


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _scan(repo: Path, tmp_path: Path) -> tuple[int, list[dict[str, object]]]:
    """Scan the repo's full history with the repo config; return (exit, findings)."""
    report = tmp_path / "report.json"
    out = subprocess.run(
        [
            _gitleaks(),
            "git",
            "--config",
            str(CONFIG),
            "--redact",
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            str(report),
            str(repo),
        ],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert out.returncode in (0, 1), out.stderr
    return out.returncode, json.loads(report.read_text())


def _run_gate(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    bindir = str(Path(_gitleaks()).parent)
    return subprocess.run(
        [str(GATE)],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_clean_env(PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", **env),
    )


# --- the rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("algorithm", "length"),
    [(CryptoAlgorithm.ED25519, 31), (CryptoAlgorithm.SECP256K1, 29)],
    ids=["ed25519", "secp256k1"],
)
def test_fresh_seed_is_flagged(tmp_path: Path, algorithm: CryptoAlgorithm, length: int) -> None:
    seed = generate_seed(algorithm=algorithm)
    assert len(seed) == length  # the regex's fixed lengths track xrpl-py's encoding
    repo = _repo(tmp_path)
    _commit(repo, "wallet.env", f"SEED={seed}\n")
    _commit(repo, "wallet.json", json.dumps({"secret": seed}) + "\n")

    code, findings = _scan(repo, tmp_path)

    assert code == 1
    assert sorted(f["File"] for f in findings if f["RuleID"] == SEED_RULE) == [
        "wallet.env",
        "wallet.json",
    ]


def test_public_xrpl_identifiers_pass(tmp_path: Path) -> None:
    """Addresses, hashes, NFTokenIDs, public keys: public by construction, never flagged.

    The XRPL field names matter: generic-api-key keys on words like "token"
    and "key", so `NFTokenID`/`RegularKey`/`nft_id` beside a hex id or an
    address used to trip it (76 findings on the tree at 2026-09-22).
    """
    lines = []
    for algorithm in (CryptoAlgorithm.ED25519, CryptoAlgorithm.SECP256K1):
        public_key, _private = derive_keypair(generate_seed(algorithm=algorithm))
        address = derive_classic_address(public_key)
        tx_hash = secrets.token_hex(32).upper()
        nft_id = "00080000" + secrets.token_hex(28).upper()
        lines += [
            f"owner = '{address}'",
            f'"Account": "{address}", "RegularKey": "{address}", "NFTokenMinter": "{address}",',
            f'"xaddress": "{classic_address_to_xaddress(address, None, False)}",',
            f'"SigningPubKey": "{public_key}",',
            f'"hash": "{tx_hash}", "NFTokenID": "{nft_id}", "nftoken_id": "{nft_id}",',
            f'"NFTokenBuyOffer": "{tx_hash}", "NFTokenSellOffer": "{tx_hash}",',
            f"nft_id={nft_id.lower()} tx={tx_hash.lower()}",
        ]
    lines += [
        "TOKEN_ISSUER_ADDRESS=rrrrrrrrrrrrrrrrrrrrrhoLvTp",
        "blackhole rrrrrrrrrrrrrrrrrNAMEtxvNvQ",
        "TOKEN_CURRENCY_HEX=4C46474F00000000000000000000000000000000",
    ]
    repo = _repo(tmp_path)
    _commit(repo, "ledger.txt", "\n".join(lines) + "\n")

    code, findings = _scan(repo, tmp_path)

    assert findings == []
    assert code == 0


def test_repo_config_is_picked_up_without_flag(tmp_path: Path) -> None:
    """gitleaks reads <target>/.gitleaks.toml on its own, so a bare
    `gitleaks git` from the repo root uses these rules too."""
    repo = _repo(tmp_path)
    shutil.copy(CONFIG, repo / ".gitleaks.toml")
    _commit(repo, "wallet.env", f"SEED={generate_seed()}\n")

    out = subprocess.run(
        [_gitleaks(), "git", "--redact", "--no-banner", "--verbose", str(repo)],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert out.returncode == 1
    assert SEED_RULE in out.stdout + out.stderr


# --- the gate wiring --------------------------------------------------------


def test_hook_runs_the_gate_not_the_staged_scan() -> None:
    hook = _gitleaks_hook()
    assert hook.get("entry") == "scripts/gitleaks-scan"
    assert os.access(GATE, os.X_OK)


def test_prepush_catches_seed_in_pushed_commits(tmp_path: Path) -> None:
    """A seed committed then deleted inside one push is still in history."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "wallet.env", f"SEED={generate_seed()}\n")
    head = _commit(repo, "wallet.env", "SEED=\n")

    out = _run_gate(repo, PRE_COMMIT_FROM_REF=base, PRE_COMMIT_TO_REF=head)

    assert out.returncode != 0, out.stdout + out.stderr
    assert SEED_RULE in out.stdout + out.stderr


def test_prepush_scans_only_the_pushed_range(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "wallet.env", f"SEED={generate_seed()}\n")
    base = _commit(repo, "wallet.env", "SEED=\n")  # already on the remote
    head = _commit(repo, "notes.md", "clean\n")

    out = _run_gate(repo, PRE_COMMIT_FROM_REF=base, PRE_COMMIT_TO_REF=head)

    assert out.returncode == 0, out.stdout + out.stderr


def test_root_push_scans_every_unpushed_commit(tmp_path: Path) -> None:
    """A push carrying a root commit gets no FROM/TO refs from pre-commit, only
    the remote name and local branch: scan the branch's unpushed history,
    not HEAD's snapshot, which no longer holds the seed."""
    repo = _repo(tmp_path)
    _commit(repo, "wallet.env", f"SEED={generate_seed()}\n")
    _commit(repo, "wallet.env", "SEED=\n")

    out = _run_gate(
        repo, PRE_COMMIT_REMOTE_NAME="origin", PRE_COMMIT_LOCAL_BRANCH="refs/heads/main"
    )

    assert out.returncode != 0, out.stdout + out.stderr
    assert SEED_RULE in out.stdout + out.stderr


def test_root_push_skips_commits_already_on_the_remote(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = _repo(tmp_path)
    _commit(repo, "wallet.env", f"SEED={generate_seed()}\n")
    _commit(repo, "wallet.env", "SEED=\n")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    _commit(repo, "notes.md", "clean\n")

    out = _run_gate(
        repo, PRE_COMMIT_REMOTE_NAME="origin", PRE_COMMIT_LOCAL_BRANCH="refs/heads/main"
    )

    assert out.returncode == 0, out.stdout + out.stderr


def test_all_files_mode_scans_tracked_tree(tmp_path: Path) -> None:
    """CI's `pre-commit run --all-files` sets no refs: scan HEAD's tree."""
    repo = _repo(tmp_path)
    _commit(repo, "config.py", f'SEED = "{generate_seed(algorithm=CryptoAlgorithm.SECP256K1)}"\n')

    out = _run_gate(repo)

    assert out.returncode != 0, out.stdout + out.stderr
    assert SEED_RULE in out.stdout + out.stderr


def test_all_files_mode_ignores_untracked_env(tmp_path: Path) -> None:
    """The main checkout's gitignored .env holds the live seeds; never scan it."""
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text(".env\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore .env")
    (repo / ".env").write_text(f"SEED={generate_seed()}\n")

    out = _run_gate(repo)

    assert out.returncode == 0, out.stdout + out.stderr
