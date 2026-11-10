"""Indicador histórico e alertas percentuais do RadarPreço 8.0.

Somente cotações datadas. Nunca mistura lojas distintas numa queda.
"""
from __future__ import annotations
import hashlib, re, statistics
from datetime import datetime, timedelta, timezone

def now_utc():
    return datetime.now(timezone.utc)

def parse_at(v):
    try:
        t=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError):return None

def init_schema(cn):
    cn.execute("""CREATE TABLE IF NOT EXISTS watches (
        device TEXT NOT NULL,product TEXT NOT NULL,threshold REAL NOT NULL DEFAULT 10,
        created_at TEXT NOT NULL,last_auto_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(device,product))""")
    cn.execute('CREATE INDEX IF NOT EXISTS watches_auto ON watches(last_auto_at)')
    cn.execute("""CREATE TABLE IF NOT EXISTS price_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device TEXT NOT NULL,product TEXT NOT NULL,merchant TEXT NOT NULL,
        old_price REAL NOT NULL,new_price REAL NOT NULL,percent REAL NOT NULL,
        old_at TEXT NOT NULL,new_at TEXT NOT NULL,created_at TEXT NOT NULL,
        read_at TEXT NOT NULL DEFAULT '',
        UNIQUE(device,product,merchant,new_at,new_price))""")
    cn.execute('CREATE INDEX IF NOT EXISTS alerts_owner_date ON price_alerts(device,id DESC)')

def device_key(raw):
    token=str(raw or '').strip()
    if not re.fullmatch(r'[a-f0-9]{48,64}',token):
        raise ValueError('Identificação do navegador inválida.')
    return hashlib.sha256(bytes.fromhex(token)).hexdigest()

def watch_state(cn,device):
    watches=[dict(x) for x in cn.execute(
        'SELECT product,threshold,created_at,last_auto_at FROM watches WHERE device=? ORDER BY created_at DESC LIMIT 40',(device,))]
    alerts=[dict(x) for x in cn.execute("""SELECT id,product,merchant,old_price,new_price,percent,
        old_at,new_at,created_at,read_at FROM price_alerts WHERE device=? ORDER BY id DESC LIMIT 50""",(device,))]
    unread=cn.execute("SELECT COUNT(*) FROM price_alerts WHERE device=? AND read_at=''",(device,)).fetchone()[0]
    return {'watches':watches,'alerts':alerts,'unread':unread,
        'note':'Alertas no aplicativo; o servidor gratuito pode suspender verificações quando inativo.'}

def change_watch(cn,device,product,action,threshold,now):
    if action=='remove':
        cn.execute('DELETE FROM watches WHERE device=? AND product=?',(device,product));return
    if action!='set':raise ValueError('Ação de acompanhamento inválida.')
    try:pct=float(threshold)
    except (TypeError,ValueError):raise ValueError('Queda percentual inválida.')
    if pct not in (5,10,15,20,30,50):
        raise ValueError('Escolha uma queda de 5%, 10%, 15%, 20%, 30% ou 50%.')
    count=cn.execute('SELECT COUNT(*) FROM watches WHERE device=?',(device,)).fetchone()[0]
    exists=cn.execute('SELECT 1 FROM watches WHERE device=? AND product=?',(device,product)).fetchone()
    if not exists and count>=30:raise ValueError('Limite de 30 monitoramentos por navegador atingido.')
    cn.execute("""INSERT INTO watches(device,product,threshold,created_at) VALUES(?,?,?,?)
        ON CONFLICT(device,product) DO UPDATE SET threshold=excluded.threshold""",
        (device,product,pct,now))

def mark_read(cn,device,alert_id,now):
    if alert_id=='all':
        cn.execute("UPDATE price_alerts SET read_at=? WHERE device=? AND read_at=''",(now,device))
    else:
        try:i=int(alert_id)
        except (ValueError,TypeError):raise ValueError('Alerta inválido.')
        cn.execute('UPDATE price_alerts SET read_at=? WHERE device=? AND id=?',(now,device,i))

