# Tests for scripts/import_bithomp_csv.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import import_bithomp_csv as imp  # noqa: E402

from lfg_core import nft_index  # noqa: E402

HEADER = (
    '"NFT ID","Issuer","Taxon","Serial","Name","URI","Owner","Image",'
    '"Attribute Background","Attribute Body","Attribute Clothing","Attribute Mouth",'
    '"Attribute Eyebrows","Attribute Eyes","Attribute Head","Attribute Accessory"'
)
ROW_MALE = (
    '"00ABC","rLfgo","1760","1","Let\'s Effing Go! #2003",'
    '"https://lfgo.b-cdn.net/LFGO/2003/2003_34.json","rOwner1",'
    '"https://lfgo.b-cdn.net/LFGO/2003/2003_34.png",'
    '"Pastel Aqua","Straight Wood","Hoodie GO Punk","Determined","Questioning","Hypno",'
    '"Egg Head","None"'
)


def _write_csv(path, *rows, header=HEADER):
    # utf-8-sig so the importer's BOM handling is exercised.
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(r + "\n")


def test_csv_record_maps_fields_and_body():
    row = {
        "NFT ID": "00ABC",
        "Name": "Let's Effing Go! #2003",
        "Owner": "rOwner1",
        "URI": "https://x/2003.json",
        "Image": "https://x/2003.png",
        "Attribute Body": "Straight Wood",
        "Attribute Clothing": "Hoodie GO Punk",
        "Attribute Head": "Egg Head",
        "Attribute Accessory": "None",
    }
    rec = imp.csv_record(row)
    assert rec.nft_id == "00ABC"
    assert rec.nft_number == 2003
    assert rec.owner == "rOwner1"
    assert rec.body == "male"  # Straight -> male
    assert rec.mutable is None  # unknown from CSV
    assert rec.is_burned is False
    vals = {a["trait_type"]: a["value"] for a in rec.attributes}
    assert vals["Clothing"] == "Hoodie GO Punk"
    assert vals["Head"] == "Egg Head"  # 'Attribute Head' -> Head (layer name)
    # uri stored as hex of the URI string
    assert bytes.fromhex(rec.uri_hex).decode() == "https://x/2003.json"


def test_burned_column_is_detected():
    row = {
        "NFT ID": "00X",
        "Name": "#5",
        "Owner": "",
        "Burned": "true",
        "Attribute Body": "Curved Green",
    }
    assert imp.csv_record(row).is_burned is True
    row2 = {"NFT ID": "00Y", "Name": "#6", "Status": "burned", "Attribute Body": "Ape X"}
    assert imp.csv_record(row2).is_burned is True


def test_import_csv_upserts(tmp_path):
    csv_path = tmp_path / "data.csv"
    _write_csv(str(csv_path), ROW_MALE)
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    counts = imp.import_csv(conn, str(csv_path))
    assert counts["imported"] == 1
    row = conn.execute(
        "SELECT nft_number, body, owner FROM onchain_nfts WHERE nft_id='00ABC'"
    ).fetchone()
    assert row == (2003, "male", "rOwner1")
    # idempotent
    imp.import_csv(conn, str(csv_path))
    assert conn.execute("SELECT COUNT(*) FROM onchain_nfts").fetchone()[0] == 1
