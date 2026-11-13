"""Conservative model identity validation for external shopping listings.

A matching number, brand, or technology does not prove that two products
share the same model. Old observations without product titles are quarantined
from recommendations but preserved in storage for audit.
"""
from __future__ import annotations
import re
import unicodedata
import json
from pathlib import Path
from functools import lru_cache

QUALITY_REBASE_AT = '2026-11-13T17:00:12+00:00'

def normalized(value):
    text=unicodedata.normalize('NFKD',str(value or '').casefold())
    return ''.join(c for c in text if not unicodedata.combining(c))

def canonical_query(query):
    s=re.sub(r'\s+',' ',normalized(query).strip())
    match=re.fullmatch(r'(?:(?:tenis|tenis de corrida)\s+)?(?:olympikus\s+)?(?:core|corre)\s*([345])',s)
    return 'Olympikus Corre '+match.group(1) if match else str(query or '').strip()

_BRANDS={
 'olympikus':('olympikus',),'logitech':('logitech',),
 'samsung':('samsung',),'motorola':('motorola',),
 'xiaomi':('xiaomi',),'redragon':('redragon',),
 'electrolux':('electrolux',),'brastemp':('brastemp',),
 'midea':('midea',),'hyperx':('hyperx',),
 'razer':('razer',),'corsair':('corsair',),
 'jbl':('jbl',),'adidas':('adidas',),'nike':('nike',),
 'asics':('asics',),'new balance':('new balance','newbalance'),
 'puma':('puma',),'mizuno':('mizuno',),'hoka':('hoka',),
 'dell':('dell',),'lenovo':('lenovo',),'acer':('acer',),
 'philips':('philips',),'lg':('lg',),'aoc':('aoc',),
 'kingston':('kingston',),'sandisk':('sandisk',),
 'sony':('sony',),'apple':('apple','iphone','ipad','macbook','airpods'),
}

