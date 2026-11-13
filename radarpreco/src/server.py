#!/usr/bin/env python3
"""RadarPreço 7.0 — two-source search with provenance and asynchronous updates.

Only real source readings are stored. The API cannot invent unavailable
historical values. For a public hosted service add authentication and robust
rate limits, persistent disk/database, schedule worker, and protection.
"""
from __future__ import annotations
import json, os, re, sqlite3, threading, time, unicodedata, hashlib, sys
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed
import providers
import alerts_engine
import accounts, variant_rules, pg_store, offer_quality, product_identity, price_assurance, variant_identity, audit_runtime
ROOT=Path(__file__).resolve().parent


def load_dotenv():
    f=ROOT/'.env'
    if not f.is_file():return
    for line in f.read_text(encoding='utf-8').splitlines():
        s=line.strip()
        if not s or s.startswith('#') or '=' not in s:continue
        k,v=s.split('=',1)
        if re.fullmatch(r'[A-Z_][A-Z0-9_]*',k.strip()):os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
load_dotenv()
SERPAPI_KEY=os.environ.get('SERPAPI_KEY','').strip()
KEEPA_KEY=os.environ.get('KEEPA_KEY','').strip()
BRIGHTDATA_API_TOKEN=os.environ.get('BRIGHTDATA_API_TOKEN','').strip()
BRIGHTDATA_DATASET_ID=os.environ.get('BRIGHTDATA_DATASET_ID',providers.DEFAULT_DATASET).strip()
MELI_ACCESS_TOKEN=os.environ.get('MELI_ACCESS_TOKEN','').strip()
PORT=int(os.environ.get('PORT','8765'))
HOST=os.environ.get('HOST','127.0.0.1')
CACHE_SECONDS=max(180,int(os.environ.get('CACHE_SECONDS','3600')))
MAX_LOOKUPS_PER_DAY=max(1,int(os.environ.get('MAX_LOOKUPS_PER_DAY','20')))
MAX_LOOKUPS_PER_MONTH=max(1,int(os.environ.get('MAX_LOOKUPS_PER_MONTH','200')))
DB_PATH=Path(os.environ.get('RADAR_DB_PATH',str(ROOT/'radar_historico.sqlite3')))
DATABASE_URL=os.environ.get('DATABASE_URL','').strip()
USE_POSTGRES=bool(DATABASE_URL)
lock=threading.RLock(); last_fetch={}; requests_per_min=deque(maxlen=50)
auth_attempts={}


def timestamp():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def clean_query(q):
    q=str(q).strip()
    if len(q)<2 or len(q)>160 or any(ord(c)<32 for c in q):raise ValueError('Nome deve ter entre 2 e 160 caracteres.')
    return product_identity.canonical_query(q)

def quality_cutoff(name):
    """Exclude legacy unverified snapshots across all product categories."""
    starts=[offer_quality.minimum_trusted_at(name),product_identity.minimum_trusted_at(name)]
    return max((x for x in starts if x),default=None)

def normalize(s):
    s=unicodedata.normalize('NFKD',str(s or ''))
    return ''.join(c for c in s if not unicodedata.combining(c)).lower()

def numeric_price(raw):
    if isinstance(raw,bool):return None
    if isinstance(raw,(int,float)):value=float(raw)
    elif isinstance(raw,str):
        s=raw.replace('R$','').replace('\xa0','').replace(' ','')
        s=re.sub(r'[^0-9,.\-]','',s)
        if ',' in s and '.' in s:s=s.replace('.','').replace(',','.') if s.rfind(',')>s.rfind('.') else s.replace(',','')
        elif ',' in s:s=s.replace(',','.')
        elif s.count('.')==1 and re.fullmatch(r'\d{1,3}\.\d{3}',s):s=s.replace('.','')
        try:value=float(s)
        except (TypeError,ValueError):return None
    else:return None
    if value!=value or value<0.01 or value>100_000_000 or value==float('inf'):return None
    return round(value,2)

def safe_url(u):
    u=str(u or '').strip()[:1600]
    p=urlsplit(u)
    return u if p.scheme=='https' and p.hostname else ''

def image_url(raw):
    """Only remote HTTPS images, never local paths/data URLs."""
    u=safe_url(raw)
    if not u:return ''
    p=urlsplit(u)
    if p.hostname in ('localhost','127.0.0.1','0.0.0.0') or p.hostname.endswith('.local'):return ''
    return u

def words(raw):
    s=normalize(raw)
    s=re.sub(r'(?<=\d)\s+(?=(?:gb|tb|v|hz|w|l)\b)', '',s)
    s=re.sub(r'\bs(2[0-9])\s*(fe|ultra|plus)\b',r's\1 \2',s)
    s=re.sub(r'\b(\d+)\s*servi[cç]os?\b',r'\1servicos',s)
    s=re.sub(r'\bcorre(?=[345]\b)', 'corre ', s)
    return re.findall(r'[a-z0-9]+',s)

def match_score(wanted,candidate):
    """Match title identity, required model and capacity; never cross FE/Ultra or 128/256GB."""
    wanted=product_identity.canonical_query(wanted)
    a=words(wanted);b=words(candidate)
    if (not a or not b or not variant_identity.compare(wanted,candidate)[0] or variant_rules.incompatible(wanted,candidate)
            or not offer_quality.device_offer_ok(wanted,candidate)
            or not product_identity.exact_model_ok(wanted,candidate)):
        return 0.0
    sa,sb=set(a),set(b)
    # Every model number in the requested query must be present (accept 220 V / 220V).
    required=[t for t in a if re.search(r'\d',t)]
    if any(t not in sb for t in required):return 0.0
    # If a storage/capacity is explicitly requested, reject conflicting offers.
    sizes=lambda ss:set(t for t in ss if re.fullmatch(r'\d+(?:gb|tb)',t))
    if sizes(sa) and sizes(sb) and not sizes(sa).intersection(sizes(sb)):return 0.0
    # Reject another member of the same flagship family.
    if any(t.startswith('s2') and re.fullmatch(r's2[0-9]',t) for t in sa):
        for edition in ('fe','ultra','plus'):
            if (edition in sa)!=(edition in sb):return 0.0
    # Don't accidentally match a different 220V/127V version.
    volts=lambda ss:set(t for t in ss if re.fullmatch(r'\d{3}v',t))
    if volts(sa) and volts(sb) and not volts(sa).intersection(volts(sb)):return 0.0
    ignore={'de','do','da','para','com','e','em','the','a','o','pro','nova','novo','oficial','mouse','teclado','fone','smartphone','celular','notebook','produto','polegadas','preto','branco','5g','wifi'}
    use=[t for t in a if t not in ignore]
    if not use:use=a
    # For generic searches, exact model digits + matching key terms give a good match.
    return sum(t in sb for t in use)/len(use)

def shipping_cost(row):
    """None means shipping cost unknown. Zero only if clearly advertised free."""
    raw=row.get('shipping') or row.get('delivery') or row.get('shipping_cost')
    if isinstance(raw,dict):raw=raw.get('price') or raw.get('cost') or raw.get('label')
    if isinstance(raw,(int,float)):return numeric_price(raw) if raw!=0 else 0.0
    if not raw:return None
    val=normalize(str(raw))
    if any(x in val for x in ('frete gratis','frete gratuito','free shipping','entrega gratis')):return 0.0
    if 'r$' not in str(raw).lower():return None
    return numeric_price(str(raw))

