"""Reject mismatched product variants *before* accepting provider prices."""
import re
import unicodedata

COLORS={'preto':{'preto','black'},'branco':{'branco','white'},'azul':{'azul','blue'},'verde':{'verde','green'},'vermelho':{'vermelho','red'},'rosa':{'rosa','pink'},'cinza':{'cinza','grey','gray'},'prata':{'prata','silver'}}

def normalize(raw):
    s=unicodedata.normalize('NFKD',str(raw or '')).lower()
    return ''.join(ch for ch in s if not unicodedata.combining(ch))

def variant_tokens(s):
    text=normalize(s)
    text=re.sub(r'(?<=\d)\s+(?=(?:gb|tb|v|hz|w)\b)','',text)
    return set(re.findall(r'[a-z0-9]+',text))

def incompatible(wanted,candidate):
    a=variant_tokens(wanted);b=variant_tokens(candidate)
    def group(pattern):
        return ({x for x in a if re.fullmatch(pattern,x)},{x for x in b if re.fullmatch(pattern,x)})
    for pattern in (r'\d{2,4}v',r'\d+(?:gb|tb)',r'\d{2,4}hz'):
        requested,found=group(pattern)
        if requested and found and not requested.issubset(found):return True
    # Explicit electrical voltage must be stated in the listing; 127 V and 220 V aren't interchangeable.
    volts,found=group(r'(?:110|127|220|230|240)v')
    if volts and not volts.issubset(found):return True
    # If title explicitly names a color, avoid confidently pairing a different color.
    for word,synonyms in COLORS.items():
        if a.intersection(synonyms):
            mentioned={label for label,aliases in COLORS.items() if b.intersection(aliases)}
            if mentioned and word not in mentioned:return True
    # Marketplace sellers may label pre-owned offers; do not treat them as equivalent to new.
    if re.search(r'\b(usado|seminovo|recondicionado|refurbished|open\s*box)\b',normalize(candidate)):
        if not re.search(r'\b(usado|seminovo|recondicionado|refurbished|open\s*box)\b',normalize(wanted)):
            return True
    return False
