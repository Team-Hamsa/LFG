"""Accessible structure for the wallet sign-in explainer."""

from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).parents[1] / "webapp" / "client" / "index.html"


class _DomParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[dict[str, str]] = []
        self.nodes: dict[str, dict[str, object]] = {}
        self.text: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "ancestors": tuple(self.stack)}
        node_id = node["attrs"].get("id")
        if node_id:
            self.nodes[node_id] = node
            self.text[node_id] = []
        if tag not in {"img", "input", "br", "hr", "meta", "link"}:
            self.stack.append(node["attrs"])

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        for ancestor in self.stack:
            node_id = ancestor.get("id")
            if node_id:
                self.text[node_id].append(data)


def _registration_dom():
    parser = _DomParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser


def test_each_wallet_action_describes_its_own_off_ledger_proof():
    """Catch a wallet action losing its provider-specific safety context."""
    dom = _registration_dom()
    nodes = dom.nodes

    expected = {
        "register-link-btn": "xaman-verification-note",
        "register-wc-btn": "joey-verification-note",
    }
    for action_id, note_id in expected.items():
        action = nodes[action_id]
        note = nodes[note_id]
        assert action["attrs"].get("aria-describedby") == note_id
        assert any(ancestor.get("id") == "register-panel" for ancestor in note["ancestors"])

    details = nodes["wallet-verification-help"]
    assert details["tag"] == "details"
    assert any(ancestor.get("id") == "register-panel" for ancestor in details["ancestors"])


def test_wallet_actions_use_local_decorative_app_icons_with_accessible_names():
    """Catch icon-only wallet actions losing identity or local artwork."""
    dom = _registration_dom()

    expected = {
        "register-link-btn": ("Open in Xaman", "xaman-app-icon", "assets/xaman-app-icon.png"),
        "register-wc-btn": (
            "Connect with Joey Wallet",
            "joey-app-icon",
            "assets/joey-app-icon.png",
        ),
    }
    for action_id, (label, icon_id, src) in expected.items():
        action = dom.nodes[action_id]
        icon = dom.nodes[icon_id]
        assert action["attrs"].get("aria-label") == label
        assert not " ".join(dom.text[action_id]).strip()
        assert icon["tag"] == "img"
        assert icon["attrs"].get("src") == src
        assert (INDEX.parent / src).is_file()
        assert icon["attrs"].get("alt") == ""
        assert any(ancestor.get("id") == action_id for ancestor in icon["ancestors"])


def test_wallet_copy_distinguishes_identity_linking_and_provider_proofs():
    """Catch misleading cross-surface or on-ledger wallet explanations."""
    dom = _registration_dom()
    panel = " ".join(" ".join(dom.text["register-panel"]).split())
    xaman = " ".join(" ".join(dom.text["xaman-verification-note"]).split())
    joey = " ".join(" ".join(dom.text["joey-verification-note"]).split())

    assert "connects your wallet to your LFG identity" in panel
    assert "SignIn pseudotransaction" in xaman
    assert "never submitted to the XRP Ledger" in xaman
    assert "1-drop payment" in joey
    assert "never submits the payment to the XRP Ledger" in joey
    assert "Signing in is off-ledger" in panel
    assert "real on-ledger transactions" in panel
    assert "Mainnet" not in panel


def test_wallet_picker_offers_both_providers_behind_one_neutral_prompt():
    """Catch the picker regressing to auto-Xaman copy or unlabeled arms."""
    dom = _registration_dom()
    nodes = dom.nodes

    # One shared, provider-neutral prompt — no per-button "Sign in with X" text.
    sub = " ".join(" ".join(dom.text["register-sub"]).split())
    assert sub == "Select wallet to connect."

    picker = nodes["register-picker"]
    assert any(a.get("id") == "register-panel" for a in picker["ancestors"])

    # Both arms live inside the picker, icon-only with accessible names.
    expected = {
        "register-xaman-btn": (
            "Connect with Xaman",
            "xaman-verification-note",
            "assets/xaman-app-icon.png",
        ),
        "register-wc-btn": (
            "Connect with Joey Wallet",
            "joey-verification-note",
            "assets/joey-app-icon.png",
        ),
    }
    for btn_id, (label, note_id, src) in expected.items():
        btn = nodes[btn_id]
        assert any(a.get("id") == "register-picker" for a in btn["ancestors"])
        assert btn["attrs"].get("aria-label") == label
        assert btn["attrs"].get("aria-describedby") == note_id
        assert not " ".join(dom.text[btn_id]).strip()
        icons = [
            n
            for n in dom.nodes.values()
            if n["tag"] == "img" and any(a.get("id") == btn_id for a in n["ancestors"])
        ]
        assert [i["attrs"].get("src") for i in icons] == [src]

    # The escape hatch back to the picker exists and starts hidden.
    switch = nodes["register-switch-btn"]
    assert "hidden" in switch["attrs"]


def test_wc_qr_overlay_is_branded_and_joey_specific():
    """Catch the Joey pairing overlay losing its QR/deep-link/cancel affordances."""
    dom = _registration_dom()
    nodes = dom.nodes

    overlay = nodes["wc-qr-overlay"]
    assert "hidden" in overlay["attrs"]
    for node_id in ("wc-qr-img", "wc-qr-open-btn", "wc-qr-toggle", "wc-qr-cancel-btn"):
        assert any(a.get("id") == "wc-qr-overlay" for a in nodes[node_id]["ancestors"])
    assert "Joey" in nodes["wc-qr-img"]["attrs"].get("alt", "")
    assert nodes["wc-qr-open-btn"]["attrs"].get("aria-label") == "Open in Joey Wallet"