def extract_offers(response,wanted):
    """Read Shopping formats; keep photos and validate prices/product variants."""
    groups=[]
    groups.extend(response.get('shopping_results') or [])
    groups.extend(response.get('inline_shopping_results') or [])
    for section in response.get('categorized_shopping_results') or []:
        if isinstance(section,dict):groups.extend(section.get('shopping_results') or section.get('results') or [])
    arr=[];seen=set()
    for row in groups:
        if not isinstance(row,dict):continue
        title=str(row.get('title') or '').strip()[:200]
        price=numeric_price(row.get('extracted_price')) or numeric_price(row.get('price'))
        url=safe_url(row.get('link') or row.get('product_link'))
        if not title or price is None or not url:continue
        score=match_score(wanted,title)
        if price_assurance.classify(wanted,title,score)['status']=='rejected':continue
        txt=normalize(title+' '+str(row.get('second_hand_condition') or '')+' '+str(row.get('price') or ''))
        if any(s in txt for s in ('usado','seminovo','recondicionado','refurbished','entrada +','mensalidade','aluguel','/mes','/mês','/mo','por mes','por mês')):continue
        if row.get('installment') and re.search(r'(?:/|\bpor\s+)(?:mo|mes|m[eê]s)\b',str(row.get('price','')),re.I):continue
        source=str(row.get('source') or 'Google Shopping')[:100].strip()
        if not source:source='Google Shopping'
        thumbs=row.get('thumbnails') or []
        thumb=image_url(row.get('thumbnail') or (thumbs[0] if isinstance(thumbs,list) and thumbs else ''))
        key=(normalize(title),normalize(source),price)
        if key in seen:continue
        seen.add(key)
        arr.append({'title':title,'price':price,'source':source,'url':url,'image_url':thumb,'shipping_price':shipping_cost(row),
            'pix_price':price_assurance.pix_from_row(row,numeric_price,price),
            'installments':price_assurance.installment_from_row(row,numeric_price),
            'product_id':str(row.get('product_id') or '')[:60],
            'checked_at':timestamp(),'match_score':round(score,3)})
    arr.sort(key=lambda x:x['price'])
    return arr[:40]

def api_json(url,opener=None):
    r=Request(url,headers={'User-Agent':'RadarPreco/6.0','Accept':'application/json','Accept-Encoding':'identity'})
    opener=opener or urlopen
    with opener(r,timeout=23) as res:raw=res.read(3_000_001)
    if len(raw)>3_000_000:raise ValueError('Resposta maior que o limite permitido.')
    data=json.loads(raw)
    if not isinstance(data,dict):raise ValueError('Resposta inesperada do provedor.')
    if data.get('error'):raise ValueError('Provedor informou erro: '+str(data['error'])[:160])
    return data

def shopping_search(name,key=None,fetch=None):
    key=key or SERPAPI_KEY
    if not key:raise ValueError('SerpApi não configurada')
    params=urlencode({'engine':'google_shopping','q':name,'gl':'br','hl':'pt-br','api_key':key})
    data=(fetch or api_json)('https://serpapi.com/search.json?'+params)
    return extract_offers(data,name)

def parse_google_date(raw):
    val=str(raw or '').strip()
    if not val:return None
    try:
        d=datetime.fromisoformat(val.replace('Z','+00:00'))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except ValueError:pass
    try:
        d=parsedate_to_datetime(val)
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except (ValueError,TypeError):pass
    for fmt in ('%b %d, %Y','%B %d, %Y','%d %b %Y','%Y-%m-%d'):
        try:return datetime.strptime(val,fmt).replace(hour=12,tzinfo=timezone.utc)
        except ValueError:pass
    return None

def google_price_history(body):
    product=body.get('product_results') or body.get('product') or body
    insights=product.get('price_insights') or body.get('price_insights') or {}
    if insights.get('currency','').upper() not in ('BRL','R$'):return []
    points=[]
    for store in insights.get('stores') or []:
        seller=str(store.get('name') or '').strip()[:90]
        if not seller:continue
        for record in store.get('prices') or []:
            price=numeric_price(record.get('price'))
            date=parse_google_date(record.get('date'))
            if price is None or not date or date>datetime.now(timezone.utc)+timedelta(days=2):continue
            points.append({'series':'Loja • '+seller,'price':price,'at':date.isoformat(timespec='seconds'),
                           'source':'Google Product (histórico por loja)','url':'','kind':'historical'})
    return points

def load_google_history(product_id,key=None,fetch=None):
    key=key or SERPAPI_KEY
    if not key or not re.fullmatch('[0-9]{5,30}',str(product_id)):return []
    params=urlencode({'engine':'google_product','product_id':product_id,'gl':'br','hl':'pt-br','api_key':key})
    return google_price_history((fetch or api_json)('https://serpapi.com/search.json?'+params))

def keepa_csv_points(product):
    # Keepa csv type 1 = marketplace new, times are minutes since 2011-01-01; values cents.
    csv=product.get('csv') or []
    if not isinstance(csv,list) or len(csv)<2 or not isinstance(csv[1],list):return []
    pairs=csv[1]
    now=datetime.now(timezone.utc)
    result=[]
    for j in range(0,len(pairs)-1,2):
        k,v=pairs[j:j+2]
        try:
            n=float(v);at=datetime.fromtimestamp((int(k)+21564000)*60,timezone.utc)
        except (TypeError,ValueError,OverflowError,OSError):continue
        if n<=0 or at>now+timedelta(days=2) or at<now-timedelta(days=400):continue
        result.append({'series':'Amazon BR • menor novo','price':round(n/100,2),'at':at.isoformat(timespec='seconds'),
                       'source':'Keepa / Amazon BR','url':'https://www.amazon.com.br/dp/'+str(product.get('asin') or ''),'kind':'historical'})
    # Keep the last event on each day, and one point before this 365-day window if present.
    daily={x['at'][:10]:x for x in result}
    return [daily[k] for k in sorted(daily)]

def load_keepa_history(name,key=None,fetch=None):
    key=key or KEEPA_KEY
    if not key:return []
    params=urlencode({'key':key,'domain':'12','type':'product','term':name,'history':'1'})
    body=(fetch or api_json)('https://api.keepa.com/search?'+params)
    matches=[p for p in body.get('products') or [] if match_score(name,str(p.get('title') or ''))>=0.75]
    if not matches:return []
    # A single matching ASIN avoids confusing similar configurations.
    return keepa_csv_points(matches[0])

@contextmanager
def db_conn():
    if USE_POSTGRES:
        cn=pg_store.connect(DATABASE_URL)
    else:
        cn=sqlite3.connect(DB_PATH,timeout=15)
        cn.row_factory=sqlite3.Row
    try:
        with cn:
            yield cn
    finally:
        cn.close()

