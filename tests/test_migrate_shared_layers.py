# Tests for scripts/migrate_shared_layers.py (Task 19).
#
# Pure stdlib script (no lfg_core import), so no env-guard preamble is needed
# here — see tests/test_shared_layers.py for the layer_store union tests this
# migration is downstream of.
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
from migrate_shared_layers import migrate  # noqa: E402


def _make_body_dirs(tmp_path, trait_type="Background"):
    for body in ("ape", "female", "male", "skeleton"):
        d = tmp_path / body / trait_type
        d.mkdir(parents=True)
        (d / "Sunset.png").write_bytes(b"same")
        (d / "City.png").write_bytes(body.encode())  # divergent bytes
    return tmp_path


def test_migrate_moves_identical_and_skips_divergent(tmp_path):
    _make_body_dirs(tmp_path)

    result = migrate(str(tmp_path), ["Background"], dry_run=False)
    assert (tmp_path / "shared" / "Background" / "Sunset.png").exists()
    assert not (tmp_path / "male" / "Background" / "Sunset.png").exists()
    assert (tmp_path / "male" / "Background" / "City.png").exists()  # skipped
    assert ("Background", "City", "divergent") in result["skipped"]
    assert ("Background", "Sunset") in result["moved"]
    # idempotent second run
    assert migrate(str(tmp_path), ["Background"], dry_run=False)["moved"] == []


def test_migrate_ignores_macos_appledouble_dotfiles(tmp_path):
    # Real trees carry macOS "._Foo.png" AppleDouble sidecar junk alongside
    # "Foo.png" (seen live in layers/*/Background); these must never be
    # treated as trait values to migrate.
    _make_body_dirs(tmp_path)
    for body in ("ape", "female", "male", "skeleton"):
        (tmp_path / body / "Background" / "._Sunset.png").write_bytes(b"junk")

    result = migrate(str(tmp_path), ["Background"], dry_run=False)
    assert ("Background", "Sunset") in result["moved"]
    assert all(v != "._Sunset" for _, v in result["moved"])
    assert all(v != "._Sunset" for _, v, _ in result["skipped"])
    # the dotfiles are left alone, untouched, in every body dir
    for body in ("ape", "female", "male", "skeleton"):
        assert (tmp_path / body / "Background" / "._Sunset.png").exists()


def test_migrate_skips_value_missing_from_a_body(tmp_path):
    for body in ("ape", "female", "male", "skeleton"):
        d = tmp_path / body / "Background"
        d.mkdir(parents=True)
    for body in ("ape", "female", "male"):  # skeleton has no Sunset
        (tmp_path / body / "Background" / "Sunset.png").write_bytes(b"same")

    result = migrate(str(tmp_path), ["Background"], dry_run=False)
    assert result["moved"] == []
    assert ("Background", "Sunset", "not-in-all-bodies") in result["skipped"]
    # nothing was touched
    assert (tmp_path / "male" / "Background" / "Sunset.png").exists()


def test_dry_run_touches_nothing(tmp_path):
    _make_body_dirs(tmp_path)

    result = migrate(str(tmp_path), ["Background"], dry_run=True)
    assert ("Background", "Sunset") in result["moved"]
    # dry-run reports what WOULD move but performs no filesystem changes
    assert not (tmp_path / "shared").exists()
    assert (tmp_path / "male" / "Background" / "Sunset.png").exists()
    assert (tmp_path / "ape" / "Background" / "Sunset.png").exists()


def test_seasons_manifest_rewrite_on_execute(tmp_path):
    _make_body_dirs(tmp_path)
    manifest_path = tmp_path / "seasons.json"
    manifest_path.write_text(
        json.dumps(
            {
                "ape/Background/Sunset": 1,
                "female/Background/Sunset": 1,
                "male/Background/Sunset": 1,
                "skeleton/Background/Sunset": 1,
            }
        )
    )

    result = migrate(
        str(tmp_path),
        ["Background"],
        dry_run=False,
        seasons_manifest=str(manifest_path),
    )
    assert result["season_conflicts"] == []
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {"shared/Background/Sunset": 1}


