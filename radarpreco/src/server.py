#!/usr/bin/env python3
"""RadarPreço 7.0 — two-source search with provenance and asynchronous updates.

Only real source readings are stored. The API cannot invent unavailable
historical values. For a public hosted service add authentication and robust
rate limits, persistent disk/database, schedule worker, and protection.
"""
from __future__ import annotations
import json, os, re, sqlite3, threading, time, unicodedata
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
lock=threading.RLock(); last_fetch={}; requests_per_min=deque(maxlen=50)

def timestamp():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def clean_query(q):
    q=str(q).strip()
    if len(q)<2 or len(q)>160 or any(ord(c)<32 for c in q):raise ValueError('Nome deve ter entre 2 e 160 caracteres.')
    return q

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
    return re.findall(r'[a-z0-9]+',s)

def match_score(wanted,candidate):
    """Match title identity, required model and capacity; never cross FE/Ultra or 128/256GB."""
    a=words(wanted);b=words(candidate)
    if not a or not b:return 0.0
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
        if score<0.72:continue
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
        arr.append({'title':title,'price':price,'source':source,'url':url,'image_url':thumb,
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
    cn=sqlite3.connect(DB_PATH,timeout=15)
    cn.row_factory=sqlite3.Row
    try:
        with cn:
            yield cn
    finally:
        cn.close()

def initialize():
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

def store_offer_cache(name, items, at=None):
    """Keep only offers observed together, one lowest listing per merchant."""
    stamp=at or timestamp()
    grouped={}
    for item in items:
        merchant=str(item.get('source') or '').strip()[:100]
        price=numeric_price(item.get('price'))
        if not merchant or not price:continue
        key=normalize(merchant)
        if key not in grouped or price<grouped[key]['price']:
            grouped[key]={**item, 'source':merchant,'price':price,'checked_at':str(item.get('checked_at') or stamp)}
    with lock,db_conn() as cn:
        cn.execute('DELETE FROM offers WHERE product=?',(name,))
        for item in grouped.values():
            cn.execute('INSERT INTO offers(product,merchant,title,price,url,checked_at,image_url,provider) VALUES (?,?,?,?,?,?,?,?)',
                (name,item['source'],str(item.get('title',''))[:200],item['price'],safe_url(item.get('url')),str(item.get('checked_at') or stamp),image_url(item.get('image_url')),str(item.get('provider') or 'serpapi')[:30]))
    return list(sorted(grouped.values(),key=lambda x:x['price']))


def read_offers(name):
    with lock,db_conn() as cn:
        rows=cn.execute('SELECT merchant AS source,title,price,url,checked_at,image_url,provider FROM offers WHERE product=? ORDER BY price ASC LIMIT 50',(name,)).fetchall()
    return [dict(row) for row in rows]


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
    if days not in ('7','30','90','365','all'):days='7'
    since=(datetime.now(timezone.utc)-timedelta(days=int(days))).isoformat(timespec='seconds') if days!='all' else '2000-01-01T00:00:00+00:00'
    with lock,db_conn() as cn:
        rows=cn.execute('''SELECT series,price,at,source,url,kind FROM prices
             WHERE product=? AND at >= ? ORDER BY at ASC LIMIT 4000''',(name,since)).fetchall()
        latest=cn.execute('''SELECT series,price,at,source,url,kind FROM prices
             WHERE product=? AND kind='snapshot' AND at=(SELECT MAX(at) FROM prices WHERE product=? AND kind='snapshot')
             ORDER BY price ASC LIMIT 1''',(name,name)).fetchone()
    offers=read_offers(name)
    return {'query':name,'days':days,'points':[dict(row) for row in rows],
           'latest':dict(latest) if latest else None,'offers':offers,'checked_at':timestamp(),'image_url':next((o.get('image_url','') for o in offers if o.get('image_url')),'')}

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
    """Merge the responses from both providers; never overwrite a source during a pending job."""
    if not new_offers:return read_offers(name)
    stamp=stamp or timestamp()
    # Keep only fresh old offers from other sources, never retain an outdated
    # lower price for the same merchant after a newer observation.
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec='seconds')
    old=[o for o in read_offers(name) if str(o.get('checked_at') or '')>=cutoff]
    refreshed={normalize(str(o.get('source') or '')) for o in new_offers}
    remaining=[o for o in old if normalize(str(o.get('source') or '')) not in refreshed]
    merged=store_offer_cache(name,remaining+new_offers,stamp)
    store_points(name,[{'series':'Loja • '+o['source'],'price':o['price'],'at':str(o.get('checked_at') or stamp),
          'source':str(o.get('provider') or o['source']),'url':o['url'],'kind':'snapshot'}
          for o in new_offers if o.get('price')])
    return read_offers(name)

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
    state,body=providers.read_bright(job['snapshot_id'],BRIGHTDATA_API_TOKEN)
    if state=='pending':return ('pending',[])
    job_clear(name)
    if state=='ready':
        offers=providers.parse_bright(body,name,numeric_price,match_score,safe_url,image_url,timestamp)
        return ('ready',offers)
    return (state,[])

def enforce_lookup_quota(name):
    # Every *new search* is counted; background polling is not.
    utc=datetime.now(timezone.utc)
    day=utc.date().isoformat()
    month=day[:7]
    with lock,db_conn() as cn:
        n_day=cn.execute('SELECT COUNT(*) FROM lookup_quota WHERE at>=?',(day,)).fetchone()[0]
        n_month=cn.execute('SELECT COUNT(*) FROM lookup_quota WHERE at>=?',(month+'-01',)).fetchone()[0]
        if n_day>=MAX_LOOKUPS_PER_DAY or n_month>=MAX_LOOKUPS_PER_MONTH:
            raise ValueError('Limite preventivo de consultas atingido. Tente mais tarde.')
        cn.execute('INSERT INTO lookup_quota(at,product) VALUES (?,?)',(timestamp(),name))

def perform_refresh(name):
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
    with lock:
        stamp=last_fetch.get(name.casefold())
        if stamp and now-stamp<CACHE_SECONDS:
            out['cached']=True
            out['sources'].append({'name':'Cache','status':'recent'})
            return out
        while requests_per_min and now-requests_per_min[0]>60:requests_per_min.popleft()
        if len(requests_per_min)>=10:raise ValueError('Limite de 10 pesquisas novas por minuto atingido.')
        enforce_lookup_quota(name)
        requests_per_min.append(now)
        last_fetch[name.casefold()]=now

    new=[]
    # The provider trigger and marketplace query run concurrently, not as a hard dependency.
    with ThreadPoolExecutor(max_workers=2) as pool:
        work={}
        if BRIGHTDATA_API_TOKEN:
            work[pool.submit(providers.trigger_bright,name,BRIGHTDATA_API_TOKEN,BRIGHTDATA_DATASET_ID)]='brightdata'
        if MELI_ACCESS_TOKEN:
            work[pool.submit(providers.search_meli,name,MELI_ACCESS_TOKEN)]='mercadolivre'
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
            rows=shopping_search(name)
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

class Handler(BaseHTTPRequestHandler):
    server_version='RadarPreco/7.0'
    def log_message(self,fmt,*args):pass # prevent query/key logging
    def respond(self,obj,status=200):
        raw=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        self.send_response(status)
        for k,v in [('Content-Type','application/json; charset=utf-8'),('Cache-Control','no-store'),
                    ('X-Content-Type-Options','nosniff'),('Content-Length',str(len(raw)))]:self.send_header(k,v)
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        path=urlsplit(self.path)
        if path.path=='/healthz':
            self.respond({'ok':True,'service':'RadarPreço','api_configured':configured()});return
        if path.path in ('/','/index.html'):
            data=(ROOT/'index.html').read_bytes()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if path.path=='/api/status':
            self.respond({'version':'7.0','connected':configured(),
             'shopping':bool(SERPAPI_KEY or BRIGHTDATA_API_TOKEN or MELI_ACCESS_TOKEN),
             'brightdata':bool(BRIGHTDATA_API_TOKEN),'mercadolivre':bool(MELI_ACCESS_TOKEN),
             'serpapi_fallback':bool(SERPAPI_KEY),'coverage':'on_demand','catalog_items':1000,
             'historical_google':bool(SERPAPI_KEY),'historical_amazon':bool(KEEPA_KEY),
             'history_note':'Histórico anterior depende de cobertura do provedor. Não são gerados preços simulados.'});return
        if path.path=='/api/history':
            try:name=clean_query((parse_qs(path.query).get('q') or [''])[0])
            except ValueError as e:self.respond({'error':str(e)},400);return
            self.respond(read_history(name,(parse_qs(path.query).get('days') or ['7'])[0]));return
        if path.path=='/api/refresh':self.respond({'error':'Use POST para atualizar preços.'},405);return
        self.respond({'error':'Rota inexistente'},404)
    def do_POST(self):
        path=urlsplit(self.path)
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
            result=perform_refresh(name)
            result['history']=read_history(name,'365')
            self.respond(result)
        except ValueError as e:self.respond({'error':str(e)},429)
        except Exception:self.respond({'error':'Falha ao consultar a fonte. Tente novamente.'},502)

def main():
    initialize();app=ThreadingHTTPServer((HOST,PORT),Handler)
    print('RadarPreço 7.0 em http://%s:%d | Bright Data %s | Mercado Livre %s | SerpApi %s' % (HOST,PORT, 'ON' if BRIGHTDATA_API_TOKEN else 'OFF','ON' if MELI_ACCESS_TOKEN else 'OFF','ON' if SERPAPI_KEY else 'OFF'))
    try:app.serve_forever()
    except KeyboardInterrupt:pass
    finally:app.server_close()
if __name__=='__main__':main()