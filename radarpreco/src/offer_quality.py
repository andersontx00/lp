"""Conservative title checks: never price an accessory as a complete device.

This is a fail-closed filter for device searches, not an estimation of price.
Intentionally does not reject legitimate explicit accessory searches.
"""
from __future__ import annotations
import re
import unicodedata

# Cut over when accessory classification was added. Keep earlier data in the DB
# for an audit, but do not use unverified legacy device prices in graphs/alerts.
DEVICE_REBASE_AT = '2026-11-13T03:55:00+00:00'


def normalized(raw: object) -> str:
    s = unicodedata.normalize('NFKD', str(raw or '').casefold())
    return ''.join(c for c in s if not unicodedata.combining(c))


_ACCESSORY = re.compile(
    r'\b(?:capas?|capinhas?|cases?|covers?|peliculas?|protetor(?:es|a|as)?|'
    r'carregador(?:es)?|cabos?|adaptador(?:es)?|fontes?|suportes?|'
    r'bolsas?|carteiras?|tampas?|carcacas?|chassis|chassi|'
    r'tela(?:s)?|displays?|modulos?|touch\s*screen|'
    r'baterias?|conectores?|flex(?:ivel)?|lentes?|'
    r'adesivos?|skins?|peluche|acessorios?|pecas?|reparos?|'
    r'trip[eé]s?|popsockets?|vidros?|'
    r'capa\s+traseira|tampa\s+traseira|'
    r'fone(?:s)?\s+(?:de\s+ouvido|bluetooth)|'
    r'headsets?|mousepads?|teclados?)\b', re.I)

_NON_DEVICE = re.compile(
    r'\b(?:replica|fake|dummy|maquete|miniatura|brinquedo|'
    r'molde|molde\s+de\s+silicone|somente\s+(?:a\s+)?caixa|'
    r'caixa\s+vazia|apenas\s+a\s+caixa|sem\s+aparelho|'
    r'nao\s+inclui\s+(?:o\s+)?(?:celular|smartphone|aparelho))\b', re.I)

_STRONG_PHONE = re.compile(
    r'\b(?:smartphone|celular|telefone\s+movel|aparelho\s+(?:celular|smartphone))\b', re.I)
_STRONG_TABLET = re.compile(r'\btablet\b', re.I)

_ACCESSORY_INTENT = re.compile(
    r'^(?:(?:kit|combo|conjunto|pack|jogo)\s+(?:com|de)?\s*(?:\d+\s*)?)?'
    r'(?:capas?|capinhas?|cases?|covers?|peliculas?|protetores?|'
    r'carregadores?|cabos?|adaptadores?|fontes?|suportes?|bolsas?|'
    r'telas?|displays?|baterias?|tampas?|carcacas?|lentes?|'
    r'adesivos?|skins?|mousepads?|acessorios?|pecas?|teclados?)\b', re.I)


def kind_of(query: str) -> str | None:
    q = normalized(query).strip()
    if _ACCESSORY_INTENT.search(q):
        return None
    if re.search(r'\b(?:ipad|tablet|galaxy\s+tab|xiaomi\s+pad|lenovo\s+tab|redmi\s+pad|kindle|kobo\s+(?:clara|libra))\b', q):
        return 'tablet'
    if re.search(r'\b(?:iphone|smartphone|celular|galaxy\s+(?:a\d+|s\d+|m\d+|z\s*(?:fold|flip)|note\s*\d+)|'
                 r'redmi(?:\s+note)?\s*\d+|poco\s+[a-z]\d+|moto\s+[a-z]\d+|'
                 r'motorola\s+(?:edge|moto|razr)|realme\s+[a-z]?\d+|zenfone\s+\d+|oneplus\s+\d+|google\s+pixel\s+\d+|xiaomi\s+\d+[a-z]?)\b', q):
        return 'phone'
    return None


def device_offer_ok(wanted: str, candidate_title: str) -> bool:
    """Reject accessory-only offers for smartphone/tablet searches.

    Bundled devices can contain accessories, but must explicitly describe the
    main device before the accessory; accessory headings are never accepted.
    """
    kind = kind_of(wanted)
    if not kind:
        return True
    title = normalized(candidate_title).strip()
    if not title or _NON_DEVICE.search(title):
        return False
    if not _ACCESSORY.search(title):
        return True
    if _ACCESSORY_INTENT.search(title):
        return False
    if re.search(r'^\s*(?:kit|combo|pack|conjunto)\s+(?:de|com)?\s*\d*\s*', title):
        stripped = re.sub(r'^\s*(?:kit|combo|pack|conjunto)\s+(?:de|com)?\s*\d*\s*', '', title)
        if _ACCESSORY_INTENT.search(stripped):
            return False
    device = _STRONG_PHONE if kind == 'phone' else _STRONG_TABLET
    m_device = device.search(title)
    m_accessory = _ACCESSORY.search(title)
    if not m_device or not m_accessory or m_device.start() >= m_accessory.start():
        return False
    return True


def minimum_trusted_at(query: str) -> str | None:
    """Never use pre-validation snapshots whose source listing title is lost."""
    from product_identity import minimum_trusted_at as identity_cutoff
    global_cutoff=identity_cutoff(query)
    phone_cutoff=DEVICE_REBASE_AT if kind_of(query) else None
    return max(global_cutoff or '',phone_cutoff or '') or None
