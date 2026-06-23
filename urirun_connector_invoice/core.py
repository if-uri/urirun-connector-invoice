# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
# invoice:// connector — structured field extraction from invoices (Polish "faktura"
# first), turning a PDF/text into {nip, number, dates, net/vat/gross, seller, currency}
# over a URI instead of eyeballing PDFs. Text comes from pdftotext (reused from the doc
# flow); fields come from robust regex tuned for PL invoices, with an OPTIONAL LLM pass
# (OpenRouter via litellm) for layouts the regex misses. A folder route batches a whole
# month of invoices into a structured CSV/JSON for accounting — net/VAT/gross totals.

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

import urirun

CONNECTOR_ID = "invoice"
INVOICE = urirun.connector(CONNECTOR_ID, scheme="invoice", target="host", meta={"label": "Invoice field extraction"})


def _ledger(event: str, **fields: Any) -> None:
    """Best-effort append of one transaction line to the shared ledger (env URIRUN_LEDGER,
    default ~/.urirun/ledger.jsonl; 0/off disables). Never raises; logs no secrets."""
    import time
    path = os.getenv("URIRUN_LEDGER", os.path.expanduser("~/.urirun/ledger.jsonl"))
    if path.lower() in ("0", "off", "none", ""):
        return
    try:
        rec = {"ts": time.time(), "connector": CONNECTOR_ID, "event": event,
               "live": False, **fields}  # ledger only holds frozen artifacts, never widgets
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - telemetry must never break a route
        pass

# --- Polish-invoice patterns (also catch common EN labels) -----------------------------
# inner class excludes newline so a number on the *next* line can't bleed into the NIP
_NIP_RE = re.compile(r"NIP[:\s]*([A-Z]{0,2}[ \t]?\d[\d \t\-]{8,13}\d)", re.I)
_NUM_RE = re.compile(r"(?:faktura(?:\s+(?:nr|vat|nr\.?))?|invoice(?:\s+no)?|nr\s+faktury|FV|FS|FVS)[:\s/]*"
                     r"([A-Z]{0,4}[\w./\-]*\d[\w./\-]*)", re.I)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})")
_ISSUE_RE = re.compile(r"(?:data\s+wystawienia|wystawiono|date\s+of\s+issue|issue\s+date)[:\s]*"
                       r"(\d{4}-\d{2}-\d{2}|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})", re.I)
_SELLER_RE = re.compile(r"(?:sprzedawca|seller|sprzedaj[ąa]cy)[:\s]*\n?\s*(.+)", re.I)
# a money amount needs a 2-digit minor part (",00" / ".00") — skips years, percents, NIPs
_AMOUNT = r"([0-9][0-9\s .]*[.,][0-9]{2})\b"
# the currency token may sit between the label and the amount ("SUMA PLN 29,90", "Razem PLN ...")
_GROSS_RE = re.compile(r"(?:do\s+zap[łl]aty|razem\s+do\s+zap[łl]aty|brutto|gross|total|suma|razem)[:\s]*"
                       r"(?:PLN|EUR|USD|GBP|z[łl])?\s*" + _AMOUNT, re.I)
_NET_RE = re.compile(r"(?:netto|net)\s*[:\s]*" + _AMOUNT, re.I)
_VAT_RE = re.compile(r"(?:VAT|podatek)(?:\s*\d{1,2}\s*%)?[:\s]*" + _AMOUNT, re.I)
_CCY_RE = re.compile(r"\b(PLN|EUR|USD|GBP|z[łl])\b", re.I)


def _pdf_text(path: str) -> str:
    if path.lower().endswith(".pdf") and shutil.which("pdftotext"):
        try:
            r = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return r.stdout
        except Exception:  # noqa: BLE001
            return ""
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except Exception:  # noqa: BLE001
        return ""