def test_seasons_manifest_disagreement_keeps_minimum_and_reports(tmp_path):
    _make_body_dirs(tmp_path)
    manifest_path = tmp_path / "seasons.json"
    manifest_path.write_text(
        json.dumps(
            {
                "ape/Background/Sunset": 3,
                "female/Background/Sunset": 1,  # earliest premiere
                "male/Background/Sunset": 2,
                "skeleton/Background/Sunset": 3,
            }
        )
    )

    result = migrate(
        str(tmp_path),
        ["Background"],
        dry_run=False,
        seasons_manifest=str(manifest_path),
    )
    assert len(result["season_conflicts"]) == 1
    trait_type, value, per_body = result["season_conflicts"][0]
    assert (trait_type, value) == ("Background", "Sunset")
    assert per_body == {"ape": 3, "female": 1, "male": 2, "skeleton": 3}
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {"shared/Background/Sunset": 1}  # minimum kept


def test_seasons_manifest_missing_value_is_noop(tmp_path):
    _make_body_dirs(tmp_path)
    manifest_path = tmp_path / "seasons.json"
    # Manifest exists but has no entry at all for the moved Sunset value.
    manifest_path.write_text(json.dumps({"ape/Background/Other": 1}))

    result = migrate(
        str(tmp_path),
        ["Background"],
        dry_run=False,
        seasons_manifest=str(manifest_path),
    )
    assert result["season_conflicts"] == []
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {"ape/Background/Other": 1}  # untouched


def test_seasons_manifest_missing_file_is_noop(tmp_path):
    _make_body_dirs(tmp_path)
    manifest_path = tmp_path / "seasons.json"  # never created

    result = migrate(
        str(tmp_path),
        ["Background"],
        dry_run=False,
        seasons_manifest=str(manifest_path),
    )
    assert result["season_conflicts"] == []
    assert not manifest_path.exists()  # no file conjured out of thin air


def test_dry_run_does_not_touch_seasons_manifest(tmp_path):
    _make_body_dirs(tmp_path)
    manifest_path = tmp_path / "seasons.json"
    original = {
        "ape/Background/Sunset": 3,
        "female/Background/Sunset": 1,
        "male/Background/Sunset": 2,
        "skeleton/Background/Sunset": 3,
    }
    manifest_path.write_text(json.dumps(original))
    mtime_before = manifest_path.stat().st_mtime_ns
    text_before = manifest_path.read_text()

    result = migrate(
        str(tmp_path),
        ["Background"],
        dry_run=True,
        seasons_manifest=str(manifest_path),
    )
    assert ("Background", "Sunset") in result["moved"]
    # Dry-run may still report what the conflict/rewrite WOULD be, but must not
    # write to disk.
    assert manifest_path.read_text() == text_before
    assert manifest_path.stat().st_mtime_ns == mtime_before


def test_seasons_manifest_default_path_derives_from_layers_dir(tmp_path):
    _make_body_dirs(tmp_path)
    (tmp_path / "seasons.json").write_text(json.dumps({"ape/Background/Sunset": 5}))

    result = migrate(str(tmp_path), ["Background"], dry_run=False)
    assert result["season_conflicts"] == []
    manifest = json.loads((tmp_path / "seasons.json").read_text())
    assert manifest == {"shared/Background/Sunset": 5}


# ---------------------------------------------------------------------------
# Pre-existing shared/ destination: never blind-overwrite (review finding 1)
# ---------------------------------------------------------------------------


def test_existing_identical_shared_dest_is_not_overwritten_and_bodies_removed(tmp_path):
    # A manual preload, or a prior run that copied but crashed before the
    # removal loop, already left the correct bytes at shared/. The value is
    # still reported as moved (the removals still need to happen) but copy2
    # must never run again.
    _make_body_dirs(tmp_path)
    shared_dir = tmp_path / "shared" / "Background"
    shared_dir.mkdir(parents=True)
    (shared_dir / "Sunset.png").write_bytes(b"same")
    dest_mtime_before = (shared_dir / "Sunset.png").stat().st_mtime_ns

    result = migrate(str(tmp_path), ["Background"], dry_run=False)

    assert ("Background", "Sunset") in result["moved"]
    assert result["skipped"] == [("Background", "City", "divergent")]
    assert (shared_dir / "Sunset.png").stat().st_mtime_ns == dest_mtime_before  # untouched
    for body in ("ape", "female", "male", "skeleton"):
        assert not (tmp_path / body / "Background" / "Sunset.png").exists()


