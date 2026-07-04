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
