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
    assert 'id="mint-qty"' in html and "hidden" in html.split('id="mint-qty"')[1][:120]
    assert 'id="qty-minus"' in html
    assert 'id="qty-plus"' in html
    assert 'id="qty-value"' in html


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
