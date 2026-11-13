"""Validation of explicitly labelled GS1 barcodes and manufacturer part numbers.

Never infer product identifiers from an unlabeled model number or arbitrary digits.
Missing identifiers are reported as unverified, not silently treated as matching.
"""
from __future__ import annotations
import re
import unicodedata

_GTIN = re.compile(r'(?<!\w)(?:GTIN|EAN(?:-?13|-?8)?|UPC(?:-?A)?|codigo\s+de\s+barras)\s*[:#-]?\s*([\d][\d .-]{6,21}\d)(?!\w)', re.I)
_MPN = re.compile(r'(?<!\w)(?:MPN|part\s*number|codigo\s+do\s+fabricante|referencia\s+do\s+fabricante)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]{2,39})(?!\w)', re.I)

def valid_gtin(value):
    digits = re.sub(r'[^0-9]', '', str(value or ''))
    if len(digits) not in (8, 12, 13, 14):
        return False
    data = digits[:-1]
    checksum = sum(int(d) * (3 if position % 2 == 0 else 1)
                   for position, d in enumerate(reversed(data)))
    return (10 - checksum % 10) % 10 == int(digits[-1])

def identifiers(text):
    raw = unicodedata.normalize('NFKC', str(text or ''))
    gtins = {re.sub(r'[^0-9]', '', m.group(1)) for m in _GTIN.finditer(raw)}
    mpns = {re.sub(r'[^A-Z0-9]', '', m.group(1).upper()) for m in _MPN.finditer(raw)}
    return {'gtin': {x for x in gtins if valid_gtin(x)}, 'mpn': mpns}

def compare(request, listing):
    """Return (accepted, reason) with fail-closed validation for explicit IDs."""
    wanted = identifiers(request)
    offered = identifiers(listing)
    for field in ('gtin', 'mpn'):
        if wanted[field]:
            if not offered[field]:
                return False, field + '_not_verified'
            if not (wanted[field] & offered[field]):
                return False, field + '_conflict'
    return True, 'identifiers_compatible'
