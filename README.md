# urirun-connector-invoice

**Invoice field extraction** — connector ekosystemu [ifURI / urirun](https://github.com/if-uri/urirun).
Schemat URI: `invoice://`

Extract structured fields from invoices over invoice:// URIs — NIP, number, dates, net/VAT/gross, seller, currency. Regex tuned for Polish faktura (also EN), text via pdftotext, optional LLM (OpenRouter) fallback. A folder route batches a month of invoices into a CSV with net/VAT/gross totals.

## Opis

invoice:// turns invoice parsing into a first-class URI. invoice://host/file/query/parse returns structured fields {nip, number, issueDate, seller, net, vat, gross, currency} from one PDF/text — regex-first (text-layer PDFs via pdftotext), with an optional LLM pass (OpenRouter via litellm, use_llm=true) for layouts the regex misses. invoice://host/folder/query/audit parses every invoice under a folder into rows plus net/VAT/gross totals, optionally writing a CSV — for monthly accounting. Read-only over the invoice files. Pairs with doc:// (OCR for scans) and fs:// (dedupe) in the office flow.

## Wymagania

- **system:** pdftotext (poppler) for text-layer PDFs
- **python:** urirun
- **optional:** litellm + OPENROUTER_API_KEY for the LLM fallback; doc:// OCR for scanned invoices

## Instalacja (dev)

```bash
pip install -e .
pytest -q
```

## Powiązane

- Rdzeń: [if-uri/urirun](https://github.com/if-uri/urirun)
- Hub connectorów: [connect.ifuri.com](https://connect.ifuri.com)

---
Kategoria: Accounting · Słowa kluczowe: invoice, faktura, nip, vat, accounting, ksiegowosc, ocr, extraction · Wydawca: if-uri
