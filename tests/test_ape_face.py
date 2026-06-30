# tests/test_ape_face.py
from lfg_core import ape_face


def test_should_mask_normal_face_feature_on_melt_body():
    assert ape_face.should_mask("Eyes", "Creepy", "Ape Melting XRay") is True
    assert ape_face.should_mask("Mouth", "Serious", "Ape Xray") is True
    assert ape_face.should_mask("Eyebrows", "Flat", "Ape Melting") is True


def test_should_mask_exempts_top_effect():
    assert ape_face.should_mask("Mouth", "Rainbow Puke", "Ape Xray") is False
    assert ape_face.should_mask("Eyes", "Laser Eyes", "Ape Xray") is False


def test_should_mask_only_melt_bodies():
    assert ape_face.should_mask("Eyes", "Creepy", "Ape Gold") is False
    assert ape_face.should_mask("Eyes", "Creepy", "Straight Dark") is False


def test_should_mask_only_face_trait_types():
    assert ape_face.should_mask("Clothing", "Wonder", "Ape Xray") is False
    assert ape_face.should_mask("Background", "Wave", "Ape Xray") is False


def test_should_mask_respects_skip_list(monkeypatch):
    monkeypatch.setattr(ape_face, "NO_MASK_VALUES", [{"trait_type": "Mouth", "value": "Cigar"}])
    assert ape_face.should_mask("Mouth", "Cigar", "Ape Xray") is False
    assert ape_face.should_mask("Mouth", "Smile", "Ape Xray") is True
