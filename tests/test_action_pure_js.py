import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_js(expr: str):
    script = (
        "import * as M from './webapp/client/action_pure.js';\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_action_route_only_accepts_mint():
    assert run_js("M.requestedAction('?action=mint')") == "mint"
    assert run_js("M.requestedAction('?action=other')") is None
    assert run_js("M.requestedAction('?action=mint&action=other')") == "mint"


def test_terminal_action_states():
    for state in ("done", "rejected", "expired", "failed", "indeterminate"):
        assert run_js(f"M.actionIsTerminal('{state}')") is True
    assert run_js("M.actionIsTerminal('confirming')") is False


def test_action_error_copy_is_safe_and_bounded():
    assert "ticket" in run_js("M.actionErrorCopy('ticket_unavailable')").lower()
    assert (
        run_js("M.actionErrorCopy('<script>')")
        == "The atomic mint did not complete. No payment was taken."
    )


def test_active_action_session_id_requires_live_session():
    assert (
        run_js(
            "M.activeActionSessionId({session:{sessionId:'s1',state:'confirming'}})"
        )
        == "s1"
    )
    assert (
        run_js("M.activeActionSessionId({session:{sessionId:'s1',state:'done'}})")
        is None
    )
    assert run_js("M.activeActionSessionId({session:null})") is None


def test_app_boot_routes_mint_action_after_auth():
    src = Path(ROOT, "webapp/client/app.js").read_text()
    assert "./action_pure.js" in src
    assert "requestedAction(window.location.search)" in src
    assert "startAtomicMint" in src
    main = src.split("async function main", 1)[1]
    assert "await resumeAtomicMint()" in main
    assert "await startAtomicMint()" in main


def test_action_ui_never_calls_legacy_accept_endpoint():
    src = Path(ROOT, "webapp/client/app.js").read_text()
    body = src.split("async function startAtomicMint", 1)[1].split(
        "async function resumeAtomicMint", 1
    )[0]
    assert "/api/actions/mint" in body
    assert "create_accept" not in body
    assert "/api/mint" not in body.replace("/api/actions/mint", "")


def test_pages_workflow_publishes_and_rewrites_action_discovery():
    discovery = Path(
        ROOT, "webapp/client/.well-known/xrpl-actions.json"
    )
    assert discovery.exists()
    assert json.loads(discovery.read_text())["rules"][0]["apiPath"] == (
        "/api/actions/**"
    )
    workflow = Path(ROOT, ".github/workflows/pages.yml").read_text()
    assert "cp -r webapp/client/. _site/" in workflow
    assert "_site/.well-known/xrpl-actions.json" in workflow
    assert "WEB_API_BASE" in workflow
