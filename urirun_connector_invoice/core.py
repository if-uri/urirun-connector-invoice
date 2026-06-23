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
import os
import re
import shutil
import subprocess
from typing import Any

import urirun

CONNECTOR_ID = "invoice"
INVOICE = urirun.connector(CONNECTOR_ID, scheme="invoice", target="host", meta={"label": "Invoice field extraction"})

# --- Polish-invoice patterns (also catch common EN labels) -----------------------------
_NIP_RE = re.compile(r"NIP[:\s]*([A-Z]{0,2}\s?\d[\d\s\-]{8,13}\d)", re.I)
_NUM_RE = re.compile(r"(?:faktura(?:\s+(?:nr|vat|nr\.?))?|invoice(?:\s+no)?|nr\s+faktury|FV|FS|FVS)[:\s/]*"
                     r"([A-Z]{0,4}[\w./\-]*\d[\w./\-]*)", re.I)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})")
_ISSUE_RE = re.compile(r"(?:data\s+wystawienia|wystawiono|date\s+of\s+issue|issue\s+date)[:\s]*"
                       r"(\d{4}-\d{2}-\d{2}|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})", re.I)
_SELLER_RE = re.compile(r"(?:sprzedawca|seller|sprzedaj[ąa]cy)[:\s]*\n?\s*(.+)", re.I)
# a money amount needs a 2-digit minor part (",00" / ".00") — skips years, percents, NIPs
_AMOUNT = r"([0-9][0-9\s .]*[.,][0-9]{2})\b"
_GROSS_RE = re.compile(r"(?:do\s+zap[łl]aty|razem\s+do\s+zap[łl]aty|brutto|gross|total|suma)[:\s]*" + _AMOUNT, re.I)
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
            "connector": CONNECTOR_ID, "path": path, "method": method, "fields": fields}


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
    return {"ok": True, "connector": CONNECTOR_ID, "root": root, "invoices": len(rows),
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
            "connector": CONNECTOR_ID, "path": path, "fields": fields}


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
    return {"ok": True, "connector": CONNECTOR_ID, "root": root, "invoices": len(rows),
            "totals": {k: round(v, 2) for k, v in totals.items()},
            "byRate": {r: {k: round(x, 2) for k, x in v.items()} for r, v in by_rate.items()},
            "csv": csv_written, "rows": rows[:300]}


def main(argv: list[str] | None = None) -> int:
    return INVOICE.cli(argv, manifest_prose=urirun.load_manifest(__package__))


urirun_bindings = INVOICE.bindings
