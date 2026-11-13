"""Price-quality metadata and conservative offer presentation.

Never infer a Pix price, installment plan, GTIN identity, or delivered cost.
The identity gate is based on the existing strict model matcher and title evidence.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

MATCH_THRESHOLD = 0.90

def classify(query, title, score):
    """Return a publish/no-publish decision with an explainable label."""
    if not title or not isinstance(score, (float, int)) or score < MATCH_THRESHOLD:
        return {'status':'rejected', 'label':'Correspondência insuficiente',
                'basis':'A identificação por título não foi conclusiva.', 'score':round(float(score or 0),3),
                'gtin_verified':False}
    return {'status':'title_matched','label':'Modelo identificado no anúncio',
            'basis':'Nome, modelo e variantes comparados com o título; GTIN não validado.',
            'score':round(float(score),3),'gtin_verified':False}

def _money(raw, parser):
    if isinstance(raw,dict):
        raw=raw.get('amount',raw.get('value',raw.get('price')))
    if raw is None or isinstance(raw,bool):return None
    try:
        p=parser(raw)
        return round(float(p),2) if p is not None and 0<float(p)<100000000 else None
    except (TypeError,ValueError,OverflowError):
        return None

def pix_from_row(row, parser, regular):
    # Use explicit Pix fields only; generic 'discount' is not necessarily Pix.
    val=None
    for name in ('pix_price','price_pix','preco_pix','cash_pix_price'):
        if row.get(name) is not None:
            val=_money(row[name],parser)
            break
    if val is None and isinstance(row.get('payment_methods'),dict):
        methods=row['payment_methods']
        if isinstance(methods.get('pix'),dict):
            val=_money(methods['pix'].get('price'),parser)
    return val if val is not None and 0<val<=float(regular) else None

def installment_from_row(row, parser):
    raw=row.get('installments') or row.get('installment')
    if not isinstance(raw,dict):return ''
    try:
        n=int(raw.get('quantity') or raw.get('count') or raw.get('installments') or 0)
    except (TypeError,ValueError):return ''
    if not 2<=n<=36:return ''
    p=_money(raw.get('amount') or raw.get('installment_amount') or raw.get('price'),parser)
    if p is None:return ''
    total=p*n
    if total>100000000:return ''
    return f'{n}x de R$ {p:,.2f}'.replace(',','_').replace('.',',').replace('_','.')

def explicit_shipping(row,parser):
    raw=row.get('shipping_cost')
    if raw is None:raw=row.get('shipping_price')
    if raw is None and isinstance(row.get('shipping'),dict):
        obj=row['shipping']
        if obj.get('free_shipping') is True:return 0.0
        raw=obj.get('price') or obj.get('cost')
    if isinstance(raw,str) and re.search(r'\b(?:gratis|gratuito|free shipping)\b',raw.lower()):
        return 0.0
    if raw == 0 and raw is not None:return 0.0
    val=_money(raw,parser)
    return val

def enrich(offer):
    """Clean and return the available offer metadata without guessing."""
    o=dict(offer)
    ship=o.get('shipping_price')
    pix=o.get('pix_price')
    price=o.get('price')
    try:
        p=float(price)
        ship=float(ship) if ship is not None else None
        pix=float(pix) if pix is not None else None
    except (ValueError,TypeError,OverflowError):
        ship,pix=None,None
    o['shipping_price']=round(ship,2) if ship is not None and ship>=0 else None
    o['pix_price']=round(pix,2) if pix is not None and 0<pix<=p else None
    o['installments']=str(o.get('installments') or '')[:90]
    o['total_known']=round(p+ship,2) if ship is not None else None
    o['pix_total_known']=round((pix if pix is not None else p)+ship,2) if ship is not None else None
    o['quality_status']='title_matched'
    o['quality_label']='Modelo identificado pelo título'
    o['gtin_verified']=False
    o['shipping_known']=ship is not None
    return o

def audit_summary(offers):
    accepted=len(offers)
    with_shipping=sum(1 for x in offers if x.get('shipping_price') is not None)
    with_pix=sum(1 for x in offers if x.get('pix_price') is not None)
    return {'title_checked':accepted,'gtin_confirmed':0,'shipping_known':with_shipping,
            'pix_known':with_pix,'method':'Modelo e versão conferidos pelo título; GTIN não validado.'}

def fresh_label(checked_at,now=None):
    now=now or datetime.now(timezone.utc)
    try:
        stamp=datetime.fromisoformat(str(checked_at).replace('Z','+00:00'))
        if stamp.tzinfo is None:return 'Sem horário confirmado'
        age=(now-stamp).total_seconds()
    except (ValueError,TypeError):return 'Sem horário confirmado'
    if age<0:return 'Horário sob revisão'
    if age<=6*3600:return 'Atualizado recentemente'
    if age<=24*3600:return 'Cotação de hoje'
    return 'Cotação antiga'
