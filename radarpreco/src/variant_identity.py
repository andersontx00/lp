"""Category-aware variant identity; conservative by design.

A missing variant is NOT evidence of a match when the user explicitly
requested that variant. Never infer SKU, barcode, availability or a color.
"""
from __future__ import annotations
import re
import unicodedata

def norm(value):
    s=unicodedata.normalize('NFKD',str(value or '').lower())
    return ''.join(c for c in s if not unicodedata.combining(c))

COLORS={
 'preto':('preto','preta','black'),
 'branco':('branco','branca','white'),
 'azul':('azul','blue'),
 'vermelho':('vermelho','vermelha','red'),
 'verde':('verde','green'),
 'rosa':('rosa','pink'),
 'cinza':('cinza','gray','grey','grafite'),
 'dourado':('dourado','gold'),
 'prata':('prata','silver'),
}
def find_color(value):
    text=' '+norm(value)+' '
    for canonical,names in COLORS.items():
        if any(re.search(r'(?<!\w)'+re.escape(n)+r'(?!\w)',text) for n in names):
            return canonical
    return None

def category_hint(value):
    text=norm(value)
    if re.search(r'\b(?:tenis|calcado|olympikus|asics|nike\s+(?:pegasus|vomero)|adidas\s+(?:adizero|ultraboost))\b',text):return 'shoes'
    if re.search(r'\b(?:iphone|galaxy|smartphone|celular|redmi|poco|ipad|tablet)\b',text):return 'mobile'
    if re.search(r'\b(?:lava\s*loucas|geladeira|ar\s*condicionado|lavadora|forno|micro\s*ondas)\b',text):return 'appliance'
    return 'general'

def size_of(value):
    text=norm(value)
    m=re.search(r'\b(?:tamanho|tam\.?|numeracao|numero|num\.?|n\.)\s*(3[4-9]|4[0-8])\b',text)
    if m:return int(m.group(1))
    m=re.search(r'\b(?:br|eu)\s*(3[4-9]|4[0-8])\b',text)
    if m:return int(m.group(1))
    # Bare shoe sizes are ambiguous (e.g. 2024 release) unless at the end.
    m=re.search(r'\b(3[4-9]|4[0-8])\s*$',text)
    return int(m.group(1)) if m else None

def voltage_of(value):
    m=re.search(r'\b(110|127|220|240)\s*v(?:olts?)?\b',norm(value))
    return m.group(1)+'V' if m else None

def storage_of(value):
    values=[int(v) for v in re.findall(r'\b(32|64|128|256|512|1024)\s*gb\b',norm(value))]
    return str(max(values))+'GB' if values else None

def attributes(value,category=None):
    cat=category or category_hint(value)
    attrs={}
    color=find_color(value)
    if color:attrs['color']=color
    if cat=='shoes':
        size=size_of(value)
        if size:attrs['shoe_size']=str(size)
    if cat=='mobile':
        storage=storage_of(value)
        if storage:attrs['storage']=storage
    if cat=='appliance':
        voltage=voltage_of(value)
        if voltage:attrs['voltage']=voltage
    return attrs

def compare(request,title):
    cat=category_hint(request)
    requested=attributes(request,cat)
    actual=attributes(title,cat)
    for key,value in requested.items():
        if actual.get(key)!=value:
            return (False,'variante_ausente_ou_divergente_'+key,actual)
    return (True,'compatible',actual)

def key(request,title):
    cat=category_hint(request)
    values=attributes(title,cat)
    if not values:return 'sem-variante-informada'
    return '|'.join(k+'='+str(values[k]) for k in sorted(values))

def describe(request,title):
    a=attributes(title,category_hint(request))
    parts=[]
    if 'shoe_size' in a:parts.append('Tamanho '+a['shoe_size'])
    if 'storage' in a:parts.append(a['storage'])
    if 'voltage' in a:parts.append(a['voltage'])
    if 'color' in a:parts.append(a['color'].capitalize())
    return ' · '.join(parts) if parts else 'Variante não informada'