def test_existing_divergent_shared_dest_is_reported_and_never_overwritten(tmp_path):
    _make_body_dirs(tmp_path)
    shared_dir = tmp_path / "shared" / "Background"
    shared_dir.mkdir(parents=True)
    (shared_dir / "Sunset.png").write_bytes(b"totally different bytes")

    result = migrate(str(tmp_path), ["Background"], dry_run=False)

    assert ("Background", "Sunset") not in result["moved"]
    assert ("Background", "Sunset", "shared-conflict") in result["skipped"]
    # The pre-existing shared file is untouched, and so are the 4 body copies.
    assert (shared_dir / "Sunset.png").read_bytes() == b"totally different bytes"
    for body in ("ape", "female", "male", "skeleton"):
        assert (tmp_path / body / "Background" / "Sunset.png").exists()


def test_dry_run_never_touches_existing_shared_dest(tmp_path):
    _make_body_dirs(tmp_path)
    shared_dir = tmp_path / "shared" / "Background"
    shared_dir.mkdir(parents=True)
    (shared_dir / "Sunset.png").write_bytes(b"same")

    result = migrate(str(tmp_path), ["Background"], dry_run=True)

    assert ("Background", "Sunset") in result["moved"]
    for body in ("ape", "female", "male", "skeleton"):
        assert (tmp_path / body / "Background" / "Sunset.png").exists()


# ---------------------------------------------------------------------------
# Partial-crash recovery: shared/ copy already exists but some per-body
# copies weren't removed (review finding 2) — convergent from any partial
# state, including repeated repair runs.
# ---------------------------------------------------------------------------


def _simulate_partial_crash(tmp_path, trait_type="Background", value="Sunset", surviving=("male",)):
    """Shared copy already landed; only `surviving` bodies still have their
    (removed-everywhere-else) copy, as if os.remove crashed partway through."""
    _make_body_dirs(tmp_path, trait_type)
    shared_dir = tmp_path / "shared" / trait_type
    shared_dir.mkdir(parents=True)
    (shared_dir / f"{value}.png").write_bytes(b"same")
    for body in ("ape", "female", "male", "skeleton"):
        p = tmp_path / body / trait_type / f"{value}.png"
        if body not in surviving:
            p.unlink()


def test_partial_crash_repair_removes_surviving_identical_body_copies(tmp_path):
    _simulate_partial_crash(tmp_path, surviving=("male",))

    result = migrate(str(tmp_path), ["Background"], dry_run=False)

    assert ("Background", "Sunset") in result["repaired"]
    assert ("Background", "Sunset") not in result["moved"]
    assert not (tmp_path / "male" / "Background" / "Sunset.png").exists()
    assert (tmp_path / "shared" / "Background" / "Sunset.png").read_bytes() == b"same"
    # Convergent: a second run finds nothing left to repair.
    result2 = migrate(str(tmp_path), ["Background"], dry_run=False)
    assert result2["repaired"] == []
    assert result2["moved"] == []


def test_partial_crash_repair_rewrites_seasons_manifest_if_still_present(tmp_path):
    # The crash happened before _rewrite_seasons ran, so the per-body keys
    # are still in the manifest; the repair run must still collapse them.
    _simulate_partial_crash(tmp_path, surviving=("male", "female"))
    manifest_path = tmp_path / "seasons.json"
    manifest_path.write_text(
        json.dumps(
            {
                "ape/Background/Sunset": 1,
                "female/Background/Sunset": 1,
                "male/Background/Sunset": 1,
                "skeleton/Background/Sunset": 1,
            }
        )
    )

    result = migrate(
        str(tmp_path), ["Background"], dry_run=False, seasons_manifest=str(manifest_path)
    )

    assert ("Background", "Sunset") in result["repaired"]
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {"shared/Background/Sunset": 1}


def test_partial_crash_repair_is_idempotent_when_manifest_already_collapsed(tmp_path):
    # A prior run already repaired the files AND collapsed the manifest
    # (or the value was migrated cleanly to begin with); re-running with a
    # manifest that only has the shared/ key must be a no-op, not an error.
    _simulate_partial_crash(tmp_path, surviving=("male",))
    manifest_path = tmp_path / "seasons.json"
    manifest_path.write_text(json.dumps({"shared/Background/Sunset": 2}))

    result = migrate(
        str(tmp_path), ["Background"], dry_run=False, seasons_manifest=str(manifest_path)
    )

    assert ("Background", "Sunset") in result["repaired"]
    assert result["season_conflicts"] == []
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {"shared/Background/Sunset": 2}  # untouched, already collapsed