def store_drop_alerts(cn,product,new_offers,now=None):
    observers=list(cn.execute('SELECT device,threshold FROM watches WHERE product=?',(product,)))
    if not observers:return 0
    stamp=now or now_utc().isoformat(timespec='seconds')
    at=parse_at(stamp) or now_utc()
    added=0
    for offer in new_offers:
        merchant=str(offer.get('source') or '').strip()[:100]
        try:new=float(offer.get('price'))
        except (ValueError,TypeError):continue
        if not merchant or new<=0:continue
        previous=cn.execute("""SELECT price,at FROM prices WHERE product=? AND series=?
            AND kind='snapshot' ORDER BY at DESC LIMIT 1""",(product,'Loja • '+merchant)).fetchone()
        if not previous:continue
        old=float(previous['price']); before=parse_at(previous['at'])
        if not before or old<=new or before>=at or at-before>timedelta(days=30):continue
        pct=100*(old-new)/old
        for watcher in observers:
            if pct+1e-8<float(watcher['threshold']):continue
            res=cn.execute("""INSERT OR IGNORE INTO price_alerts
                (device,product,merchant,old_price,new_price,percent,old_at,new_at,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (watcher['device'],product,merchant,round(old,2),round(new,2),round(pct,2),
                previous['at'],stamp,stamp))
            added+=res.rowcount
    return added

def opportunity(cn,product,offers,now=None):
    now=now or now_utc()
    current=[]
    for o in offers:
        checked=parse_at(o.get('checked_at'))
        try:price=float(o['price'])
        except (ValueError,TypeError,KeyError):continue
        if checked and timedelta(0)<=now-checked<=timedelta(hours=24) and price>0:
            current.append(price)
    if not current:return {'ready':False,'reason':'no_recent_price','label':'Sem cotação recente','sample_days':0,'current':None}
    best=round(min(current),2)
    since=(now-timedelta(days=365)).isoformat(timespec='seconds')
    rows=cn.execute("""SELECT price,at FROM prices WHERE product=? AND kind IN ('snapshot','historical')
        AND at>=? ORDER BY at ASC LIMIT 25000""",(product,since)).fetchall()
    dates={}
    for rec in rows:
        dt=parse_at(rec['at'])
        if not dt or dt>=now or dt.date()>=now.date():continue
        price=float(rec['price'])
        if price<=0:continue
        key=dt.date().isoformat()
        dates[key]=min(dates.get(key,float('inf')),price)
    for days in (30,90,365):
        cutoff=(now-timedelta(days=days)).date().isoformat()
        values=[p for day,p in dates.items() if day>=cutoff]
        if len(values)<3:continue
        median=round(float(statistics.median(values)),2)
        if median<=0:continue
        difference=round((median-best)*100/median,1)
        if difference>=20:level,label='excellent','Excelente oportunidade'
        elif difference>=10:level,label='good','Preço abaixo do habitual'
        elif difference>=5:level,label='interesting','Oportunidade interessante'
        elif difference>=-5:level,label='normal','Dentro da faixa habitual'
        else:level,label='above','Acima do histórico'
        return {'ready':True,'label':label,'level':level,'current':best,'median':median,
            'percent_below_median':difference,'sample_days':len(values),
            'period_days':days,'confidence':'maior' if len(values)>=12 else 'limitada',
            'method':'mediana dos menores preços diários realmente coletados; sem frete'}
    return {'ready':False,'reason':'insufficient_history','label':'Histórico insuficiente',
        'current':best,'sample_days':len(dates),'required_days':3,
        'method':'Precisa de três dias anteriores distintos com cotações reais.'}

def next_due_product(cn,current_time=None):
    now=current_time or now_utc()
    cutoff=(now-timedelta(hours=24)).isoformat(timespec='seconds')
    row=cn.execute("""SELECT product,MIN(last_auto_at) AS last_auto_at,COUNT(*) AS watchers
        FROM watches GROUP BY product HAVING MIN(last_auto_at)='' OR MIN(last_auto_at)<?
        ORDER BY watchers DESC,last_auto_at ASC LIMIT 1""",(cutoff,)).fetchone()
    return row['product'] if row else None

def mark_scanned(cn,product,at):
    cn.execute('UPDATE watches SET last_auto_at=? WHERE product=?',(at,product))
