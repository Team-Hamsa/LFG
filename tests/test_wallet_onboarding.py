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