def test_partial_crash_with_divergent_survivor_is_shared_conflict(tmp_path):
    # A body copy survived but its bytes don't match the shared copy — this
    # is not a safe repair (the shared copy might be wrong, or the survivor
    # is a genuinely different, not-yet-migrated file). Never delete it.
    _make_body_dirs(tmp_path)
    shared_dir = tmp_path / "shared" / "Background"
    shared_dir.mkdir(parents=True)
    (shared_dir / "Sunset.png").write_bytes(b"same")
    for body in ("ape", "female", "skeleton"):
        (tmp_path / body / "Background" / "Sunset.png").unlink()
    (tmp_path / "male" / "Background" / "Sunset.png").write_bytes(b"different bytes on male")

    result = migrate(str(tmp_path), ["Background"], dry_run=False)

    assert ("Background", "Sunset", "shared-conflict") in result["skipped"]
    assert ("Background", "Sunset") not in result["repaired"]
    assert ("Background", "Sunset") not in result["moved"]
    assert (tmp_path / "male" / "Background" / "Sunset.png").exists()  # never removed


def test_dry_run_does_not_repair_partial_crash_state(tmp_path):
    _simulate_partial_crash(tmp_path, surviving=("male",))

    result = migrate(str(tmp_path), ["Background"], dry_run=True)

    assert ("Background", "Sunset") in result["repaired"]
    assert (tmp_path / "male" / "Background" / "Sunset.png").exists()  # untouched


# ---------------------------------------------------------------------------
# Case-insensitive discovery must use the actual on-disk filename everywhere
# downstream, not a reconstructed `value + ext` guess (review finding).
# ---------------------------------------------------------------------------


def test_uppercase_extension_migrates_preserving_actual_filename(tmp_path):
    # Real trees can carry "Sunset.PNG" instead of "sunset.png". Discovery
    # matches extensions case-insensitively, but on a case-sensitive
    # filesystem a naive `value + ".png"` reconstruction never finds the file
    # again — it must be looked up by its recorded actual name.
    for body in ("ape", "female", "male", "skeleton"):
        d = tmp_path / body / "Background"
        d.mkdir(parents=True)
        (d / "Sunset.PNG").write_bytes(b"same")

    result = migrate(str(tmp_path), ["Background"], dry_run=False)

    assert ("Background", "Sunset") in result["moved"]
    assert (tmp_path / "shared" / "Background" / "Sunset.PNG").exists()  # case preserved
    for body in ("ape", "female", "male", "skeleton"):
        assert not (tmp_path / body / "Background" / "Sunset.PNG").exists()


def test_repair_branch_requires_nonempty_survivor_set(tmp_path):
    # Discovery can record a value for a body (matching listdir + extension)
    # whose path later fails the isfile check (here: a directory squatting on
    # the filename, standing in for any real-world race between discovery and
    # path-reconstruction). That leaves `paths` empty for every body. With a
    # pre-existing shared/ copy, `all(... for p in paths)` over an empty list
    # is vacuously True — the old code reported this as "repaired" and
    # rewrote seasons.json even though nothing was actually verified or
    # removed. It must instead be skipped with a distinct reason and the
    # manifest must be left alone.
    for body in ("ape", "female", "male", "skeleton"):
        d = tmp_path / body / "Background"
        d.mkdir(parents=True)
        (d / "Sunset.png").mkdir()  # not a real file: isfile() is False

    shared_dir = tmp_path / "shared" / "Background"
    shared_dir.mkdir(parents=True)
    (shared_dir / "Sunset.png").write_bytes(b"same")

    manifest_path = tmp_path / "seasons.json"
    manifest = {
        "ape/Background/Sunset": 1,
        "female/Background/Sunset": 1,
        "male/Background/Sunset": 1,
        "skeleton/Background/Sunset": 1,
    }
    manifest_path.write_text(json.dumps(manifest))

    result = migrate(
        str(tmp_path), ["Background"], dry_run=False, seasons_manifest=str(manifest_path)
    )

    assert ("Background", "Sunset") not in result["repaired"]
    assert ("Background", "Sunset") not in result["moved"]
    assert any(
        t == "Background" and v == "Sunset" and why != "repaired" for t, v, why in result["skipped"]
    )
    # Never invented a repair: the bogus per-body directories are untouched
    # and the manifest was never rewritten.
    for body in ("ape", "female", "male", "skeleton"):
        assert (tmp_path / body / "Background" / "Sunset.png").is_dir()
    assert json.loads(manifest_path.read_text()) == manifest
