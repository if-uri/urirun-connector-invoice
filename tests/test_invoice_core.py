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


def test_receipt_draft_route_registered():
    assert "invoice://host/receipt/query/draft" in c.urirun_bindings()["bindings"]


def test_receipt_draft_from_json_derives_net_vat():
    rj = ('{"items": [{"name": "Chleb", "price": 4.99}, {"name": "Mleko", "price": 3.50}],'
          ' "total": 8.49, "currency": "PLN", "date": "2026-06-23", "nip": "1234563218"}')
    r = c.receipt_draft(receipt_json=rj, vat_rate=23)
    assert r["ok"]
    d = r["draft"]
    assert d["gross"] == 8.49 and d["sellerNip"] == "1234563218" and d["currency"] == "PLN"
    # net + vat reconstruct the gross at 23%
    assert d["net"] == 6.90 and d["vat"] == 1.59
    assert round(d["net"] + d["vat"], 2) == d["gross"]
    assert d["issueDate"] == "2026-06-23" and len(d["items"]) == 2
    assert d["ksefReady"] is True


def test_receipt_draft_from_text_reuses_invoice_regex():
    txt = "SKLEP IFURI\nNIP 778-14-22-455\n2026-06-23\nKawa 29,90\nSUMA PLN 29,90"
    r = c.receipt_draft(text=txt, vat_rate=23)
    assert r["ok"] and r["draft"]["sellerNip"] == "7781422455"
    assert r["draft"]["gross"] == 29.90 and r["draft"]["ksefReady"] is True


def test_receipt_draft_without_total_is_not_ok():
    r = c.receipt_draft(receipt_json='{"items": [], "total": null}')
    assert r["ok"] is False and "no total" in " ".join(r["notes"])


def test_ksef_build_route_registered():
    assert "invoice://host/ksef/query/build" in c.urirun_bindings()["bindings"]


def test_ksef_build_roundtrips_through_parser():
    rj = ('{"items": [{"name": "Kawa", "price": 29.90}], "total": 38.39,'
          ' "currency": "PLN", "date": "2026-06-23", "nip": "7781422455"}')
    draft = c.receipt_draft(receipt_json=rj, vat_rate=23, seller="SKLEP IFURI")["draft"]
    r = c.ksef_build(draft_json=__import__("json").dumps(draft), number="FV/7/2026")
    assert r["ok"] and r["formCode"] == "FA" and r["variant"] == "2"
    # the generated XML is FA(2) and re-parses to the same money + seller
    assert 'WariantFormularza' in r["xml"] and _FA2_NS_IN(r["xml"])
    p = r["parsed"]
    assert p["seller"]["nip"] == "7781422455"
    assert p["gross"] == 38.39 and p["net"] == 31.21 and p["vat"] == 7.18
    assert p["number"] == "FV/7/2026" and p["currency"] == "PLN"


def _FA2_NS_IN(xml):
    return "crd.gov.pl/wzor/2023/06/29/12648" in xml


def test_ksef_build_straight_from_receipt_and_writes_file(tmp_path):
    out = str(tmp_path / "fa2.xml")
    r = c.ksef_build(text="SKLEP IFURI\nNIP 778-14-22-455\n2026-06-23\nKawa 29,90\nSUMA PLN 29,90",
                     vat_rate=23, output_path=out)
    assert r["ok"] and r["path"] == out
    import os
    assert os.path.isfile(out)
    # the written file parses back through ksef_parse
    parsed = c.ksef_parse(path=out)
    assert parsed["ok"] and parsed["fields"]["gross"] == 29.90


def test_ksef_build_has_line_items():
    rj = '{"items": [{"name": "Chleb", "price": 4.99}, {"name": "Mleko", "price": 3.50}], "total": 8.49, "nip": "7781422455"}'
    draft = c.receipt_draft(receipt_json=rj, vat_rate=23)["draft"]
    r = c.ksef_build(draft_json=__import__("json").dumps(draft))
    assert r["xml"].count("<FaWiersz>") == 2 and "Chleb" in r["xml"]