def initialize_variants_and_audit(cn):
    """Preserve old offers and migrate valid rows to variant-aware storage."""
    cn.execute('''CREATE TABLE IF NOT EXISTS offer_variants (
        product TEXT NOT NULL, merchant TEXT NOT NULL, variant_key TEXT NOT NULL,
        title TEXT NOT NULL, price DOUBLE PRECISION NOT NULL, url TEXT NOT NULL,
        checked_at TEXT NOT NULL, image_url TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT 'serpapi',
        shipping_price DOUBLE PRECISION, pix_price DOUBLE PRECISION,
        installments TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(product,merchant,variant_key))''')
    cn.execute('CREATE INDEX IF NOT EXISTS by_variant_date ON offer_variants(product,checked_at)')
    audit_runtime.init_schema(cn,postgres=USE_POSTGRES)
    existing=cn.execute('SELECT COUNT(*) FROM offer_variants').fetchone()[0]
    if not existing:
        legacy=cn.execute('''SELECT product,merchant,title,price,url,checked_at,
                image_url,provider,shipping_price,pix_price,installments
                FROM offers LIMIT 1000''').fetchall()
        for record in legacy:
            row=dict(record)
            title=str(row.get('title') or '')
            name=str(row.get('product') or '')
            if price_assurance.classify(name,title,match_score(name,title))['status']=='rejected':continue
            key=variant_identity.key(name,title)
            cn.execute('''INSERT OR IGNORE INTO offer_variants
                (product,merchant,variant_key,title,price,url,checked_at,image_url,
                 provider,shipping_price,pix_price,installments)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (name,row['merchant'],key,title,row['price'],row['url'],
                 row['checked_at'],row['image_url'],row['provider'],
                 row['shipping_price'],row['pix_price'],row['installments']))


def initialize():
    if USE_POSTGRES:
        with lock,db_conn() as cn:
            pg_store.initialize(cn)
            alerts_engine.init_schema(cn,postgres=True)
            accounts.init_schema(cn,postgres=True)
            cn.execute('ALTER TABLE offers ADD COLUMN IF NOT EXISTS pix_price DOUBLE PRECISION')
            cn.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS installments TEXT NOT NULL DEFAULT ''")
            cn.execute('''CREATE TABLE IF NOT EXISTS offer_reports (
                id BIGSERIAL PRIMARY KEY,product TEXT NOT NULL,merchant TEXT NOT NULL,
                title TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
                reporter TEXT NOT NULL)''')
            cn.execute('CREATE INDEX IF NOT EXISTS offer_reports_at ON offer_reports(created_at)')
            initialize_variants_and_audit(cn)
        return
    with lock,db_conn() as cn:
        cn.execute('''CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product TEXT NOT NULL, series TEXT NOT NULL, price REAL NOT NULL,
            at TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL CHECK(kind IN ('snapshot','historical')),
            UNIQUE(product,series,at,kind))''')
        cn.execute('CREATE INDEX IF NOT EXISTS by_product_date ON prices(product,at)')
        cn.execute('''CREATE TABLE IF NOT EXISTS offers (
          product TEXT NOT NULL, merchant TEXT NOT NULL, title TEXT NOT NULL,
          price REAL NOT NULL, url TEXT NOT NULL, checked_at TEXT NOT NULL,
          image_url TEXT NOT NULL DEFAULT '', PRIMARY KEY (product, merchant))''')
        cols={r[1] for r in cn.execute('PRAGMA table_info(offers)').fetchall()}
        if 'image_url' not in cols:cn.execute("ALTER TABLE offers ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        if 'provider' not in cols:cn.execute("ALTER TABLE offers ADD COLUMN provider TEXT NOT NULL DEFAULT 'serpapi'")
        cn.execute('''CREATE TABLE IF NOT EXISTS bright_jobs (
          product TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, created_at TEXT NOT NULL,
          checked_at TEXT NOT NULL DEFAULT '')''')
        cn.execute('''CREATE TABLE IF NOT EXISTS lookup_quota (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, product TEXT NOT NULL)''')
        cn.execute('CREATE INDEX IF NOT EXISTS lookup_quota_at ON lookup_quota(at)')
        if 'client_key' not in {r[1] for r in cn.execute('PRAGMA table_info(lookup_quota)').fetchall()}:
            cn.execute("ALTER TABLE lookup_quota ADD COLUMN client_key TEXT NOT NULL DEFAULT ''")
        alerts_engine.init_schema(cn)
        accounts.init_schema(cn)
        if 'shipping_price' not in {r[1] for r in cn.execute('PRAGMA table_info(offers)').fetchall()}:
            cn.execute('ALTER TABLE offers ADD COLUMN shipping_price REAL')
        columns={r[1] for r in cn.execute('PRAGMA table_info(offers)').fetchall()}
        if 'pix_price' not in columns:cn.execute('ALTER TABLE offers ADD COLUMN pix_price REAL')
        if 'installments' not in columns:cn.execute("ALTER TABLE offers ADD COLUMN installments TEXT NOT NULL DEFAULT ''")
        cn.execute('''CREATE TABLE IF NOT EXISTS offer_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product TEXT NOT NULL,
            merchant TEXT NOT NULL,title TEXT NOT NULL,reason TEXT NOT NULL,
            created_at TEXT NOT NULL,reporter TEXT NOT NULL)''')
        cn.execute('CREATE INDEX IF NOT EXISTS offer_reports_at ON offer_reports(created_at)')
        initialize_variants_and_audit(cn)

def store_offer_cache(name, items, at=None):
    """Keep lowest comparable offers per merchant AND variant."""
    stamp=at or timestamp()
    grouped={}
    for item in items:
        merchant=str(item.get('source') or '').strip()[:100]
        price=numeric_price(item.get('price'))
        if not merchant or not price:continue
        title=str(item.get('title') or '')
        if price_assurance.classify(name,title,match_score(name,title))['status']=='rejected':continue
        variant_key=variant_identity.key(name,title)
        key=normalize(merchant)+'|'+variant_key
        if key not in grouped or price<grouped[key]['price']:
            grouped[key]=price_assurance.enrich({**item,'source':merchant,'price':price,
                'checked_at':str(item.get('checked_at') or stamp),
                'variant_key':variant_key,
                'variant_label':variant_identity.describe(name,title)})
    with lock,db_conn() as cn:
        cn.execute('DELETE FROM offer_variants WHERE product=?',(name,))
        for item in grouped.values():
            cn.execute('''INSERT INTO offer_variants
                (product,merchant,variant_key,title,price,url,checked_at,
                 image_url,provider,shipping_price,pix_price,installments)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (name,item['source'],item['variant_key'],str(item.get('title',''))[:200],
                 item['price'],safe_url(item.get('url')),str(item.get('checked_at') or stamp),
                 image_url(item.get('image_url')),str(item.get('provider') or 'serpapi')[:30],
                 item.get('shipping_price'),item.get('pix_price'),item.get('installments','')))
    return list(sorted(grouped.values(),key=lambda x:x['price']))


def read_offers(name):
    with lock,db_conn() as cn:
        rows=cn.execute('SELECT merchant AS source,title,price,url,checked_at,image_url,provider,shipping_price,pix_price,installments,variant_key FROM offer_variants WHERE product=? AND checked_at>=? ORDER BY price ASC LIMIT 50',(name,(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec='seconds'))).fetchall()
    results=[]
    trusted=quality_cutoff(name)
    for row in rows:
        item=dict(row)
        if trusted and item['checked_at']<trusted:continue
        title=item.get('title') or ''
        if price_assurance.classify(name,title,match_score(name,title))['status']=='rejected':continue
        item['variant_label']=variant_identity.describe(name,title)
        results.append(price_assurance.enrich(item))
    return results


