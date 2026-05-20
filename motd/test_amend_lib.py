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


if __name__ == "__main__":
    unittest.main()