def test_ksef_validate_route_registered():
    assert "invoice://host/ksef/query/validate" in c.urirun_bindings()["bindings"]


def test_structural_validate_passes_on_generated_xml():
    rj = '{"items": [{"name": "Kawa", "price": 29.90}], "total": 38.39, "nip": "7781422455"}'
    draft = c.receipt_draft(receipt_json=rj, vat_rate=23, seller="SKLEP IFURI")["draft"]
    xml = c.ksef_build(draft_json=__import__("json").dumps(draft), number="FV/7/2026")["xml"]
    r = c.ksef_validate(xml=xml)
    assert r["ok"] and r["checkedWith"] == "structural"
    assert r["valid"] is True and r["errors"] == []


def test_structural_validate_flags_arithmetic_and_missing():
    bad = ('<Faktura xmlns="http://crd.gov.pl/wzor/2023/06/29/12648/"><Naglowek/>'
           '<Podmiot1><DaneIdentyfikacyjne><NIP>778142245</NIP></DaneIdentyfikacyjne></Podmiot1>'
           '<Fa><KodWaluty>PLN</KodWaluty><P_1>2026-06-23</P_1><P_2>X</P_2>'
           '<P_13_1>100.00</P_13_1><P_14_1>23.00</P_14_1><P_15>200.00</P_15></Fa></Faktura>')
    r = c.ksef_validate(xml=bad)
    assert r["valid"] is False
    joined = " ".join(r["errors"])
    assert "Podmiot2" in joined            # missing buyer
    assert "Adnotacje" in joined           # missing mandatory block
    assert "net+VAT" in joined             # 100+23 != 200


def test_xsd_validation_via_lxml_when_schema_given(tmp_path):
    etree = __import__("importlib").import_module("lxml.etree")  # skip if lxml missing handled below
    # a tiny schema that requires <Faktura><Fa><P_15/></Fa></Faktura> in the FA(2) namespace
    xsd = tmp_path / "mini.xsd"
    xsd.write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:t="http://crd.gov.pl/wzor/2023/06/29/12648/" '
        'targetNamespace="http://crd.gov.pl/wzor/2023/06/29/12648/" elementFormDefault="qualified">'
        '<xs:element name="Faktura"><xs:complexType><xs:sequence>'
        '<xs:element name="Fa"><xs:complexType><xs:sequence>'
        '<xs:element name="P_15" type="xs:string"/>'
        '</xs:sequence></xs:complexType></xs:element>'
        '</xs:sequence></xs:complexType></xs:element></xs:schema>', encoding="utf-8")
    good = ('<Faktura xmlns="http://crd.gov.pl/wzor/2023/06/29/12648/">'
            '<Fa><P_15>38.39</P_15></Fa></Faktura>')
    r = c.ksef_validate(xml=good, xsd_path=str(xsd))
    assert r["checkedWith"] == "xsd" and r["valid"] is True and r["errorCount"] == 0
    # a doc missing P_15 must fail XSD validation
    bad = '<Faktura xmlns="http://crd.gov.pl/wzor/2023/06/29/12648/"><Fa/></Faktura>'
    r2 = c.ksef_validate(xml=bad, xsd_path=str(xsd))
    assert r2["valid"] is False and r2["errorCount"] >= 1


def test_ksef_upo_route_registered():
    assert "invoice://host/ksef/query/upo" in c.urirun_bindings()["bindings"]


def test_parse_upo_json_variant():
    upo = ('{"ok": true, "acquisitionTimestamp": "2026-06-23T10:00:00Z",'
           ' "ksefReferenceNumber": "7781422455-20260623-XYZ123-AB", "referenceNumber": "S-99"}')
    r = c.ksef_upo(text=upo)
    assert r["ok"] and r["format"] == "json"
    assert r["ksefNumber"] == "7781422455-20260623-XYZ123-AB"
    assert r["referenceNumber"] == "S-99" and r["timestamp"] == "2026-06-23T10:00:00Z"