def store_points(name,points):
    if not points:return 0
    with lock,db_conn() as cn:
        changes=0
        for x in points:
            try:
                if x['price']<=0:continue
                cur=cn.execute('''INSERT OR IGNORE INTO prices(product,series,price,at,source,url,kind)
                  VALUES (?,?,?,?,?,?,?)''',(name,x['series'],x['price'],x['at'],x['source'],x.get('url',''),x['kind']))
                changes+=cur.rowcount
            except (KeyError,TypeError,ValueError):continue
        return changes

def read_history(name,days=7):
    # Same source series; don't confuse min prices from different stores.
    if days not in ('7','30','90','180','365','all'):days='7'
    since=(datetime.now(timezone.utc)-timedelta(days=int(days))).isoformat(timespec='seconds') if days!='all' else '2000-01-01T00:00:00+00:00'
    quality_start=quality_cutoff(name)
    if quality_start:since=max(since,quality_start)
    with lock,db_conn() as cn:
        rows=cn.execute('''SELECT series,price,at,source,url,kind FROM prices
             WHERE product=? AND at >= ? ORDER BY at ASC LIMIT 4000''',(name,since)).fetchall()
        # The highest timestamp is not necessarily the cheapest offer.
        # Derive the last valid reading from audited, variant-qualified rows below.
        latest=None
    # Old legacy snapshots and generic provider histories have no variant key.
    # Preserve them in storage for audit, but not in trusted charts or alerts.
    rows=[row for row in rows
        if row['kind']=='snapshot' and str(row['series']).count(' • ')>=2]
    latest=max(rows,key=lambda row:str(row['at']),default=None)
    offers=read_offers(name)
    variants=sorted({o.get('variant_key') or 'sem-variante-informada' for o in offers})
    with lock,db_conn() as cn:
        if len(variants)>1:
            insight={'ready':False,'reason':'mixed_variants','label':'Selecione uma variante',
                     'sample_days':0,'current':None,'variants':variants}
        else:
            insight=alerts_engine.opportunity(cn,name,offers,minimum_at=quality_start,
                variant_key=(variants[0] if variants else None))
        audit=audit_runtime.summary(cn)
    return {'query':name,'days':days,'points':[dict(row) for row in rows],'insight':insight,
           'latest':dict(latest) if latest else None,'offers':offers,'checked_at':timestamp(),'storage':'postgres' if USE_POSTGRES else 'temporary_sqlite','image_url':next((o.get('image_url','') for o in offers if o.get('image_url')),''),
            'audit':{**price_assurance.audit_summary(offers),'recent_audit':audit},
            'variants':[{'key':key,'label':next((o.get('variant_label') for o in offers if o.get('variant_key')==key),key)}
                         for key in variants],
            'history_days':len(set(str(r['at'])[:10] for r in rows))}

def job_get(name):
    with lock, db_conn() as cn:
        row=cn.execute('SELECT snapshot_id,created_at,checked_at FROM bright_jobs WHERE product=?',(name,)).fetchone()
    return dict(row) if row else None

def job_set(name,snapshot_id):
    with lock,db_conn() as cn:
        cn.execute('INSERT OR REPLACE INTO bright_jobs(product,snapshot_id,created_at,checked_at) VALUES(?,?,?,?)',
                   (name,snapshot_id,timestamp(),''))

def job_clear(name):
    with lock,db_conn() as cn:cn.execute('DELETE FROM bright_jobs WHERE product=?',(name,))

def job_mark_checked(name):
    with lock,db_conn() as cn:cn.execute('UPDATE bright_jobs SET checked_at=? WHERE product=?',(timestamp(),name))

def record_merged(name,new_offers,stamp=None):
    """Validate, audit and quarantine candidates before storing prices."""
    stamp=stamp or timestamp()
    previous=read_offers(name)
    accepted=[]
    with lock,db_conn() as cn:
        for offer in new_offers:
            o=dict(offer)
            title=str(o.get('title') or '')
            score=match_score(name,title)
            classification=price_assurance.classify(name,title,score)
            variant=variant_identity.key(name,title)
            o['variant_key']=variant
            o['variant_label']=variant_identity.describe(name,title)
            if classification['status']=='rejected':
                audit_runtime.record_audit(cn,name,o,'rejected','identidade_ou_variante_incompativel',variant)
                continue
            reason=audit_runtime.outlier(o,previous)
            if reason is None:
                rows=cn.execute("""SELECT series,price,at FROM prices
                    WHERE product=? AND kind='snapshot' AND at>=?
                    ORDER BY at DESC LIMIT 150""",
                    (name,quality_cutoff(name) or '2000-01-01')).fetchall()
                prior={}
                for item in rows:
                    if not str(item['series']).endswith(' • '+variant):continue
                    day=str(item['at'])[:10]
                    prior[day]=min(prior.get(day,float('inf')),float(item['price']))
                if len(prior)>=3:
                    baseline=sorted(prior.values())[len(prior)//2]
                    if float(o.get('price') or 0)<baseline*.38:
                        reason='preco_muito_abaixo_do_historico_comparavel'
            status='pending_review' if reason else 'title_matched'
            audit_runtime.record_audit(cn,name,o,status,reason or 'identidade_por_titulo',variant)
            if not reason:accepted.append(o)
    if not accepted:return read_offers(name)
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec='seconds')
    old=[o for o in previous if str(o.get('checked_at') or '')>=cutoff]
    refreshed={(normalize(str(o.get('source') or '')),o['variant_key']) for o in accepted}
    remaining=[o for o in old
        if (normalize(str(o.get('source') or '')),o.get('variant_key')) not in refreshed]
    store_offer_cache(name,remaining+accepted,stamp)
    with lock,db_conn() as cn:
        alerts_engine.store_drop_alerts(cn,name,accepted,stamp,minimum_at=quality_cutoff(name))
    store_points(name,[{'series':'Loja • '+o['source']+' • '+o['variant_key'],
         'price':o['price'],'at':str(o.get('checked_at') or stamp),
         'source':str(o.get('provider') or o['source']),'url':o['url'],
         'kind':'snapshot'} for o in accepted if o.get('price')])
    return read_offers(name)

def metered_provider_call(source,name,call,*args):
    """Count API requests, not estimated invoices, with source-specific timing."""
    start=time.monotonic()
    status='ok'
    returned=0
    try:
        result=call(*args)
        if isinstance(result,list):returned=len(result)
        if isinstance(result,dict):returned=len(result.get('results') or result.get('shopping_results') or [])
        return result
    except Exception:
        status='error'
        raise
    finally:
        elapsed=int((time.monotonic()-start)*1000)
        try:
            with lock,db_conn() as cn:
                audit_runtime.log_provider(cn,source,status,name,elapsed,
                    0 if source=='brightdata-poll' else 1,returned)
        except Exception as exc:
            print('PROVIDER_METRICS_ERROR type='+type(exc).__name__,flush=True)


def configured():
    return bool(BRIGHTDATA_API_TOKEN or MELI_ACCESS_TOKEN or SERPAPI_KEY or KEEPA_KEY)

