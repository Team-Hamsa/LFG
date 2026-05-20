#!/usr/bin/env python3
"""Tests for amend_lib.py"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amend_lib

SAMPLE_MACRO = """
XRPL_FEATURE(MultiSignReserve, Supported::yes, VoteBehavior::DefaultYes)
XRPL_FIX(fix1781, Supported::yes, VoteBehavior::DefaultNo)
XRPL_FEATURE(Clawback, Supported::yes, VoteBehavior::Obsolete)
XRPL_FIX(fixNFTokenRemint, Supported::yes, VoteBehavior::DefaultNo)
"""

SAMPLE_CFG = """
[server]
port_rpc_admin_local

[amendments]
AABBCC001122
DDEEFF334455

[veto_amendments]
FFEE99887766
"""


class TestParseVoteDefaults(unittest.TestCase):
    def test_parses_defaultyes(self):
        result = amend_lib.parse_vote_defaults(SAMPLE_MACRO)
        self.assertEqual(result["MultiSignReserve"], "yes")

    def test_parses_defaultno(self):
        result = amend_lib.parse_vote_defaults(SAMPLE_MACRO)
        self.assertEqual(result["fix1781"], "no")

    def test_excludes_obsolete(self):
        result = amend_lib.parse_vote_defaults(SAMPLE_MACRO)
        self.assertNotIn("Clawback", result)


class TestParseObsoleteFeatures(unittest.TestCase):
    def test_finds_obsolete(self):
        result = amend_lib.parse_obsolete_features(SAMPLE_MACRO)
        self.assertIn("Clawback", result)

    def test_excludes_active(self):
        result = amend_lib.parse_obsolete_features(SAMPLE_MACRO)
        self.assertNotIn("MultiSignReserve", result)


class TestParseCfgOverrides(unittest.TestCase):
    def test_parses_amendments_yes(self):
        result = amend_lib.parse_cfg_overrides(SAMPLE_CFG)
        self.assertEqual(result["AABBCC001122"], "yes")
        self.assertEqual(result["DDEEFF334455"], "yes")

    def test_parses_veto_no(self):
        result = amend_lib.parse_cfg_overrides(SAMPLE_CFG)
        self.assertEqual(result["FFEE99887766"], "no")

    def test_empty_cfg(self):
        result = amend_lib.parse_cfg_overrides("")
        self.assertEqual(result, {})


class TestComputeWorkingSet(unittest.TestCase):
    def _features(self):
        return {
            "HASH_YES_OVERRIDE": {"name": "fix1781", "supported": True},
            "HASH_MATCHES_DEFAULT": {"name": "MultiSignReserve", "supported": True},
            "HASH_ENABLED": {"name": "fixNFTokenRemint", "enabled": True, "supported": True},
        }

    def test_includes_differing_vote(self):
        # fix1781 default is "no"; cfg override says "yes" → should appear
        features = self._features()
        vote_defaults = {"fix1781": "no", "MultiSignReserve": "yes", "fixNFTokenRemint": "no"}
        obsolete = set()
        overrides = {"HASH_YES_OVERRIDE": "yes"}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        names = [a["name"] for a in result]
        self.assertIn("fix1781", names)

    def test_excludes_matching_default(self):
        # MultiSignReserve default "yes", no override → matches default → excluded
        features = self._features()
        vote_defaults = {"fix1781": "no", "MultiSignReserve": "yes"}
        obsolete = set()
        overrides = {}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        names = [a["name"] for a in result]
        self.assertNotIn("MultiSignReserve", names)

    def test_excludes_enabled(self):
        features = self._features()
        vote_defaults = {"fixNFTokenRemint": "no"}
        obsolete = set()
        overrides = {"HASH_ENABLED": "yes"}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        names = [a["name"] for a in result]
        self.assertNotIn("fixNFTokenRemint", names)

    def test_excludes_obsolete(self):
        features = {"HASH_OBS": {"name": "Clawback", "supported": True}}
        vote_defaults = {"Clawback": "no"}
        obsolete = {"Clawback"}
        overrides = {"HASH_OBS": "yes"}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        self.assertEqual(result, [])


class TestUpdateCfgText(unittest.TestCase):
    def test_adds_to_amendments_section(self):
        cfg = "[amendments]\nexistinghash\n\n[server]\nport 6006\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "yes", "no")
        lines = result.splitlines()
        amend_idx = lines.index("[amendments]")
        self.assertIn("newhash", lines[amend_idx + 1 : amend_idx + 3])

    def test_adds_to_veto_section(self):
        cfg = "[veto_amendments]\nexistinghash\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "no", "yes")
        self.assertIn("newhash", result)
        self.assertIn("[veto_amendments]", result)

    def test_removes_from_opposite_section(self):
        cfg = "[amendments]\nnewhash\n\n[veto_amendments]\nother\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "no", "yes")
        sections = result.split("[amendments]")
        if len(sections) > 1:
            self.assertNotIn("newhash", sections[1].split("[")[0])

    def test_removes_when_vote_matches_default(self):
        cfg = "[amendments]\nnewhash\nextra\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "yes", "yes")
        lines = [l.strip() for l in result.splitlines()]
        self.assertNotIn("newhash", lines)

    def test_creates_section_when_absent(self):
        cfg = "[server]\nport 6006\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "yes", "no")
        self.assertIn("[amendments]", result)
        self.assertIn("newhash", result)


class TestSessionSaveLoad(unittest.TestCase):
    def test_roundtrip(self):
        amendments = [
            {"hash": "ABC", "name": "TestAmend", "your_vote": "no",
             "default_vote": "yes", "majority": False, "supported": True, "description": ""},
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            amend_lib.save_session(amendments, path=path)
            loaded = amend_lib.load_session(path=path)
            self.assertEqual(loaded[0]["hash"], "ABC")
            self.assertEqual(loaded[0]["vote"], "no")
        finally:
            os.unlink(path)


class TestAmendmentDescriptionParser(unittest.TestCase):
    SAMPLE_HTML = """
    <html><body>
    <h2 id="multisignreserve">MultiSignReserve</h2>
    <p>Reduces the reserve for multi-signing from 5 XRP to 1 XRP per signer.</p>
    <p>Second paragraph, should be ignored.</p>
    <h2 id="fix1781">fix1781</h2>
    <p>Fixes an edge case in payment path finding.</p>
    </body></html>
    """

    def test_parses_first_paragraph(self):
        parser = amend_lib._AmendmentDescriptionParser()
        parser.feed(self.SAMPLE_HTML)
        desc = parser.get_descriptions()
        self.assertEqual(desc["MultiSignReserve"],
                         "Reduces the reserve for multi-signing from 5 XRP to 1 XRP per signer.")

    def test_parses_multiple_amendments(self):
        parser = amend_lib._AmendmentDescriptionParser()
        parser.feed(self.SAMPLE_HTML)
        desc = parser.get_descriptions()
        self.assertIn("fix1781", desc)

    def test_only_first_paragraph_per_amendment(self):
        parser = amend_lib._AmendmentDescriptionParser()
        parser.feed(self.SAMPLE_HTML)
        desc = parser.get_descriptions()
        self.assertNotIn("Second paragraph", desc.get("MultiSignReserve", ""))


if __name__ == "__main__":
    unittest.main()