def has_term(text,word):
    return bool(re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',text))

def brand_ok(wanted,title):
    wanted=normalized(wanted);title=normalized(title)
    for brand,aliases in _BRANDS.items():
        if has_term(wanted,brand) and not any(has_term(title,w) for w in aliases):
            return False
    return True

def _model_edition(text,family):
    found=re.search(family,text)
    if not found:return None
    rest=text[found.end():].lstrip(' -_/')
    if family.startswith(r'\biphone'):
        found=re.match(r'(?:pro\s+max|pro|max|plus)\b',rest)
        return found.group(0) if found else 'base'
    if family.startswith(r'\b(?:geforce'):
        found=re.match(r'(?:ti\s+super|super|ti)\b',rest)
        return found.group(0) if found else 'base'
    return None

@lru_cache(maxsize=1)
def _catalog_model_names():
    """Map each catalog model to neighboring models in the SAME subcategory."""
    by_group={}
    catalog=Path(__file__).with_name('catalog.json')
    if not catalog.exists():return {}
    for row in json.loads(catalog.read_text(encoding='utf-8')):
        if row.get('kind')!='model':continue
        group=(row.get('category'),row.get('subcategory'))
        name=re.sub(r'[^a-z0-9]+',' ',normalized(row.get('name'))).strip()
        if name:by_group.setdefault(group,set()).add(name)
    lookup={}
    for names in by_group.values():
        siblings=tuple(sorted(names,key=len,reverse=True))
        for name in names:lookup[name]=siblings
    return lookup

def catalog_sibling_ok(wanted,title):
    """A longer *known* sibling model overrides its shorter name prefix.

    E.g. BlackShark V2 Pro must not be priced as BlackShark V2.
    """
    query=re.sub(r'[^a-z0-9]+',' ',normalized(canonical_query(wanted))).strip()
    possible=_catalog_model_names().get(query)
    if not possible:return True
    listed=re.sub(r'[^a-z0-9]+',' ',normalized(title)).strip()
    matches=[name for name in possible
             if re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',listed)]
    return not matches or max(matches,key=len)==query

def exact_model_ok(wanted,title):
    from product_codes import compare as compare_product_codes
    if not compare_product_codes(wanted, title)[0]:
        return False
    q=normalized(canonical_query(wanted));t=normalized(title)
    if not q or not t or not brand_ok(q,t) or not catalog_sibling_ok(wanted,title):return False
    # Exact variant tokens are part of the model, not optional description.
    editions=('pro','plus','ultra','max','super','ti','xt','tkl','mini','fe','ii')
    for edition in editions:
        if has_term(q,edition) and not has_term(t,edition):return False
    # A complete device must not be confused with a replacement part.
    accessory_re=r'\b(?:capa|capinha|pelicula|carregador|bateria|tela|display|protetor)\b'
    wanted_part=bool(re.match(r'^(?:kit\s+)?(?:capa|capinha|pelicula|carregador|cabo|bateria|tela|display|protetor|suporte)\b',q))
    part=re.search(accessory_re,t)
    if part and not wanted_part:
        device=re.search(r'\b(?:smartphone|celular|iphone|tablet)\b',t)
        if not device or part.start()<device.start():return False
    if re.search(r'\bsamsung\b',q) and re.search(r'\biphone\b',t):return False
    if re.search(r'\b(?:compativel\s+(?:com|ao)|para\s+(?:tenis|celular|iphone|samsung)|capa\s+para|cabo\s+para|refil|peca\s+de\s+reposicao)\b',t):return False
    if re.search(r'\b(?:replica|miniatura|fake|dummy|caixa\s+vazia|kit\s+de\s+pecas)\b',t):return False
    # 'Eleva Pro 2.0' is the sole technology of some genuine Corre 4 listings.
    # It does not identify the model: the exact 'Corre 4' phrase is required.
    # Corre 4, Corre Supra, Corre Grafeno 2 and Corre 4 Plus are
    # different shoes. Require the same explicit family and generation.
    family_pattern=r'\bcorre\s*(?:(grafeno|supra|turbo|plus)\s*)?([2-5])?(?![\w])'
    def corre_key(match,text):
        modifier=match.group(1) or ''
        generation=match.group(2) or ''
        tail=text[match.end():].strip()
        after=re.match(r'^(?:grafeno|supra|turbo|plus)\b',tail)
        if not modifier and after:modifier=after.group(0)
        return (modifier,generation)
    qfamilies=[corre_key(x,q) for x in re.finditer(family_pattern,q)]
    qfamilies=[x for x in qfamilies if x!=('','')]
    if qfamilies:
        tfound=list(re.finditer(family_pattern,t))
        tkeys=[corre_key(x,t) for x in tfound if corre_key(x,t)!=('','')]
        if not tkeys or any(x!=qfamilies[0] for x in tkeys):return False
        first=t.find('corre')
        other_first=re.search(r'\bolympikus\s+(?:eleva|dynamic|index|venus|flit)\b',t)
        if other_first and other_first.start()<first:return False
    for family in (r'\biphone\s*\d{1,2}\b',r'\b(?:geforce\s+)?rtx\s*\d{4}\b'):
        if not re.search(family,q):continue
        qm=_model_edition(q,family);tm=_model_edition(t,family)
        if tm is None or qm!=tm:return False
    model=re.search(r'\bg\s*(\d{3})\s*(x)?\b',q)
    if model and 'logitech' in q:
        nm=model.group(1)
        seen=re.search(r'\bg\s*'+nm+r'\s*(x)?\b',t)
        if not seen or bool(model.group(2))!=bool(seen.group(1)):return False
        if nm=='502' and model.group(2):
            qplus=bool(re.search(r'\bg502\s*x\s*plus\b',q))
            tplus=bool(re.search(r'\bg502\s*x\s*plus\b',t))
            if qplus!=tplus:return False
    if 'civic' in q:
        versions=('lxs','lxr','exr','exl','touring')
        qt=next((v for v in versions if has_term(q,v)),None)
        found={v for v in versions if has_term(t,v)}
        if qt and (qt not in found or len(found)!=1):return False
        if not qt and found:return False
        if has_term(q,'manual') and not has_term(t,'manual'):return False
        if has_term(q,'manual') and (has_term(t,'automatico') or has_term(t,'cvt')):return False
    if re.search(r'\bg502\s*x\b',q):
        qwire=has_term(q,'lightspeed')
        twire=has_term(t,'lightspeed') or has_term(t,'wireless') or has_term(t,'sem fio')
        if qwire!=twire:return False
    for unit in ('gb','tb','v'):
        requested={re.sub(r'\s+','',x) for x in re.findall(r'\b\d+\s*'+unit+r'\b',q)}
        found={re.sub(r'\s+','',x) for x in re.findall(r'\b\d+\s*'+unit+r'\b',t)}
        if requested and found and not requested.issubset(found):return False
    return True

def minimum_trusted_at(wanted):
    return QUALITY_REBASE_AT if str(wanted or '').strip() else None