def poll_bright(name):
    """Returns (state,offers); checks an already-triggered Bright Data collection."""
    job=job_get(name)
    if not job:return ('absent',[])
    checked=job.get('checked_at','')
    if checked:
        try:
            delta=(datetime.now(timezone.utc)-datetime.fromisoformat(checked)).total_seconds()
            if delta<8:return ('pending',[])
        except ValueError:pass
    created=datetime.fromisoformat(job['created_at'])
    if datetime.now(timezone.utc)-created>timedelta(hours=2):
        job_clear(name);return ('expired',[])
    job_mark_checked(name)
    state,body=metered_provider_call('brightdata-poll',name,providers.read_bright,job['snapshot_id'],BRIGHTDATA_API_TOKEN)
    if state=='pending':return ('pending',[])
    job_clear(name)
    if state=='ready':
        offers=providers.parse_bright(body,name,numeric_price,match_score,safe_url,image_url,timestamp)
        return ('ready',offers)
    return (state,[])

def enforce_lookup_quota(name,owner='anonymous'):
    # Every *new search* is counted; background polling is not.
    utc=datetime.now(timezone.utc)
    day=utc.date().isoformat()
    month=day[:7]
    with lock,db_conn() as cn:
        n_day=cn.execute('SELECT COUNT(*) FROM lookup_quota WHERE at>=?',(day,)).fetchone()[0]
        n_month=cn.execute('SELECT COUNT(*) FROM lookup_quota WHERE at>=?',(month+'-01',)).fetchone()[0]
        if n_day>=MAX_LOOKUPS_PER_DAY or n_month>=MAX_LOOKUPS_PER_MONTH:
            raise ValueError('Limite preventivo de consultas atingido. Tente mais tarde.')
        n_user=cn.execute('SELECT COUNT(*) FROM lookup_quota WHERE client_key=? AND at>=?',(owner,day)).fetchone()[0]
        if n_user>=6 and owner!='automatic':
            raise ValueError('Limite diário deste acesso atingido. Tente amanhã.')
        cn.execute('INSERT INTO lookup_quota(at,product,client_key) VALUES (?,?,?)',(timestamp(),name,owner))

def perform_refresh(name,owner='anonymous'):
    """One product request triggers Bright Data and Mercado Livre independently.

    Bright Data is an async dataset job; frontend polls /api/refresh at least
    eight seconds apart. Mercado Livre can return 403 for some integrations;
    failure is isolated, not reported as a successful price result.
    """
    if not configured():return {'error':'Configure Bright Data, Mercado Livre ou SerpApi no servidor.',
        'offline':True,'offers':read_offers(name),'provider_configured':False,
        'pending':False,'sources':[]}
    out={'query':name,'offers':read_offers(name),'errors':[],'checked_at':timestamp(),
         'cached':False,'provider_configured':True,'pending':False,'poll_after_seconds':8,'sources':[]}
    now=time.monotonic()
    # Existing Bright Data job takes precedence; never trigger a duplicate billable job.
    job=job_get(name) if BRIGHTDATA_API_TOKEN else None
    if job:
        try:
            state,found=poll_bright(name)
            out['pending']=state=='pending'
            out['sources'].append({'name':'Bright Data','status':state})
            if found:out['offers']=record_merged(name,found,out['checked_at'])
            elif state in ('failed','expired'):out['errors'].append('Bright Data: coleta indisponível.')
        except HTTPError as err:
            if err.code in (400,401,403,404):
                job_clear(name)
                out['sources'].append({'name':'Bright Data','status':'unavailable'})
                out['errors'].append('Bright Data: acesso negado ou dataset inválido.')
            else:
                out['pending']=True
                out['sources'].append({'name':'Bright Data','status':'retry'})
                out['errors'].append('Bright Data: consulta será tentada novamente.')
        except (ValueError,URLError,TimeoutError,OSError):
            out['pending']=True
            out['sources'].append({'name':'Bright Data','status':'retry'})
            out['errors'].append('Bright Data: aguardando nova tentativa.')
        return out
    fresh=read_offers(name)
    if fresh:
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(max(x['checked_at'] for x in fresh))).total_seconds()
            if 0<=age<CACHE_SECONDS:
                out['cached']=True;out['sources'].append({'name':'Cache','status':'recent'});return out
        except (ValueError,TypeError):pass
    with lock:
        stamp=last_fetch.get(name.casefold())
        cached_offers=read_offers(name)
        recent=[alerts_engine.parse_at(x.get('checked_at')) for x in cached_offers]
        newest=max((x for x in recent if x is not None),default=None)
        if (stamp and now-stamp<CACHE_SECONDS) or (newest and datetime.now(timezone.utc)-newest<timedelta(seconds=CACHE_SECONDS)):
            out['cached']=True
            out['sources'].append({'name':'Cache','status':'recent'})
            return out
        while requests_per_min and now-requests_per_min[0]>60:requests_per_min.popleft()
        if len(requests_per_min)>=10:raise ValueError('Limite de 10 pesquisas novas por minuto atingido.')
        enforce_lookup_quota(name,owner)
        requests_per_min.append(now)
        last_fetch[name.casefold()]=now

    new=[]
    # The provider trigger and marketplace query run concurrently, not as a hard dependency.
    with ThreadPoolExecutor(max_workers=2) as pool:
        work={}
        if BRIGHTDATA_API_TOKEN:
            work[pool.submit(metered_provider_call,'brightdata',name,providers.trigger_bright,name,BRIGHTDATA_API_TOKEN,BRIGHTDATA_DATASET_ID)]='brightdata'
        if MELI_ACCESS_TOKEN:
            work[pool.submit(metered_provider_call,'mercadolivre',name,providers.search_meli,name,MELI_ACCESS_TOKEN)]='mercadolivre'
        for task in as_completed(work):
            provider_name=work[task]
            try:
                result=task.result()
                if provider_name=='brightdata':
                    job_set(name,result)
                    out['pending']=True
                    out['sources'].append({'name':'Bright Data','status':'processing'})
                else:
                    rows=providers.parse_meli(result,name,numeric_price,match_score,safe_url,image_url,timestamp)
                    new.extend(rows)
                    out['sources'].append({'name':'Mercado Livre','status':'ok' if rows else 'empty'})
            except (ValueError,HTTPError,URLError,TimeoutError,OSError):
                out['sources'].append({'name':'Bright Data' if provider_name=='brightdata' else 'Mercado Livre','status':'unavailable'})
                out['errors'].append(('Bright Data' if provider_name=='brightdata' else 'Mercado Livre')+': consulta indisponível ou sem permissão.')

    if new:out['offers']=record_merged(name,new,out['checked_at'])
    # SerpApi is kept strictly as an optional fallback if neither source found
    # data and Bright Data is not pending; this avoids unnecessary charged searches.
    if SERPAPI_KEY and not out['offers'] and not out['pending']:
        try:
            rows=metered_provider_call('serpapi',name,shopping_search,name)
            for row in rows:row['provider']='serpapi'
            if rows:out['offers']=record_merged(name,rows,out['checked_at'])
            out['sources'].append({'name':'SerpApi','status':'ok' if rows else 'empty'})
        except (ValueError,HTTPError,URLError,TimeoutError,OSError):
            out['errors'].append('SerpApi: indisponível.')
            out['sources'].append({'name':'SerpApi','status':'unavailable'})
    if KEEPA_KEY:
        try:store_points(name,load_keepa_history(name))
        except (ValueError,HTTPError,URLError,TimeoutError,OSError):out['errors'].append('Keepa: histórico indisponível.')
    if out['errors'] and not out['offers'] and not out['pending']:
        with lock:last_fetch[name.casefold()]=now-CACHE_SECONDS+120
    return out

