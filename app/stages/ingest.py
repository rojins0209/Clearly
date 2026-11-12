"""Stage 1 - Ingest: open the PDF, decide whether it has a usable text layer or is a scan,
and produce source text that later stages use to *verify* what the extractor claims."""
import hashlib

import pdfplumber


def ingest(path: str) -> dict:
    data = open(path, "rb").read()
    out = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    try:
        with pdfplumber.open(path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
            out["pages"] = len(pdf.pages)
    except Exception as e:  # corrupt / not a PDF
        out.update(kind="unreadable", text="", error=str(e), pages=0)
        return out

    text = "\n".join(pages)
    if len(text.strip()) >= 40:
        out.update(kind="text", text=text, ocr_used=False)
        return out

    # No text layer: it's a scan or photo. OCR it so we have something to verify against.
    out["kind"] = "scanned"
    try:
        out["text"] = ocr(path)
        out["ocr_used"] = True
    except Exception as e:
        out["text"] = ""
        out["ocr_used"] = False
        out["ocr_error"] = f"OCR unavailable: {e}"
    return out


def ocr(path: str) -> str:
    import pypdfium2 as pdfium
    import pytesseract
    doc = pdfium.PdfDocument(path)
    texts = []
    for i in range(len(doc)):
        img = doc[i].render(scale=2.2).to_pil().convert("L")
        texts.append(pytesseract.image_to_string(img, config="--psm 6"))
    return "\n".join(texts)