def test_parse_upo_xml_variant():
    upo = ('<Potwierdzenie xmlns="http://crd.gov.pl/wzor/upo/">'
           '<NumerKSeF>7781422455-20260623-AAA111-CD</NumerKSeF>'
           '<DataPrzyjecia>2026-06-23T11:22:33Z</DataPrzyjecia>'
           '<SkrotDokumentu>abc123hash</SkrotDokumentu><NIP>778-14-22-455</NIP></Potwierdzenie>')
    r = c.ksef_upo(xml=upo)
    assert r["ok"] and r["format"] == "xml"
    assert r["ksefNumber"] == "7781422455-20260623-AAA111-CD"
    assert r["timestamp"] == "2026-06-23T11:22:33Z" and r["nip"] == "7781422455"


def test_ksef_upo_saves_raw(tmp_path):
    upo = '{"ksefReferenceNumber": "K-1", "acquisitionTimestamp": "2026-06-23T00:00:00Z"}'
    out = str(tmp_path / "upo.json")
    r = c.ksef_upo(text=upo, output_path=out)
    assert r["ok"] and r["savedTo"] == out
    import os
    assert os.path.isfile(out) and "K-1" in open(out).read()


def test_parse_upo_rejects_garbage():
    assert c.ksef_upo(text="not xml or json")["ok"] is False


def test_ledger_logs_ksef_build_and_upo(tmp_path, monkeypatch):
    import json as _j
    ledger = str(tmp_path / "ledger.jsonl")
    monkeypatch.setenv("URIRUN_LEDGER", ledger)
    rj = '{"items": [{"name": "Kawa", "price": 29.90}], "total": 38.39, "nip": "7781422455"}'
    draft = c.receipt_draft(receipt_json=rj, vat_rate=23, seller="SKLEP")["draft"]
    c.ksef_build(draft_json=_j.dumps(draft), number="FV/9/2026")
    c.ksef_upo(text='{"ksefReferenceNumber":"K-7","acquisitionTimestamp":"2026-06-23T00:00:00Z"}')
    events = [_j.loads(l)["event"] for l in open(ledger, encoding="utf-8") if l.strip()]
    assert "ksef_build" in events and "ksef_upo" in events


def test_ledger_list_reads_and_summarizes(tmp_path):
    led = tmp_path / "ledger.jsonl"
    import json as _j
    led.write_text("\n".join([
        _j.dumps({"ts": 1, "connector": "camera", "event": "receipt", "total": 10.0}),
        _j.dumps({"ts": 2, "connector": "invoice", "event": "ksef_build", "gross": 10.0, "number": "F1"}),
        _j.dumps({"ts": 3, "connector": "invoice", "event": "ksef_upo", "ksefNumber": "K-1"}),
        "garbage-not-json",
    ]) + "\n", encoding="utf-8")
    r = c.ledger_list(path=str(led))
    assert r["ok"] and r["exists"] and r["count"] == 3      # garbage line skipped
    s = r["summary"]
    assert s["receiptsTotal"] == 10.0 and s["invoicesBuilt"] == 1 and s["grossBuilt"] == 10.0
    assert s["ksefConfirmed"] == 1 and s["ksefNumbers"] == ["K-1"]
    assert r["rows"][0]["ts"] == 3                          # most recent first


def test_ledger_list_filters_and_missing(tmp_path):
    led = tmp_path / "l.jsonl"
    import json as _j
    led.write_text(_j.dumps({"ts": 1, "event": "receipt"}) + "\n"
                   + _j.dumps({"ts": 2, "event": "ksef_upo", "ksefNumber": "K"}) + "\n", encoding="utf-8")
    assert c.ledger_list(path=str(led), event="ksef_upo")["count"] == 1
    # missing file → ok, exists False, no crash
    m = c.ledger_list(path=str(tmp_path / "none.jsonl"))
    assert m["ok"] and m["exists"] is False and m["count"] == 0