def catalog_suggestions(query,limit=8):
    """Suggest known catalog models locally; never spend provider credits."""
    text=normalize(query).strip()
    if len(text)<2:return []
    try:data=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return []
    terms=[x for x in re.findall(r'[a-z0-9]+',text) if x not in ('de','do','da','para')]
    if not terms:return []
    matches=[]
    for row in data:
        name=normalize(row.get('name') or '')
        sub=normalize(row.get('subcategory') or '')
        if not all(token in name or token in sub for token in terms):continue
        rank=(100 if name==text else 70 if name.startswith(text) else 40)
        rank+=sum(4 for t in terms if re.search(r'(?<![a-z0-9])'+re.escape(t)+r'(?![a-z0-9])',name))
        matches.append((rank,len(name),row))
    matches.sort(key=lambda x:(-x[0],x[1]))
    return [{'name':o.get('name'),'category':o.get('category'),'subcategory':o.get('subcategory')}
            for _,_,o in matches[:limit]]


def catalog_cards(limit=450):
    """Serve the best stored offers (and genuine photos) without provider/API charges."""
    cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat(timespec='seconds')
    with lock,db_conn() as cn:
        rows=cn.execute('''SELECT product,merchant,title,price,checked_at,image_url,shipping_price
          FROM offer_variants WHERE checked_at>=? ORDER BY checked_at DESC LIMIT 2500''',(cutoff,)).fetchall()
    found={}
    for row in rows:
        item=dict(row);name=item.get('product')
        if not name or not numeric_price(item.get('price')):continue
        if quality_cutoff(name) and item['checked_at']<quality_cutoff(name):continue
        if price_assurance.classify(name,str(item.get('title') or ''),match_score(name,str(item.get('title') or '')))['status']=='rejected':continue
        existing=found.get(name)
        if existing is None:
            found[name]=item
        elif float(item['price'])<float(existing['price']):
            # Keep the verified product photo even if a cheaper listing lacks one.
            if not item.get('image_url') and existing.get('image_url'):
                item['image_url']=existing['image_url']
            found[name]=item
        elif not existing.get('image_url') and item.get('image_url'):
            existing['image_url']=item['image_url']
    return [price_assurance.enrich({**o,'variant_label':variant_identity.describe(o.get('product',''),o.get('title',''))}) for o in list(found.values())[:limit]]

def highlights(limit=12):
    """Genuine fresh observations only. No fabricated product prices."""
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec='seconds')
    with lock,db_conn() as cn:
        rows=cn.execute('''SELECT product,merchant,title,price,checked_at,image_url,url,shipping_price
          FROM offer_variants WHERE checked_at>=? ORDER BY checked_at DESC LIMIT 300''',(cutoff,)).fetchall()
    found={}
    for row in rows:
        data=dict(row);name=data['product']
        if quality_cutoff(name) and data['checked_at']<quality_cutoff(name):continue
        if price_assurance.classify(name,str(data.get('title') or ''),match_score(name,str(data.get('title') or '')))['status']=='rejected':continue
        if name not in found or data['price']<found[name]['price']:found[name]=data
    return [price_assurance.enrich({**o,'variant_label':variant_identity.describe(o.get('product',''),o.get('title',''))}) for o in sorted(found.values(),key=lambda o:o['price'])[:limit]]

