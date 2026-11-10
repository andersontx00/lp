"""RadarPreço: optional Bright Data Google Shopping & Mercado Livre adapters.

No tokens are embedded in this source. Network requests target only fixed provider
hosts. Pending Bright Data jobs are resolved on later polls, never by fake prices.
"""
from __future__ import annotations
import json
import re
import unicodedata
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

BRIGHT_ORIGIN = 'https://api.brightdata.com/datasets/v3'
MELI_ORIGIN = 'https://api.mercadolibre.com'
DEFAULT_DATASET = 'gd_m31f2k0d2m1bah4f3b' # Google Shopping keyword listings; confirm schema on your account

def http_json(url, method='GET', payload=None, headers=None, timeout=14):
    if not (url.startswith(BRIGHT_ORIGIN + '/') or url.startswith(MELI_ORIGIN + '/')):
        raise ValueError('Domínio da API não autorizado')
    data=None if payload is None else json.dumps(payload).encode('utf-8')
    h={'Accept':'application/json','User-Agent':'RadarPreco/7.0'}
    if data is not None:h['Content-Type']='application/json'
    if headers:h.update(headers)
    req=Request(url,data=data,headers=h,method=method)
    with urlopen(req,timeout=timeout) as res:
        raw=res.read(4_000_001)
    if len(raw)>4_000_000:raise ValueError('Resposta da API excedeu o limite')
    return json.loads(raw)

def google_shopping_url(query):
    return 'https://www.google.com/search?'+urlencode({'udm':'28','q':query,'gl':'br','hl':'pt-BR'})

def trigger_bright(query, token, dataset_id=DEFAULT_DATASET, fetch=http_json):
    if not token:raise ValueError('Bright Data não configurada')
    if not re.fullmatch(r'gd_[A-Za-z0-9_]+',dataset_id):raise ValueError('Dataset Bright Data inválido')
    url=BRIGHT_ORIGIN+'/trigger?'+urlencode({'dataset_id':dataset_id,'format':'json'})
    result=fetch(url,method='POST',payload=[{'url':google_shopping_url(query),'country':'BR'}],headers={'Authorization':'Bearer '+token},timeout=17)
    snap=result.get('snapshot_id','') if isinstance(result,dict) else ''
    if not re.fullmatch(r'(?:s|sd)_[a-zA-Z0-9_-]{5,100}',str(snap)):
        raise ValueError('Bright Data não retornou ID de coleta')
    return snap

def read_bright(snapshot_id, token, fetch=http_json):
    if not re.fullmatch(r'(?:s|sd)_[a-zA-Z0-9_-]{5,100}',snapshot_id):raise ValueError('ID de coleta inválido')
    headers={'Authorization':'Bearer '+token}
    state=fetch(BRIGHT_ORIGIN+'/progress/'+snapshot_id,headers=headers,timeout=12)
    status=str(state.get('status','')) if isinstance(state,dict) else ''
    if status in ('running','starting','pending','queued'):
        return ('pending',[])
    if status in ('failed','error','cancelled'):
        return ('failed',[])
    if status!='ready':return ('pending',[])
    data=fetch(BRIGHT_ORIGIN+'/snapshot/'+snapshot_id+'?format=json',headers=headers,timeout=20)
    return ('ready',data)

def search_meli(query, token, fetch=http_json):
    """Official MLB listings search. Availability depends on app permissions (403 may occur)."""
    if not token:raise ValueError('Mercado Livre não configurado')
    url=MELI_ORIGIN+'/sites/MLB/search?'+urlencode({'q':query,'limit':'35'})
    return fetch(url,headers={'Authorization':'Bearer '+token},timeout=13)

def simplify(s):
    text=unicodedata.normalize('NFKD',str(s or ''))
    return ''.join(c for c in text if not unicodedata.combining(c)).lower()

def bright_rows(data):
    """Flatten documented/common Google Shopping dataset result variants safely."""
    result=[]
    def walk(obj,depth=0):
        if depth>5:return
        if isinstance(obj,list):
            for x in obj[:250]:walk(x,depth+1)
        elif isinstance(obj,dict):
            if any(k in obj for k in ('final_price','price','extracted_price','price_value','sale_price')) and any(k in obj for k in ('title','product_title','name','product_name')):
                result.append(obj)
            else:
                for field in ('results','products','shopping_results','offers','data','items','product_results'):
                    if field in obj:walk(obj[field],depth+1)
    walk(data)
    return result[:100]

def parse_bright(data, query, price_parser, matcher, url_checker, image_checker, timestamp):
    out=[];seen=set()
    for row in bright_rows(data):
        title=str(row.get('title') or row.get('product_title') or row.get('product_name') or row.get('name') or '')[:210]
        raw=row.get('final_price') or row.get('extracted_price') or row.get('price_value') or row.get('sale_price') or row.get('price')
        if isinstance(raw,dict):raw=raw.get('value') or raw.get('amount')
        p=price_parser(raw)
        merchant=row.get('seller') or row.get('source') or row.get('merchant') or row.get('store') or 'Google Shopping'
        if isinstance(merchant,dict):merchant=merchant.get('name') or merchant.get('seller_name') or 'Google Shopping'
        merchant=str(merchant)[:90]
        link=url_checker(row.get('product_url') or row.get('url') or row.get('link') or row.get('seller_url') or row.get('buy_url'))
        img=row.get('image') or row.get('image_url') or row.get('thumbnail') or row.get('product_image') or ''
        if isinstance(img,dict):img=img.get('url') or img.get('src') or ''
        if isinstance(img,list):img=img[0] if img else ''
        image=image_checker(img)
        currency=str(row.get('currency') or row.get('currency_code') or '').upper()
        if currency not in ('BRL','R$','REAL',''):continue
        if not currency and isinstance(raw,str) and '$' in raw and 'R$' not in raw:continue
        if not p or not title or matcher(query,title)<.72:continue
        if any(w in simplify(title) for w in ('usado','recondicionado','refurbished','seminovo','para pecas')):continue
        if not link:continue  # only verifiable original listings
        key=(simplify(title),simplify(merchant),p)
        if key in seen:continue
        seen.add(key)
        out.append({'title':title,'price':p,'source':merchant,'url':link,'image_url':image,
                    'provider':'brightdata','checked_at':timestamp()})
    return sorted(out,key=lambda x:x['price'])[:35]

def parse_meli(data,query,price_parser,matcher,url_checker,image_checker,timestamp):
    out=[];seen=set()
    for row in (data.get('results') or [])[:60] if isinstance(data,dict) else []:
        if not isinstance(row,dict):continue
        title=str(row.get('title') or '')[:210]
        price=price_parser(row.get('price'))
        if not price or matcher(query,title)<.72:continue
        if row.get('condition') not in (None,'new'):continue
        link=url_checker(row.get('permalink'))
        if not link or urlsplit(link).hostname not in ('www.mercadolivre.com.br','mercadolivre.com.br','produto.mercadolivre.com.br','articulo.mercadolibre.com.br'):continue
        seller=row.get('seller') or {}
        sellername=str(seller.get('nickname') or seller.get('id') or 'Marketplace')[:75] if isinstance(seller,dict) else 'Marketplace'
        merchant='Mercado Livre • '+sellername
        key=(str(row.get('id') or ''),price)
        if key in seen:continue
        seen.add(key)
        out.append({'title':title,'price':price,'source':merchant,'url':link,
                    'image_url':image_checker(row.get('thumbnail')),'provider':'mercadolivre','checked_at':timestamp()})
    return sorted(out,key=lambda x:x['price'])[:35]