def _amount(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip().replace(" ", "")
    # 1.234,56 (PL) or 1,234.56 (EN) → normalise to float
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        val = round(float(re.sub(r"[^\d.]", "", s)), 2)
    except ValueError:
        return None
    # a single invoice line above ~100M PLN is almost always a misparse (NIP, account no,
    # hash) — drop it so it can't poison the audit totals.
    return val if 0 <= val < 100_000_000 else None


def _first(rx: re.Pattern, text: str) -> str | None:
    m = rx.search(text)
    return m.group(1).strip() if m else None


def _clean_number(num: str | None) -> str | None:
    """An invoice number is short; trim hashes/tokens the greedy capture dragged in."""
    if not num:
        return None
    num = num.strip().rstrip(".,;:")
    return num[:40] if len(num) <= 40 else num.split("/")[0][:40]


def _norm_nip(nip: str | None) -> str | None:
    if not nip:
        return None
    digits = re.sub(r"\D", "", nip)
    return digits[-10:] if len(digits) >= 10 else nip.strip()


def _regex_fields(text: str) -> dict[str, Any]:
    issue = _first(_ISSUE_RE, text) or (_DATE_RE.search(text).group(1) if _DATE_RE.search(text) else None)
    seller = _first(_SELLER_RE, text)
    net, vat, gross = _amount(_first(_NET_RE, text)), _amount(_first(_VAT_RE, text)), _amount(_first(_GROSS_RE, text))
    if vat is None and net is not None and gross is not None:
        vat = round(gross - net, 2)  # derive VAT when only net+gross are labelled
    return {
        "nip": _norm_nip(_first(_NIP_RE, text)),
        "number": _clean_number(_first(_NUM_RE, text)),
        "issueDate": issue,
        "seller": (seller[:80] if seller else None),
        "net": net, "vat": vat, "gross": gross,
        "currency": (_first(_CCY_RE, text) or "").upper().replace("ZŁ", "PLN").replace("ZL", "PLN") or None,
    }


def _llm_fields(text: str, model: str) -> dict[str, Any] | None:
    """Optional LLM pass (OpenRouter via litellm) for invoices the regex can't crack."""
    try:
        import json as _json
        import litellm  # type: ignore
        prompt = ("Extract invoice fields as compact JSON with keys "
                  "nip, number, issueDate (YYYY-MM-DD), seller, net, vat, gross, currency. "
                  "Use null when absent. Invoice text:\n\n" + text[:6000])
        resp = litellm.completion(model=model, messages=[{"role": "user", "content": prompt}],
                                  temperature=0, max_tokens=300)
        out = resp["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", out, re.S)
        return _json.loads(m.group(0)) if m else None
    except Exception:  # noqa: BLE001
        return None


@INVOICE.handler("file/query/parse", isolated=True,
                 meta={"label": "Extract structured fields from one invoice", "cliAlias": "parse"})
def parse(path: str = "", text: str = "", use_llm: bool = False,
          model: str = "openrouter/google/gemini-3.1-flash-image-preview") -> dict[str, Any]:
    """Structured fields of an invoice at `path` (or raw `text`): nip, number, issueDate,
    seller, net, vat, gross, currency. Regex-first (text-layer PDFs via pdftotext); set
    use_llm=true to fall back to an LLM (OpenRouter) when regex leaves key fields empty."""
    path = os.path.expanduser(path) if path else ""
    body = text or (_pdf_text(path) if path else "")
    if not body.strip():
        return {"ok": False, "error": "no text (scan without OCR? run doc:// OCR first)", "path": path}
    fields = _regex_fields(body)
    method = "regex"
    if use_llm and not (fields["nip"] and fields["gross"]):
        llm = _llm_fields(body, model)
        if llm:
            for k, v in llm.items():
                if v not in (None, "") and not fields.get(k):
                    fields[k] = v
            method = "regex+llm"
    fields["nip"] = _norm_nip(fields.get("nip"))
    return {"ok": bool(fields.get("nip") or fields.get("number") or fields.get("gross")),
            "connector": CONNECTOR_ID, "kind": "invoice", "live": False, "path": path, "method": method, "fields": fields}


@INVOICE.handler("folder/query/audit", isolated=True,
                 meta={"label": "Parse every invoice in a folder → structured CSV/JSON", "cliAlias": "audit"})
def audit(root: str = "", extensions: str = "pdf", recursive: bool = True, use_llm: bool = False,
          max_files: int = 2000, output_csv: str = "", model: str = "openrouter/google/gemini-3.1-flash-image-preview") -> dict[str, Any]:
    """Parse all invoices under `root` into structured rows + net/VAT/gross totals. Writes a
    CSV to `output_csv` if given. Read-only over the invoice files."""
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return {"ok": False, "error": f"not a directory: {root}"}
    exts = {("." + e.strip().lstrip(".")).lower() for e in extensions.split(",") if e.strip()}
    rows, totals = [], {"net": 0.0, "vat": 0.0, "gross": 0.0}
    walker = os.walk(root) if recursive else [(root, [], os.listdir(root))]
    for dirpath, _d, files in walker:
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            p = os.path.join(dirpath, fn)
            res = parse(path=p, use_llm=use_llm, model=model)
            f = res.get("fields") or {}
            row = {"file": fn, "ok": res.get("ok"), "method": res.get("method"), **f}
            rows.append(row)
            for k in totals:
                if isinstance(f.get(k), (int, float)):
                    totals[k] += f[k]
            if len(rows) >= max_files:
                break
    cols = ["file", "ok", "method", "nip", "number", "issueDate", "seller", "net", "vat", "gross", "currency"]
    csv_written = None
    if output_csv:
        output_csv = os.path.expanduser(output_csv)
        with open(output_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        csv_written = output_csv
    buf = io.StringIO()
    csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore").writeheader()
    return {"ok": True, "connector": CONNECTOR_ID, "kind": "audit", "live": False, "root": root, "invoices": len(rows),
            "parsed": sum(1 for r in rows if r.get("ok")),
            "totals": {k: round(v, 2) for k, v in totals.items()},
            "csv": csv_written, "rows": rows[:300]}


# --- KSeF FA_VAT XML (the ksef:// API connector fetches these; here we parse them locally) ---
# KSeF e-invoices are structured XML (FA(1)/FA(2)/FA(3)), so fields are EXACT — no regex/OCR.
import xml.etree.ElementTree as _ET

_RATE = {"1": "23", "2": "8", "3": "5", "4": "ryczalt", "5": "0_wdt", "6": "0_kraj", "7": "zw"}


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sub(root, name: str):
    for el in root.iter():
        if _ln(el.tag) == name:
            return el
    return None


def _txt(node, name: str) -> str | None:
    if node is None:
        return None
    for el in node.iter():
        if _ln(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return None


def _xnum(s: str | None) -> float | None:
    try:
        return round(float(s.replace(" ", "").replace(",", ".")), 2) if s else None
    except ValueError:
        return None


def _party(node) -> dict[str, Any]:
    ident = _sub(node, "DaneIdentyfikacyjne") if node is not None else None
    return {"nip": _txt(ident, "NIP"), "name": _txt(ident, "Nazwa") or _txt(ident, "PelnaNazwa")}


def _parse_fa_vat(data) -> dict[str, Any]:
    root = _ET.fromstring(data)
    fa = _sub(root, "Fa")
    if fa is None:
        fa = root
    by_rate: dict[str, dict] = {}
    for el in fa.iter():
        ln = _ln(el.tag)
        if ln.startswith("P_13_") and el.text:
            by_rate.setdefault(_RATE.get(ln[5:], ln[5:]), {})["net"] = _xnum(el.text)
        elif ln.startswith("P_14_") and el.text:
            by_rate.setdefault(_RATE.get(ln[5:], ln[5:]), {})["vat"] = _xnum(el.text)
    net = round(sum(v.get("net") or 0 for v in by_rate.values()), 2) or _xnum(_txt(fa, "P_13_1"))
    vat = round(sum(v.get("vat") or 0 for v in by_rate.values()), 2) or _xnum(_txt(fa, "P_14_1"))
    return {"formCode": _txt(root, "KodFormularza"), "variant": _txt(root, "WariantFormularza"),
            "seller": _party(_sub(root, "Podmiot1")), "buyer": _party(_sub(root, "Podmiot2")),
            "number": _txt(fa, "P_2") or _txt(fa, "P_2A"), "issueDate": _txt(fa, "P_1"),
            "saleDate": _txt(fa, "P_6"), "currency": _txt(fa, "KodWaluty"),
            "net": net, "vat": vat, "gross": _xnum(_txt(fa, "P_15")), "byRate": by_rate}


@INVOICE.handler("ksef/query/parse", isolated=True,
                 meta={"label": "Parse one KSeF FA_VAT XML into structured fields", "cliAlias": "ksef-parse"})
def ksef_parse(path: str = "", xml: str = "") -> dict[str, Any]:
    """Exact fields of a KSeF FA_VAT XML (at `path`, or raw `xml`): seller/buyer NIP+name,
    number, issue/sale dates, net/VAT/gross, byRate breakdown, currency. Structured → no OCR."""
    data = xml
    if not data and path:
        path = os.path.expanduser(path)
        try:
            data = open(path, "rb").read()
        except OSError as exc:
            return {"ok": False, "error": str(exc), "path": path}
    if not data:
        return {"ok": False, "error": "provide path or xml"}
    try:
        fields = _parse_fa_vat(data)
    except _ET.ParseError as exc:
        return {"ok": False, "error": f"not valid XML: {exc}", "path": path}
    return {"ok": bool(fields.get("number") or (fields.get("seller") or {}).get("nip")),
            "connector": CONNECTOR_ID, "kind": "invoice", "live": False, "path": path, "fields": fields}


@INVOICE.handler("ksef/folder/register", isolated=True,
                 meta={"label": "Aggregate KSeF XMLs into a VAT register (per-rate totals → JPK)", "cliAlias": "ksef-register"})
def ksef_register(root: str = "", recursive: bool = True, output_csv: str = "", max_files: int = 5000) -> dict[str, Any]:
    """Parse every *.xml KSeF invoice under `root` into a VAT register: rows + net/VAT/gross
    totals broken down per VAT rate — the ewidencja a JPK_V7M is built from. Writes CSV if
    `output_csv` is given. Read-only over the invoice files."""
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return {"ok": False, "error": f"not a directory: {root}"}
    rows, totals, by_rate, seen = [], {"net": 0.0, "vat": 0.0, "gross": 0.0}, {}, set()
    walker = os.walk(root) if recursive else [(root, [], os.listdir(root))]
    for dirpath, _d, files in walker:
        for fn in sorted(files):
            if not fn.lower().endswith(".xml"):
                continue
            res = ksef_parse(path=os.path.join(dirpath, fn))
            if not res.get("ok"):
                continue
            f = res["fields"]
            key = (f.get("number"), (f.get("seller") or {}).get("nip"), f.get("gross"))
            if key in seen:  # a VAT register counts each invoice once, not each file copy
                continue
            seen.add(key)
            rows.append({"file": fn, "number": f.get("number"), "issueDate": f.get("issueDate"),
                         "sellerNip": (f.get("seller") or {}).get("nip"),
                         "sellerName": (f.get("seller") or {}).get("name"),
                         "buyerNip": (f.get("buyer") or {}).get("nip"),
                         "net": f.get("net"), "vat": f.get("vat"), "gross": f.get("gross"),
                         "currency": f.get("currency")})
            for k in totals:
                if isinstance(f.get(k), (int, float)):
                    totals[k] += f[k]
            for rate, amt in (f.get("byRate") or {}).items():
                slot = by_rate.setdefault(rate, {"net": 0.0, "vat": 0.0})
                slot["net"] += amt.get("net") or 0
                slot["vat"] += amt.get("vat") or 0
            if len(rows) >= max_files:
                break
    cols = ["file", "number", "issueDate", "sellerNip", "sellerName", "buyerNip", "net", "vat", "gross", "currency"]
    csv_written = None
    if output_csv and rows:
        output_csv = os.path.expanduser(output_csv)
        with open(output_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        csv_written = output_csv
    return {"ok": True, "connector": CONNECTOR_ID, "kind": "register", "live": False, "root": root, "invoices": len(rows),
            "totals": {k: round(v, 2) for k, v in totals.items()},
            "byRate": {r: {k: round(x, 2) for k, x in v.items()} for r, v in by_rate.items()},
            "csv": csv_written, "rows": rows[:300]}


@INVOICE.handler("receipt/query/draft", isolated=True,
                 meta={"label": "Build an invoice draft from a receipt (paragon)", "cliAlias": "receipt-draft"})
def receipt_draft(text: str = "", receipt_json: str = "", path: str = "", vat_rate: float = 23.0,
                  seller: str = "", buyer_nip: str = "", number: str = "", currency: str = "",
                  use_llm: bool = False,
                  model: str = "openrouter/google/gemini-3.1-flash-image-preview") -> dict[str, Any]:
    """Turn a receipt ('paragon') into an invoice draft — the bridge from
    camera://host/receipt/query/parse into the invoice/KSeF flow. Accepts the camera
    parser's JSON (`receipt_json` with items/total/nip/date/currency), raw OCR `text`, or a
    `path`. Reuses the invoice field extractor for nip/number/dates/seller/gross, derives
    net+VAT from gross at `vat_rate` (default 23%), and returns a KSeF-ready draft."""
    receipt: dict[str, Any] = {}
    if receipt_json:
        try:
            loaded = json.loads(receipt_json)
            receipt = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid receipt_json: {exc}", "connector": CONNECTOR_ID}

    body = text or (str(receipt.get("text") or "")) or (_pdf_text(os.path.expanduser(path)) if path else "")
    fields = _regex_fields(body) if body.strip() else {
        "nip": None, "number": None, "issueDate": None, "seller": None,
        "net": None, "vat": None, "gross": None, "currency": None,
    }
    if use_llm and body.strip() and not (fields.get("nip") and fields.get("gross")):
        llm = _llm_fields(body, model)
        if llm:
            for k, v in llm.items():
                if v not in (None, "") and not fields.get(k):
                    fields[k] = v

    items = receipt.get("items") if isinstance(receipt.get("items"), list) else []
    items_sum = round(sum(float(i.get("price") or 0) for i in items), 2) if items else None
    gross = fields.get("gross")
    if gross is None:
        gross = receipt.get("total") if isinstance(receipt.get("total"), (int, float)) else items_sum

    net, vat = fields.get("net"), fields.get("vat")
    rate = float(vat_rate or 0)
    if gross is not None and (net is None or vat is None) and rate > 0:
        net = round(gross / (1 + rate / 100.0), 2)
        vat = round(gross - net, 2)

    nip = _norm_nip(fields.get("nip") or receipt.get("nip"))
    draft = {
        "type": "invoice-draft",
        "source": "receipt",
        "number": _clean_number(number) or fields.get("number"),
        "issueDate": fields.get("issueDate") or receipt.get("date"),
        "seller": (seller or fields.get("seller")),
        "sellerNip": nip,
        "buyerNip": _norm_nip(buyer_nip) if buyer_nip else None,
        "currency": (currency or fields.get("currency") or receipt.get("currency") or "PLN").upper(),
        "vatRate": rate,
        "items": items,
        "itemsSum": items_sum,
        "net": net,
        "vat": vat,
        "gross": gross,
        "ksefReady": bool(nip and gross is not None),
    }
    notes = []
    if gross is None:
        notes.append("no total/gross found — set items or total")
    if items_sum is not None and gross is not None and abs(items_sum - gross) > 0.02:
        notes.append(f"items sum {items_sum} != gross {gross}")
    return {"ok": gross is not None, "connector": CONNECTOR_ID, "kind": "invoice-draft", "live": False,
            "draft": draft, "fields": fields, "notes": notes}


# --- KSeF FA(2) XML generation (build an e-invoice draft the API connector can submit) ----
# We emit the FA(2) structure (namespace + Naglowek/Podmiot1/Podmiot2/Fa/FaWiersz) that
# _parse_fa_vat reads back exactly. It is a DRAFT: it is not XSD-validated here, so validate
# against the official FA(2) schema before sending it to the real KSeF API.
_FA2_NS = "http://crd.gov.pl/wzor/2023/06/29/12648/"
_RATE_INDEX = {23: "1", 8: "2", 5: "3", 0: "6"}  # P_13_x / P_14_x slot per VAT rate


def _q2(x: Any) -> str:
    return f"{round(float(x), 2):.2f}"


def _build_fa2_xml(draft: dict[str, Any], *, system_info: str = "ifURI",
                   created_at: str = "", sale_date: str = "", number: str = "",
                   seller_address: str = "", buyer_address: str = "") -> str:
    """Serialise an invoice draft into KSeF FA(2) XML, including the mandatory Adnotacje
    block and party addresses so it is structurally complete (still validate vs the XSD)."""
    def E(parent, tag, text=None, **attrs):
        el = _ET.SubElement(parent, f"{{{_FA2_NS}}}{tag}", **attrs)
        if text is not None:
            el.text = str(text)
        return el

    _ET.register_namespace("", _FA2_NS)
    root = _ET.Element(f"{{{_FA2_NS}}}Faktura")
    nag = E(root, "Naglowek")
    E(nag, "KodFormularza", "FA", kodSystemowy="FA (2)", wersjaSchemy="1-0E")
    E(nag, "WariantFormularza", "2")
    E(nag, "DataWytworzeniaFa", created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    E(nag, "SystemInfo", system_info)

    p1 = E(root, "Podmiot1")
    did1 = E(p1, "DaneIdentyfikacyjne")
    E(did1, "NIP", draft.get("sellerNip") or "0000000000")
    E(did1, "Nazwa", draft.get("seller") or "Sprzedawca")
    adr1 = E(p1, "Adres")
    E(adr1, "KodKraju", "PL")
    E(adr1, "AdresL1", seller_address or "ul. Przykładowa 1, 00-001 Miasto")
    p2 = E(root, "Podmiot2")
    did2 = E(p2, "DaneIdentyfikacyjne")
    E(did2, "NIP", draft.get("buyerNip") or "9999999999")
    E(did2, "Nazwa", draft.get("buyer") or "Nabywca")
    adr2 = E(p2, "Adres")
    E(adr2, "KodKraju", "PL")
    E(adr2, "AdresL1", buyer_address or "ul. Nabywcza 2, 00-002 Miasto")

    fa = E(root, "Fa")
    E(fa, "KodWaluty", draft.get("currency") or "PLN")
    E(fa, "P_1", draft.get("issueDate") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    E(fa, "P_2", number or draft.get("number") or "DRAFT")
    E(fa, "P_6", sale_date or draft.get("issueDate") or "")
    rate = int(round(float(draft.get("vatRate") or 23)))
    idx = _RATE_INDEX.get(rate, "1")
    if draft.get("net") is not None:
        E(fa, f"P_13_{idx}", _q2(draft["net"]))
    if draft.get("vat") is not None:
        E(fa, f"P_14_{idx}", _q2(draft["vat"]))
    if draft.get("gross") is not None:
        E(fa, "P_15", _q2(draft["gross"]))
    # Adnotacje is mandatory in FA(2); set every flag to 2 ("nie") / "brak" by default.
    adn = E(fa, "Adnotacje")
    E(adn, "P_16", "2")
    E(adn, "P_17", "2")
    E(adn, "P_18", "2")
    E(adn, "P_18A", "2")
    zw = E(adn, "Zwolnienie")
    E(zw, "P_19N", "1")
    nst = E(adn, "NoweSrodkiTransportu")
    E(nst, "P_22N", "1")
    E(adn, "P_23", "2")
    pm = E(adn, "PMarzy")
    E(pm, "P_PMarzyN", "1")
    E(fa, "RodzajFaktury", "VAT")

    for i, item in enumerate(draft.get("items") or [], start=1):
        gross_line = float(item.get("price") or 0)
        net_line = round(gross_line / (1 + rate / 100.0), 2) if rate else gross_line
        w = E(fa, "FaWiersz")
        E(w, "NrWierszaFa", str(i))
        E(w, "P_7", str(item.get("name") or f"Pozycja {i}"))
        E(w, "P_8B", "1")
        E(w, "P_9A", _q2(net_line))
        E(w, "P_11", _q2(net_line))
        E(w, "P_12", str(rate))

    return _ET.tostring(root, encoding="unicode")


@INVOICE.handler("ksef/query/build", isolated=True,
                 meta={"label": "Build a KSeF FA(2) XML draft from an invoice/receipt", "cliAlias": "ksef-build"})
def ksef_build(draft_json: str = "", text: str = "", receipt_json: str = "", vat_rate: float = 23.0,
               seller: str = "", buyer_nip: str = "", number: str = "", currency: str = "",
               output_path: str = "", system_info: str = "ifURI", created_at: str = "",
               sale_date: str = "", seller_address: str = "", buyer_address: str = "") -> dict[str, Any]:
    """Build a KSeF **FA(2) XML draft** from an invoice draft (`draft_json` from
    invoice://host/receipt/query/draft) or straight from a receipt (`receipt_json`/`text`).
    Completes the office chain: paragon → draft → FA(2) XML the ksef:// API connector submits.
    Optionally writes the XML to `output_path`. NOTE: a draft — validate against the official
    FA(2) XSD before real submission. Re-parses its own output so `parsed` mirrors `ksef_parse`."""
    if draft_json:
        try:
            loaded = json.loads(draft_json)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid draft_json: {exc}", "connector": CONNECTOR_ID}
        draft = loaded.get("draft") if isinstance(loaded, dict) and "draft" in loaded else loaded
    else:
        built = receipt_draft(text=text, receipt_json=receipt_json, vat_rate=vat_rate,
                              seller=seller, buyer_nip=buyer_nip, number=number, currency=currency)
        if not built.get("ok"):
            return built
        draft = built["draft"]
    if not isinstance(draft, dict):
        return {"ok": False, "error": "draft must be an object", "connector": CONNECTOR_ID}

    xml = _build_fa2_xml(draft, system_info=system_info, created_at=created_at,
                         sale_date=sale_date, number=number,
                         seller_address=seller_address, buyer_address=buyer_address)
    written = None
    if output_path:
        written = os.path.expanduser(output_path)
        os.makedirs(os.path.dirname(os.path.abspath(written)) or ".", exist_ok=True)
        with open(written, "w", encoding="utf-8") as fh:
            fh.write(xml)
    parsed = _parse_fa_vat(xml)
    _ledger("ksef_build", gross=draft.get("gross"), nip=draft.get("sellerNip"),
            number=draft.get("number") or number, currency=draft.get("currency"), path=written)
    return {"ok": bool(draft.get("gross") is not None and draft.get("sellerNip")),
            "connector": CONNECTOR_ID, "kind": "invoice-xml", "live": False, "formCode": "FA", "variant": "2",
            "xml": xml, "path": written, "draft": draft, "parsed": parsed}


# --- KSeF FA(2) validation (structural always; full XSD when an official schema is given) ---
# Required top-level / Fa elements for a structurally complete FA(2). The authoritative check
# is the official XSD (set xsd_path / KSEF_FA2_XSD); these catch the common omissions offline.
_FA2_REQUIRED_TOP = ["Naglowek", "Podmiot1", "Podmiot2", "Fa"]
_FA2_REQUIRED_FA = ["KodWaluty", "P_1", "P_2", "P_15", "Adnotacje", "RodzajFaktury"]


def _structural_validate(data) -> tuple[list[str], list[str]]:
    """Offline structural + arithmetic checks for a FA(2) document. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        root = _ET.fromstring(data)
    except _ET.ParseError as exc:
        return [f"not well-formed XML: {exc}"], []
    if _FA2_NS not in (root.tag or "") and not any(_FA2_NS in (e.tag or "") for e in root.iter()):
        warnings.append(f"FA(2) namespace {_FA2_NS} not found — wrong schema version?")
    if _ln(root.tag) != "Faktura":
        errors.append(f"root element is <{_ln(root.tag)}>, expected <Faktura>")
    for name in _FA2_REQUIRED_TOP:
        if _sub(root, name) is None:
            errors.append(f"missing required <{name}>")
    fa = _sub(root, "Fa")
    for name in _FA2_REQUIRED_FA:
        if fa is not None and _txt(fa, name) is None and _sub(fa, name) is None:
            errors.append(f"missing required <Fa>/<{name}>")
    for party in ("Podmiot1", "Podmiot2"):
        node = _sub(root, party)
        if node is not None:
            if _txt(node, "NIP") is None:
                errors.append(f"<{party}> missing NIP")
            if _sub(node, "Adres") is None:
                warnings.append(f"<{party}> has no <Adres> (FA(2) requires an address)")
    # arithmetic: P_13_x + P_14_x == P_15
    if fa is not None:
        net = vat = None
        for el in fa.iter():
            ln = _ln(el.tag)
            if ln.startswith("P_13_") and el.text:
                net = (net or 0) + (_xnum(el.text) or 0)
            elif ln.startswith("P_14_") and el.text:
                vat = (vat or 0) + (_xnum(el.text) or 0)
        gross = _xnum(_txt(fa, "P_15"))
        if net is not None and vat is not None and gross is not None and abs(round(net + vat, 2) - gross) > 0.02:
            errors.append(f"net+VAT ({round(net + vat, 2)}) != P_15 gross ({gross})")
    nip = _txt(_sub(root, "Podmiot1"), "NIP") if _sub(root, "Podmiot1") is not None else None
    if nip and len(re.sub(r'\D', '', nip)) != 10:
        warnings.append(f"seller NIP '{nip}' is not 10 digits")
    return errors, warnings


@INVOICE.handler("ksef/query/validate", isolated=True,
                 meta={"label": "Validate a KSeF FA(2) XML (XSD when given, else structural)", "cliAlias": "ksef-validate"})
def ksef_validate(xml: str = "", path: str = "", xsd_path: str = "") -> dict[str, Any]:
    """Validate a KSeF FA(2) XML (`xml` or `path`). With an official FA(2) schema —
    `xsd_path` or env KSEF_FA2_XSD — runs a full XSD validation via lxml. Otherwise runs
    offline structural + arithmetic checks (required elements, party NIP/address, net+VAT==gross)
    and says so. Download the official FA(2) XSD from crd.gov.pl once and point xsd_path at it
    for an authoritative check before real submission."""
    data = xml
    if not data and path:
        path = os.path.expanduser(path)
        try:
            data = open(path, encoding="utf-8").read()
        except OSError as exc:
            return {"ok": False, "error": str(exc), "path": path, "connector": CONNECTOR_ID}
    if not data:
        return {"ok": False, "error": "provide xml or path", "connector": CONNECTOR_ID}

    xsd = os.path.expanduser(xsd_path) if xsd_path else os.getenv("KSEF_FA2_XSD", "")
    if xsd:
        try:
            from lxml import etree  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"lxml required for XSD validation: {exc}", "connector": CONNECTOR_ID}
        try:
            schema = etree.XMLSchema(etree.parse(xsd))
            doc = etree.fromstring(data.encode("utf-8"))
        except (etree.XMLSyntaxError, etree.XMLSchemaParseError, OSError) as exc:
            return {"ok": False, "error": f"schema/XML load failed: {exc}", "connector": CONNECTOR_ID, "schema": xsd}
        valid = bool(schema.validate(doc))
        errors = [f"line {e.line}: {e.message}" for e in schema.error_log]
        return {"ok": True, "connector": CONNECTOR_ID, "kind": "validation", "live": False, "valid": valid, "checkedWith": "xsd",
                "schema": xsd, "errors": errors, "errorCount": len(errors)}

    errors, warnings = _structural_validate(data)
    return {"ok": True, "connector": CONNECTOR_ID, "kind": "validation", "live": False, "valid": not errors, "checkedWith": "structural",
            "errors": errors, "warnings": warnings,
            "note": "no FA(2) XSD given (set xsd_path or KSEF_FA2_XSD) — only structural/arithmetic checks ran"}


# --- KSeF UPO (Urzędowe Poświadczenie Odbioru) — confirmation of a submitted invoice --------
# After a successful send KSeF returns the UPO carrying the assigned KSeF number(s). KSeF 2.0
# may hand it back as JSON or as the signed XML; this reads either, namespace-agnostic.
_UPO_JSON_KEYS = {
    "ksefNumber": ["ksefReferenceNumber", "ksefNumber", "referenceNumberKsef"],
    "referenceNumber": ["referenceNumber", "elementReferenceNumber", "sessionReferenceNumber"],
    "timestamp": ["acquisitionTimestamp", "receiveTimestamp", "timestamp", "invoicingDate"],
    "invoiceNumber": ["invoiceNumber", "documentNumber"],
    "invoiceHash": ["invoiceHash", "documentHash", "sha"],
}
_UPO_XML_TAGS = {
    "ksefNumber": ["NumerKSeF", "NumerKSeFDokumentu", "KSeFReferenceNumber"],
    "referenceNumber": ["NumerReferencyjny", "ElementReferencyjny", "NumerReferencyjnyEPP"],
    "timestamp": ["DataPrzyjecia", "Czas", "DataWytworzeniaPoswiadczenia", "DataPrzeslania"],
    "invoiceNumber": ["NumerFaktury", "P_2"],
    "invoiceHash": ["SkrotDokumentu", "SkrotZlozenia"],
    "nip": ["NIP", "Identyfikator"],
}


def _find_json(obj, keys: list[str]):
    """Depth-first search of a decoded JSON object for the first matching key (case-insensitive)."""
    want = {k.lower() for k in keys}
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in want and v not in (None, "", [], {}):
                    return v
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _parse_upo(data: str) -> dict[str, Any]:
    """Extract the KSeF confirmation fields from a UPO (JSON or signed XML)."""
    text = (data or "").strip()
    fmt = "unknown"
    out: dict[str, Any] = {"ksefNumber": None, "referenceNumber": None, "timestamp": None,
                           "invoiceNumber": None, "invoiceHash": None, "nip": None}
    if text.startswith("{") or text.startswith("["):
        try:
            obj = json.loads(text)
            fmt = "json"
            for field, keys in _UPO_JSON_KEYS.items():
                out[field] = _find_json(obj, keys)
            out["nip"] = out.get("nip") or _find_json(obj, ["nip", "identifier"])
        except json.JSONDecodeError:
            pass
    if fmt == "unknown":
        try:
            root = _ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
            fmt = "xml"
            for field, tags in _UPO_XML_TAGS.items():
                for tag in tags:
                    val = _txt(root, tag)
                    if val:
                        out[field] = val
                        break
        except _ET.ParseError:
            return {"ok": False, "error": "UPO is neither valid JSON nor XML", "format": fmt}
    out["nip"] = _norm_nip(out.get("nip")) if out.get("nip") else None
    return {"ok": bool(out.get("ksefNumber") or out.get("referenceNumber")),
            "connector": CONNECTOR_ID, "kind": "upo", "live": False, "format": fmt, **out}


@INVOICE.handler("ksef/query/upo", isolated=True,
                 meta={"label": "Parse a KSeF UPO (confirmation) → KSeF number + timestamp", "cliAlias": "ksef-upo"})
def ksef_upo(path: str = "", xml: str = "", text: str = "", output_path: str = "") -> dict[str, Any]:
    """Parse a KSeF UPO (Urzędowe Poświadczenie Odbioru) from `path`/`xml`/`text` — JSON or
    signed XML — into the assigned KSeF number, reference number, timestamp and invoice hash.
    Optionally save the raw UPO to `output_path` (the durable proof a flow archives)."""
    data = xml or text
    if not data and path:
        path = os.path.expanduser(path)
        try:
            data = open(path, encoding="utf-8").read()
        except OSError as exc:
            return {"ok": False, "error": str(exc), "path": path, "connector": CONNECTOR_ID}
    if not data:
        return {"ok": False, "error": "provide path, xml or text", "connector": CONNECTOR_ID}
    parsed = _parse_upo(data)
    if output_path and parsed.get("ok"):
        out = os.path.expanduser(output_path)
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(data)
        parsed["savedTo"] = out
    if parsed.get("ok"):
        _ledger("ksef_upo", ksefNumber=parsed.get("ksefNumber"),
                referenceNumber=parsed.get("referenceNumber"), savedTo=parsed.get("savedTo"))
    return parsed


@INVOICE.handler("ledger/query/list", isolated=True,
                 meta={"label": "Read the shared transaction ledger → recent rows + totals", "cliAlias": "ledger"})
def ledger_list(path: str = "", limit: int = 50, event: str = "", connector: str = "",
                since: float = 0.0) -> dict[str, Any]:
    """Read the shared transaction ledger (`~/.urirun/ledger.jsonl`, or env URIRUN_LEDGER /
    `path`) that the camera + invoice connectors auto-append to. Optional filters: `event`
    (receipt|inspect|ingest|ksef_build|ksef_upo), `connector`, `since` (epoch). Returns the
    most recent rows plus a summary: per-event counts, receipts total, invoices built + gross
    sum, and the KSeF numbers confirmed."""
    src = os.path.expanduser(path) if path else os.getenv(
        "URIRUN_LEDGER", os.path.expanduser("~/.urirun/ledger.jsonl"))
    rows: list[dict[str, Any]] = []
    try:
        with open(src, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event and rec.get("event") != event:
                    continue
                if connector and rec.get("connector") != connector:
                    continue
                if since and (rec.get("ts") or 0) < since:
                    continue
                rows.append(rec)
    except OSError:
        return {"ok": True, "connector": CONNECTOR_ID, "kind": "ledger", "live": False, "path": src, "exists": False,
                "count": 0, "summary": {}, "rows": []}

    counts: dict[str, int] = {}
    gross_sum = 0.0
    receipts_total = 0.0
    ksef_numbers: list[str] = []
    for rec in rows:
        ev = rec.get("event", "?")
        counts[ev] = counts.get(ev, 0) + 1
        if ev == "ksef_build" and isinstance(rec.get("gross"), (int, float)):
            gross_sum += rec["gross"]
        if ev == "receipt" and isinstance(rec.get("total"), (int, float)):
            receipts_total += rec["total"]
        if ev == "ksef_upo" and rec.get("ksefNumber"):
            ksef_numbers.append(rec["ksefNumber"])

    summary = {"events": dict(sorted(counts.items())),
               "receiptsTotal": round(receipts_total, 2),
               "invoicesBuilt": counts.get("ksef_build", 0),
               "grossBuilt": round(gross_sum, 2),
               "ksefConfirmed": len(ksef_numbers), "ksefNumbers": ksef_numbers[-20:]}
    recent = sorted(rows, key=lambda r: r.get("ts") or 0, reverse=True)[: max(1, int(limit))]
    return {"ok": True, "connector": CONNECTOR_ID, "kind": "ledger", "live": False, "path": src, "exists": True,
            "count": len(rows), "summary": summary, "rows": recent}


def main(argv: list[str] | None = None) -> int:
    return INVOICE.cli(argv, manifest_prose=urirun.load_manifest(__package__))


urirun_bindings = INVOICE.bindings
