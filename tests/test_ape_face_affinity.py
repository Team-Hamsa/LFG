# Apes gained their own face art (Eyes/Eyebrows/Mouth) and the placeholder
# None layers were removed, so ape mints must now draw a REAL face trait. That
# only works if trait_config's affinity allows apes those values — historically
# it excluded ape from every face value (built when apes were faceless), which
# left ape Eyebrows with zero legal values (mint would raise). These tests pin
# the affinity so apes can mint faces and the other bodies are unaffected.

from lfg_core import trait_config

# A representative sample from each ape face folder (values that previously
# carried an affinity entry excluding ape).
_APE_FACE_SAMPLES = {
    "Eyes": ["3D", "Aviators", "Chucky", "Nerd"],
    "Eyebrows": ["Angry", "Flat", "Sad", "Surprised"],
    "Mouth": ["Anchor", "Angry", "Determined", "Frown"],
}


def test_ape_is_allowed_its_face_values():
    cfg = trait_config.get_config()
    for trait_type, values in _APE_FACE_SAMPLES.items():
        for value in values:
            assert cfg.value_allowed("ape", trait_type, value), (
                f"ape should be allowed {trait_type}={value}"
            )


def test_ape_eyebrows_is_not_universally_empty():
    # The specific regression: with None deleted, if affinity excludes ape from
    # every Eyebrows value, select_random_attributes raises on ape mint.
    cfg = trait_config.get_config()
    assert cfg.value_allowed("ape", "Eyebrows", "Flat")


def test_other_bodies_still_allowed_for_shared_face_values():
    # Adding ape must not remove male/female (regression guard).
    cfg = trait_config.get_config()
    assert cfg.value_allowed("male", "Eyes", "3D")
    assert cfg.value_allowed("female", "Eyes", "3D")


def test_skeleton_still_excluded_from_face_values():
    # Skeletons keep their built-in faces; affinity must still exclude them
    # from ape/male/female face art (their own folders are None-only anyway).
    cfg = trait_config.get_config()
    assert not cfg.value_allowed("skeleton", "Eyes", "3D")