class Handler(BaseHTTPRequestHandler):
    server_version='RadarPreco/10.1'
    def log_message(self,fmt,*args):pass # prevent query/key logging
    def client_session(self):
        import http.cookies
        cookies=http.cookies.SimpleCookie()
        try:cookies.load(self.headers.get('Cookie',''))
        except Exception:return (None,None)
        token=cookies.get('radar_session')
        secret=token.value if token else None
        if not secret:return (None,None)
        with lock,db_conn() as cn:user=accounts.who(cn,secret)
        return (user,secret)

    def safe_origin(self):
        origin=self.headers.get('Origin')
        if not origin:return self.headers.get('Sec-Fetch-Site','') not in ('cross-site','same-site')
        try:
            p=urlsplit(origin)
            host=(self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '').split(':')[0].lower()
            return (p.scheme in ('http','https') and p.hostname==host)
        except ValueError:return False

    def auth_throttle(self):
        # 8 login attempts per IP per 10 minutes; memory-only additional protection.
        address=self.client_address[0]
        now=time.monotonic()
        with lock:
            a=auth_attempts.get(address,[])
            a=[t for t in a if now-t<600]
            if len(a)>=8:raise ValueError('Muitas tentativas. Volte mais tarde.')
            a.append(now);auth_attempts[address]=a

    def set_session_cookie(self,secret):
        import http.cookies
        cookie=http.cookies.SimpleCookie()
        cookie['radar_session']=secret or ''
        cookie['radar_session']['path']='/'
        cookie['radar_session']['httponly']=True
        cookie['radar_session']['samesite']='Lax'
        if os.environ.get('HOST','')!='127.0.0.1' and self.headers.get('X-Forwarded-Proto','https')=='https':
            cookie['radar_session']['secure']=True
        cookie['radar_session']['max-age']=accounts.SESSION_SECONDS if secret else 0
        self._session_cookie=cookie.output(header='').strip()

    def auth_result(self,user,secret):
        return {'user':user,'csrf':accounts.csrf(secret),'favorites':accounts.favorites(self._auth_cn,user['id'])}

    def request_json(self,max_size=4096):
        if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
            raise ValueError('JSON obrigatório.')
        size=int(self.headers.get('Content-Length','0'))
        if size<2 or size>max_size:raise ValueError('Tamanho da requisição inválido.')
        data=json.loads(self.rfile.read(size))
        if not isinstance(data,dict):raise ValueError('Objeto JSON inválido.')
        return data

    def session_owner(self):
        user,secret=self.client_session()
        if user:return 'account:'+str(user['id'])
        return alerts_engine.device_key(self.headers.get('X-Radar-Device'))

    def respond(self,obj,status=200):
        raw=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        self.send_response(status)
        for k,v in [('Content-Type','application/json; charset=utf-8'),('Cache-Control','no-store'),('Referrer-Policy','strict-origin-when-cross-origin'),('X-Frame-Options','DENY'),('Permissions-Policy','camera=(), microphone=()'),
                    ('X-Content-Type-Options','nosniff'),('Content-Length',str(len(raw)))]:self.send_header(k,v)
        if getattr(self,'_session_cookie',None):self.send_header('Set-Cookie',self._session_cookie)
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        path=urlsplit(self.path)
        if path.path=='/healthz':
            self.respond({'ok':True,'service':'RadarPreço','api_configured':configured()});return
        if path.path in ('/','/index.html'):
            data=(ROOT/'index.html').read_bytes()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(data)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','strict-origin-when-cross-origin');self.end_headers();self.wfile.write(data);return
        if path.path=='/api/watch':
            try:
                device=self.session_owner()
                with lock,db_conn() as cn: state=alerts_engine.watch_state(cn,device)
                self.respond(state)
            except ValueError as e:self.respond({'error':str(e)},400)
            return
        if path.path=='/api/me':
            user,secret=self.client_session()
            if user:
                with lock,db_conn() as cn:saved=accounts.favorites(cn,user['id'])
                self.respond({'user':user,'csrf':accounts.csrf(secret),'favorites':saved})
            else:self.respond({'user':None,'favorites':[]})
            return
        if path.path=='/api/favorites':
            user,_=self.client_session()
            if not user:self.respond({'error':'Faça login para sincronizar favoritos.'},401);return
            with lock,db_conn() as cn:saved=accounts.favorites(cn,user['id'])
            self.respond({'favorites':saved});return
        if path.path=='/api/suggest':
            query=(parse_qs(path.query).get('q') or [''])[0][:100]
            self.respond({'items':catalog_suggestions(query),'no_billable_queries':True})
            return
        if path.path=='/api/catalog-cards':
            self.respond({'items':catalog_cards(),'source':'stored_verified_offers','no_billable_queries':True});return
        if path.path=='/api/highlights':
            self.respond({'offers':highlights(),'note':'Somente anúncios aprovados pelo validador nas últimas 24 horas.'});return
        if path.path=='/favicon.svg' or path.path in ('/icon-192.png','/icon-512.png'):
            file=ROOT/path.path.lstrip('/')
            raw=file.read_bytes()
            mime='image/svg+xml' if path.path.endswith('.svg') else 'image/png'
            self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        if path.path in ('/ui-refresh.css','/ui-refresh.js','/search-experience.js'):
            raw=(ROOT/path.path.lstrip('/')).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type','text/css; charset=utf-8' if path.path.endswith('.css') else 'text/javascript; charset=utf-8')
            self.send_header('Cache-Control','public, max-age=3600')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw);return
        if path.path=='/photo-discovery.js':
            raw=(ROOT/'photo_discovery.js').read_bytes()
            self.send_response(200);self.send_header('Content-Type','text/javascript; charset=utf-8')
            self.send_header('Cache-Control','public, max-age=3600')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        if path.path=='/manifest.webmanifest':
            raw=(ROOT/'manifest.webmanifest').read_bytes()
            self.send_response(200);self.send_header('Content-Type','application/manifest+json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        if path.path=='/sw.js':
            raw=(ROOT/'sw.js').read_bytes()
            self.send_response(200);self.send_header('Content-Type','text/javascript');self.send_header('Service-Worker-Allowed','/');self.send_header('Cache-Control','no-cache');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        if path.path=='/api/admin/audit':
            import secrets
            expected=os.environ.get('RADAR_ADMIN_KEY','')
            supplied=self.headers.get('X-Radar-Admin-Key','')
            if not expected or not secrets.compare_digest(supplied,expected):
                self.respond({'error':'Rota indisponível.'},404);return
            with lock,db_conn() as cn:
                report_rows=cn.execute("""SELECT product,merchant,title,reason,created_at
                    FROM offer_reports ORDER BY id DESC LIMIT 100""").fetchall()
                pending=cn.execute("""SELECT product,merchant,variant_key,title,price,reason,checked_at
                    FROM offer_audit_events WHERE status='pending_review'
                    ORDER BY id DESC LIMIT 100""").fetchall()
                self.respond({'audit':audit_runtime.summary(cn),
                    'provider_usage':audit_runtime.usage(cn),
                    'reports':[dict(x) for x in report_rows],
                    'pending':[dict(x) for x in pending],
                    'pricing_note':'Consumo em requisições. Tarifas reais dependem do contrato com cada API.'})
            return
        if path.path=='/api/status':
            self.respond({'version':'16.0','smoke_test':dict(smoke_state),'connected':configured(),
             'shopping':bool(SERPAPI_KEY or BRIGHTDATA_API_TOKEN or MELI_ACCESS_TOKEN),
             'brightdata':bool(BRIGHTDATA_API_TOKEN),'mercadolivre':bool(MELI_ACCESS_TOKEN),
             'serpapi_fallback':bool(SERPAPI_KEY),'coverage':'on_demand_and_opportunistic_background','catalog_items':1000,'automatic_checks':True,'checks_best_effort':True,'automatic_daily_cap':2,'storage':'postgres' if USE_POSTGRES else 'temporary_sqlite','auth':'email_password',
             'historical_google':bool(SERPAPI_KEY),'historical_amazon':bool(KEEPA_KEY),
             'history_note':'Histórico anterior depende de cobertura do provedor. Não são gerados preços simulados.',
              'quality_policy':'Modelos, variantes e anomalias revisados internamente; GTIN externo ainda não confirmado.',
               'shipping_by_cep_enabled':False,'admin_audit_requires_key':True});return
        if path.path=='/api/history':
            try:name=clean_query((parse_qs(path.query).get('q') or [''])[0])
            except ValueError as e:self.respond({'error':str(e)},400);return
            self.respond(read_history(name,(parse_qs(path.query).get('days') or ['7'])[0]));return
        if path.path=='/api/refresh':self.respond({'error':'Use POST para atualizar preços.'},405);return
        self.respond({'error':'Rota inexistente'},404)
    def do_POST(self):
        path=urlsplit(self.path)
        if not self.safe_origin():self.respond({'error':'Origem não autorizada.'},403);return
        if path.path=='/api/report-offer':
            try:
                body=self.request_json(1800)
                name=clean_query(body.get('product',''))
                merchant=str(body.get('merchant') or '').strip()[:100]
                reason=str(body.get('reason') or '').strip()[:90]
                if reason not in ('modelo_incorreto','variante_incorreta','anuncio_indisponivel','preco_incorreto'):
                    raise ValueError('Motivo de revisão inválido.')
                match=next((o for o in read_offers(name) if o['source']==merchant),None)
                if not match:raise ValueError('Oferta não encontrada na consulta atual.')
                fingerprint=hashlib.sha256((self.client_address[0]+'|'+os.environ.get('RADAR_REPORT_SALT','radar-report')).encode()).hexdigest()
                with lock,db_conn() as cn:
                    n=cn.execute('SELECT COUNT(*) FROM offer_reports WHERE reporter=? AND created_at>=?',
                         (fingerprint,datetime.now(timezone.utc).date().isoformat())).fetchone()[0]
                    if n>=8:raise ValueError('Limite diário de revisões atingido.')
                    cn.execute('INSERT INTO offer_reports(product,merchant,title,reason,created_at,reporter) VALUES(?,?,?,?,?,?)',
                        (name,merchant,str(match.get('title') or '')[:200],reason,timestamp(),fingerprint))
                self.respond({'received':True,'message':'Oferta enviada para revisão. Não será retirada automaticamente.'})
            except (ValueError,TypeError,json.JSONDecodeError) as exc:
                self.respond({'error':str(exc) if isinstance(exc,ValueError) else 'Dados inválidos.'},400)
            except Exception:
                self.respond({'error':'Não foi possível registrar a revisão agora.'},503)
            return
        if path.path in ('/api/auth/register','/api/auth/login','/api/auth/logout','/api/auth/migrate_watch','/api/favorites'):
            try:
                body=self.request_json(10000)
                user,secret=self.client_session()
                if path.path in ('/api/auth/register','/api/auth/login'):
                    self.auth_throttle()
                    with lock,db_conn() as cn:
                        if path.path.endswith('register'):
                            user=accounts.register(cn,body.get('email'),body.get('password'))
                        else:user=accounts.login(cn,body.get('email'),body.get('password'))
                        token=accounts.new_session(cn,user)
                        favs=accounts.favorites(cn,user['id'])
                    self.set_session_cookie(token)
                    self.respond({'user':user,'csrf':accounts.csrf(token),'favorites':favs});return
                if not user or not accounts.check_csrf(secret,self.headers.get('X-Radar-CSRF')):
                    self.respond({'error':'Sessão inválida ou proteção CSRF ausente.'},403);return
                if path.path.endswith('migrate_watch'):
                    old_device=alerts_engine.device_key(body.get('device'))
                    new_device='account:'+str(user['id'])
                    with lock,db_conn() as cn:
                        old=cn.execute('SELECT product,threshold,created_at FROM watches WHERE device=? LIMIT 30',(old_device,)).fetchall()
                        existing=cn.execute('SELECT COUNT(*) FROM watches WHERE device=?',(new_device,)).fetchone()[0]
                        for w in old[:max(0,30-existing)]:
                            cn.execute('INSERT OR IGNORE INTO watches(device,product,threshold,created_at,last_auto_at) VALUES(?,?,?,?,?)',
                                       (new_device,w['product'],w['threshold'],w['created_at'],''))
                    self.respond({'imported':len(old)});return
                if path.path.endswith('logout'):
                    with lock,db_conn() as cn:accounts.logout(cn,secret)
                    self.set_session_cookie(None)
                    self.respond({'user':None,'favorites':[]});return
                with lock,db_conn() as cn:
                    favs=accounts.save_favorites(cn,user['id'],body.get('favorites',[]))
                self.respond({'favorites':favs});return
            except (ValueError,TypeError,json.JSONDecodeError) as exc:
                self.respond({'error':str(exc) if isinstance(exc,ValueError) else 'Dados inválidos.'},400);return
            except Exception as exc:
                # Do not reveal database and user details.
                self.respond({'error':'Operação indisponível. Tente novamente.'},503);return
        if path.path=='/api/watch':
            user,secret=self.client_session()
            if user and not accounts.check_csrf(secret,self.headers.get('X-Radar-CSRF')):
                self.respond({'error':'Proteção de sessão inválida.'},403);return
            if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
                self.respond({'error':'JSON obrigatório.'},415);return
            try:
                device=self.session_owner()
                length=int(self.headers.get('Content-Length','0'))
                if not 2<=length<=800:raise ValueError('Corpo inválido.')
                body=json.loads(self.rfile.read(length))
                action=str(body.get('action') or '').strip()
                with lock,db_conn() as cn:
                    if action=='read':
                        alerts_engine.mark_read(cn,device,body.get('id','all'),timestamp())
                    else:
                        name=clean_query(body.get('product',''))
                        alerts_engine.change_watch(cn,device,name,action,body.get('threshold',10),timestamp(),body.get('target_price'))
                    state=alerts_engine.watch_state(cn,device)
                self.respond(state)
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as e:
                self.respond({'error':str(e) if isinstance(e,ValueError) else 'Entrada inválida.'},400)
            return
        if path.path!='/api/refresh':self.respond({'error':'Rota inexistente'},404);return
        if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
            self.respond({'error':'Conteúdo JSON obrigatório.'},415);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<2 or size>1024:self.respond({'error':'Tamanho de entrada inválido.'},400);return
            data=json.loads(self.rfile.read(size))
            name=clean_query(data.get('q',''))
        except (ValueError,TypeError,AttributeError,json.JSONDecodeError) as e:
            self.respond({'error':'Nome do produto inválido.'},400);return
        try:
            user,_=self.client_session()
            owner='user:'+str(user['id']) if user else 'guest:'+str(self.client_address[0])
            result=perform_refresh(name,owner)
            result['history']=read_history(name,'365')
            self.respond(result)
        except ValueError as e:self.respond({'error':str(e)},429)
        except Exception:self.respond({'error':'Falha ao consultar a fonte. Tente novamente.'},502)

def automatic_watch_loop():
    # Best-effort scheduler on a free sleeping service. Limit reserved
    # background lookups to 2/day, leaving the rest of the API budget to users.
    daily_day=''
    completed=0
    while True:
        time.sleep(300)
        if not configured():continue
        day=datetime.now(timezone.utc).date().isoformat()
        if day!=daily_day:daily_day=day;completed=0
        if completed>=2:continue
        try:
            with lock,db_conn() as cn:
                name=alerts_engine.next_due_product(cn)
                if name:alerts_engine.mark_scanned(cn,name,timestamp())
            if not name:continue
            completed+=1
            result=perform_refresh(name,'automatic')
            print('AUTO_WATCH status=%s' % ('ok' if result.get('offers') else 'no_offers'),flush=True)
        except ValueError:
            # Quota reached; do not retry until tomorrow.
            completed=2
        except Exception as exc:
            print('AUTO_WATCH error_type=%s' % type(exc).__name__,flush=True)

smoke_state={'status':'not_requested','offers':0,'checked_at':None}

def run_smoke_query():
    """One-off deployment smoke test; never print keys or full provider error URLs."""
    raw=os.environ.get('RADAR_SMOKE_QUERY','').strip()
    if not raw:return
    smoke_state.update(status='checking',offers=0,checked_at=timestamp())
    try:
        name=clean_query(raw)
        outcome=perform_refresh(name)
        offers=outcome.get('offers') or []
        state='ok' if offers else ('provider_unavailable' if outcome.get('errors') else 'no_matching_offers')
        smoke_state.update(status=state,offers=len(offers),checked_at=timestamp())
        print('RADAR_SMOKE status=%s offers=%d' % (state,len(offers)),flush=True)
    except Exception as exc:
        smoke_state.update(status='error',offers=0,checked_at=timestamp())
        print('RADAR_SMOKE status=error category='+type(exc).__name__,flush=True)

def main():
    if os.environ.get('RADAR_QUALITY_SELFTEST')=='1':
        import quality_selftest
        print('RADAR_QUALITY_TEST passed=%d' % quality_selftest.run(sys.modules[__name__]),flush=True)
    if os.environ.get('RADAR_MATCH_SELFTEST')=='1':
        import matching_selftest
        checks=matching_selftest.run(match_score,extract_offers,product_identity.canonical_query,ROOT/'catalog.json')
        print('RADAR_IDENTITY_TEST passed=%d cross_catalog_matches=%d examples=%s' %
              (checks['passed_cases'],checks['cross_catalog_matches'],
               repr(checks['cross_catalog_examples'][:60])),flush=True)
    initialize();app=ThreadingHTTPServer((HOST,PORT),Handler)
    print('RadarPreço 10.0 em http://%s:%d | Bright Data %s | Mercado Livre %s | SerpApi %s' % (HOST,PORT, 'ON' if BRIGHTDATA_API_TOKEN else 'OFF','ON' if MELI_ACCESS_TOKEN else 'OFF','ON' if SERPAPI_KEY else 'OFF'))
    threading.Thread(target=automatic_watch_loop,daemon=True).start()
    if os.environ.get('RADAR_SMOKE_QUERY','').strip():
        threading.Thread(target=run_smoke_query,daemon=True).start()
    try:app.serve_forever()
    except KeyboardInterrupt:pass
    finally:app.server_close()
if __name__=='__main__':main()