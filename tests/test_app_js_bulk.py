# tests/test_app_js_bulk.py
# The webapp client is no-build vanilla JS (no JS test harness) — guard the
# bulk-mint UI (#215) the same way tests/test_app_js_boot.py guards boot:
# assert the source contains the flag gate, the stepper, and the routing,
# and that the single-mint path survives unchanged.
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_index_has_qty_stepper_hidden_by_default():
    html = _read("index.html")
    assert 'id="flow-qty"' in html and "hidden" in html.split('id="flow-qty"')[1][:120]
    assert 'id="flow-qty-minus"' in html
    assert 'id="flow-qty-plus"' in html
    assert 'id="flow-qty-value"' in html


def test_app_js_gates_stepper_on_config_flag():
    src = _read("app.js")
    assert "bulk_mint_ui" in src
    assert "bulk_mint_max" in src
    assert "bulkCfg" in src


def test_app_js_routes_qty_to_bulk_and_preserves_single_mint():
    src = _read("app.js")
    assert "startBulkMint" in src
    # single-mint path untouched: startMint still POSTs /api/mint
    assert "api('/api/mint', { method: 'POST'" in src
    assert "'/api/mint/bulk'" in src


def test_index_has_bulk_panel():
    html = _read("index.html")
    assert 'id="bulk-panel"' in html
    assert 'id="bulk-progress"' in html
    assert 'id="bulk-units"' in html
    assert 'id="bulk-done-btn"' in html


def test_app_js_bulk_flow_wiring():
    src = _read("app.js")
    assert "function pollBulk(" in src
    assert "function renderBulkJob(" in src
    assert "async function bulkAccept(" in src
    assert "async function resumeBulkMint(" in src
    assert "'/api/mint/bulk/active'" in src
    assert "/units/" in src and "/accept" in src
    # accept payloads are lazy: exactly the one endpoint call site, no
    # eager loop over units[]
    assert src.count("/accept`") == 1


def test_app_js_bulk_resume_runs_before_single_resume():
    src = _read("app.js")
    # every boot path that resumes single mint checks bulk first: the two
    # call sites use the combined guard, so the counts must match
    assert src.count("await resumeBulkMint()") == src.count("await resumeMint()")
    assert "await resumeBulkMint()) && !(await resumeMint()" in src
