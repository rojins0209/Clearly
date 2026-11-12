"""Indian GST helpers: GSTIN structure + checksum, state codes, tax-type rule.

GSTIN = 2-digit state code + 10-char PAN + entity number + 'Z' + check character.
The check character is a mod-36 checksum over the first 14 characters.
"""
import re

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}
STATE_NAME_TO_CODE = {v.lower(): k for k, v in STATE_CODES.items()}


def checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = CHARSET.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return CHARSET[(36 - total % 36) % 36]


def normalize_gstin(value: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (value or "").upper())


def validate_gstin(value: str | None) -> tuple[bool, str]:
    """Returns (ok, reason)."""
    g = normalize_gstin(value)
    if not g:
        return False, "GSTIN missing"
    if len(g) != 15:
        return False, f"GSTIN has {len(g)} characters, expected 15"
    if not GSTIN_RE.match(g):
        return False, "GSTIN does not follow the state+PAN+entity+Z+check pattern"
    if g[:2] not in STATE_CODES:
        return False, f"Unknown state code {g[:2]}"
    expected = checksum_char(g[:14])
    if g[14] != expected:
        return False, f"Checksum character is '{g[14]}', expected '{expected}' (likely a typo or a fabricated GSTIN)"
    return True, "valid"


def state_of(gstin: str | None) -> str | None:
    g = normalize_gstin(gstin)
    return g[:2] if len(g) >= 2 and g[:2] in STATE_CODES else None


def state_code_from_text(text: str | None) -> str | None:
    """Accepts '29', '29-Karnataka', 'Karnataka (29)', 'Karnataka'."""
    if not text:
        return None
    m = re.search(r"\b(\d{2})\b", text)
    if m and m.group(1) in STATE_CODES:
        return m.group(1)
    low = text.lower()
    for name, code in STATE_NAME_TO_CODE.items():
        if name in low:
            return code
    return None


def expected_tax_type(supplier_state: str, place_of_supply_state: str) -> str:
    """Intra-state supply -> CGST+SGST; inter-state -> IGST (IGST Act s.7/s.8 logic, simplified)."""
    return "CGST+SGST" if supplier_state == place_of_supply_state else "IGST"


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    base = f"{state}{pan}{entity}Z"
    return base + checksum_char(base)
