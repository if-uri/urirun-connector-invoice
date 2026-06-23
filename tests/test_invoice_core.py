"""Offline tests for the invoice connector: regex extraction, amount sanity, FA_VAT XML, register dedup."""
import urirun_connector_invoice.core as c


def test_bindings_valid():
    b = c.urirun_bindings()
    uris = set(b["bindings"])
    assert "invoice://host/file/query/parse" in uris
    assert "invoice://host/ksef/query/parse" in uris
    for spec in b["bindings"].values():
        assert spec["python"]["module"].endswith("core")
        assert spec["uri"].startswith("invoice://")


def test_regex_parse_full_pl_faktura():
    txt = ("FAKTURA VAT nr FV 7/2026\nData wystawienia: 2026-05-13\n"
           "NIP: 778-14-22-455\nNetto: 1 000,00\nVAT 23%: 230,00\nRazem do zapłaty: 1 230,00 PLN\n")
    r = c.parse(text=txt)
    f = r["fields"]
    assert r["ok"] and r["method"] == "regex"
    assert f["nip"] == "7781422455"          # normalised to 10 digits
    assert f["number"] == "7/2026"
    assert f["issueDate"] == "2026-05-13"
    assert f["net"] == 1000.0 and f["vat"] == 230.0 and f["gross"] == 1230.0
    assert f["currency"] == "PLN"


def test_vat_derived_from_net_and_gross():
    r = c.parse(text="Wartość netto 500,00\nDo zapłaty 615,00 zł\nNIP 1132871234")
    f = r["fields"]
    assert f["net"] == 500.0 and f["gross"] == 615.0
    assert f["vat"] == 115.0                  # gross - net


def test_amount_sanity_cap_rejects_garbage():
    # a NIP-like / huge number must not be parsed as an amount (would poison totals)
    assert c._amount("49877905955.87") is None
    assert c._amount("100,00") == 100.0


def test_number_cleaned_of_hash_tail():
    assert c._clean_number("5511402765/13-03-2026/6GGicx2EhEMaEYx0UXV") is not None
    assert len(c._clean_number("5511402765/13-03-2026/6GGicx2EhEMaEYx0UXVDhLxLhc1uDCzuPViWQ23")) <= 40


FA3_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/">
  <Naglowek><KodFormularza>FA</KodFormularza><WariantFormularza>3</WariantFormularza></Naglowek>
  <Podmiot1><DaneIdentyfikacyjne><NIP>5213584915</NIP><Nazwa>premium.pl Sp. z o.o.</Nazwa></DaneIdentyfikacyjne></Podmiot1>
  <Podmiot2><DaneIdentyfikacyjne><NIP>5881918662</NIP><Nazwa>Tomasz Sapletta</Nazwa></DaneIdentyfikacyjne></Podmiot2>
  <Fa><KodWaluty>PLN</KodWaluty><P_1>2026-05-31</P_1><P_2>PPL2026050000639</P_2>
      <P_13_1>313.00</P_13_1><P_14_1>71.99</P_14_1><P_15>384.99</P_15></Fa>
</Faktura>"""


def test_ksef_fa_vat_parse():
    r = c.ksef_parse(xml=FA3_XML)
    f = r["fields"]
    assert r["ok"]
    assert f["seller"]["nip"] == "5213584915" and f["seller"]["name"] == "premium.pl Sp. z o.o."
    assert f["buyer"]["nip"] == "5881918662"
    assert f["number"] == "PPL2026050000639" and f["issueDate"] == "2026-05-31"
    assert f["net"] == 313.0 and f["vat"] == 71.99 and f["gross"] == 384.99
    assert f["byRate"]["23"] == {"net": 313.0, "vat": 71.99}


def test_ksef_register_dedups_invoice(tmp_path):
    # two file copies of the SAME invoice → counted once in the VAT register
    (tmp_path / "a.xml").write_text(FA3_XML, encoding="utf-8")
    (tmp_path / "b.xml").write_text(FA3_XML, encoding="utf-8")
    r = c.ksef_register(root=str(tmp_path))
    assert r["ok"] and r["invoices"] == 1
    assert r["totals"] == {"net": 313.0, "vat": 71.99, "gross": 384.99}
    assert r["byRate"]["23"] == {"net": 313.0, "vat": 71.99}


def test_ksef_parse_rejects_non_xml():
    assert c.ksef_parse(xml="not xml at all")["ok"] is False
