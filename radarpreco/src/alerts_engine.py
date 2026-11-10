"""Reliable opportunity indicators and per-merchant drop alerts.

Only dated observations collected from providers are considered. No invented
historical prices; no mix of different variants or offers from distinct stores
when evaluating a single-store drop.
"""
from __future__ import annotations
import hashlib
import re
import statistics
from datetime import datetime, timedelta, timezone


def now_utc():
    return datetime.now(timezone.utc)


def parse_at(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def init_schema(cn,postgres=False):
    idtype='BIGSERIAL PRIMARY KEY' if postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    cn.execute('''CREATE TABLE IF NOT EXISTS watches (
        device TEXT NOT NULL, product TEXT NOT NULL, threshold REAL NOT NULL DEFAULT 10,
        created_at TEXT NOT NULL, last_auto_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(device,product))''')
    cn.execute('CREATE INDEX IF NOT EXISTS watches_auto ON watches(last_auto_at)')
    cn.execute(f'''CREATE TABLE IF NOT EXISTS price_alerts (
        id {idtype},
        device TEXT NOT NULL, product TEXT NOT NULL, merchant TEXT NOT NULL,
        old_price REAL NOT NULL, new_price REAL NOT NULL, percent REAL NOT NULL,
        old_at TEXT NOT NULL, new_at TEXT NOT NULL, created_at TEXT NOT NULL,
        read_at TEXT NOT NULL DEFAULT '',
        UNIQUE(device,product,merchant,new_at,new_price))''')
    cn.execute('CREATE INDEX IF NOT EXISTS alerts_owner_date ON price_alerts(device,id DESC)')


def device_key(raw):
    value = str(raw or '').strip()
    if not re.fullmatch(r'[a-f0-9]{48,64}', value):
        raise ValueError('Identificação do navegador inválida.')
    return hashlib.sha256(bytes.fromhex(value)).hexdigest()


def watch_state(cn, device):
    watches = [dict(row) for row in cn.execute(
        'SELECT product,threshold,created_at,last_auto_at FROM watches WHERE device=? ORDER BY created_at DESC LIMIT 40', (device,))]
    alerts = [dict(row) for row in cn.execute('''SELECT id,product,merchant,old_price,new_price,percent,
        old_at,new_at,created_at,read_at FROM price_alerts WHERE device=? ORDER BY id DESC LIMIT 50''',(device,))]
    unread = cn.execute('SELECT COUNT(*) FROM price_alerts WHERE device=? AND read_at=\'\'', (device,)).fetchone()[0]
    return {'watches': watches, 'alerts': alerts, 'unread': unread,
            'note':'Alertas no aplicativo; o servidor gratuito pode suspender verificações quando inativo.'}


def change_watch(cn, device, product, action, threshold, now):
    if action == 'remove':
        cn.execute('DELETE FROM watches WHERE device=? AND product=?',(device,product))
        return
    if action != 'set':
        raise ValueError('Ação de acompanhamento inválida.')
    try:
        percent = float(threshold)
    except (TypeError,ValueError):
        raise ValueError('Queda percentual inválida.')
    if percent not in (5,10,15,20,30,50):
        raise ValueError('Escolha uma queda de 5%, 10%, 15%, 20%, 30% ou 50%.')
    n=cn.execute('SELECT COUNT(*) FROM watches WHERE device=?',(device,)).fetchone()[0]
    exists=cn.execute('SELECT 1 FROM watches WHERE device=? AND product=?',(device,product)).fetchone()
    if not exists and n>=30:
        raise ValueError('Limite de 30 monitoramentos por navegador atingido.')
    cn.execute('''INSERT INTO watches(device,product,threshold,created_at) VALUES (?,?,?,?)
        ON CONFLICT(device,product) DO UPDATE SET threshold=excluded.threshold''',
        (device,product,percent,now))


def mark_read(cn,device,alert_id,now):
    if alert_id=='all':
        cn.execute('UPDATE price_alerts SET read_at=? WHERE device=? AND read_at=\'\'', (now,device))
    else:
        try:ident=int(alert_id)
        except (ValueError,TypeError):raise ValueError('Alerta inválido.')
        cn.execute('UPDATE price_alerts SET read_at=? WHERE device=? AND id=?',(now,device,ident))


def store_drop_alerts(cn,product,new_offers,now=None):
    """Alert only if this exact merchant was previously observed more expensively.

    Called before writing the new price points. Prefer comparing only snapshots
    less than 30 days old, and dedupe repeated same-price snapshots.
    """
    observers=list(cn.execute('SELECT device,threshold FROM watches WHERE product=?',(product,)))
    if not observers:return 0
    stamp=now or now_utc().isoformat(timespec='seconds')
    incoming_at=parse_at(stamp) or now_utc()
    count=0
    for offer in new_offers:
        merchant=str(offer.get('source') or '').strip()[:100]
        try:new=float(offer.get('price'))
        except (TypeError,ValueError):continue
        if not merchant or new<=0:continue
        previous=cn.execute('''SELECT price,at FROM prices WHERE product=? AND series=? AND kind='snapshot'
            ORDER BY at DESC LIMIT 1''',(product,'Loja • '+merchant)).fetchone()
        if not previous:continue
        old=float(previous['price'])
        before=parse_at(previous['at'])
        if not before or old<=new or before>=incoming_at or incoming_at-before>timedelta(days=30):continue
        pct=(old-new)*100/old
        for observer in observers:
            if pct+1e-8<float(observer['threshold']):continue
            result=cn.execute('''INSERT OR IGNORE INTO price_alerts
              (device,product,merchant,old_price,new_price,percent,old_at,new_at,created_at)
              VALUES(?,?,?,?,?,?,?,?,?)''',
              (observer['device'],product,merchant,round(old,2),round(new,2),round(pct,2),
               previous['at'],stamp,stamp))
            count+=result.rowcount
    return count


def opportunity(cn,product,offers,now=None):
    """Compare today's fresh lowest offer to median of past *distinct* days.

    Days are aggregated to the best offer on that day; repeatedly fetched
    same-day offers do not inflate evidence. Require >= 3 distinct earlier days.
    """
    current_time=now or now_utc()
    current=[]
    for offer in offers:
        checked=parse_at(offer.get('checked_at'))
        try:price=float(offer['price'])
        except (KeyError,ValueError,TypeError):continue
        if checked and timedelta(0)<=current_time-checked<=timedelta(hours=24) and price>0:
            current.append(price)
    if not current:
        return {'ready':False,'reason':'no_recent_price','label':'Sem cotação recente','sample_days':0,'current':None}
    best=round(min(current),2)
    since=(current_time-timedelta(days=365)).isoformat(timespec='seconds')
    records=cn.execute('''SELECT price,at FROM prices WHERE product=? AND kind IN ('snapshot','historical')
       AND at>=? ORDER BY at ASC LIMIT 25000''',(product,since)).fetchall()
    by_date={}
    for rec in records:
        at=parse_at(rec['at'])
        if not at or at>=current_time or at.date()>=current_time.date():continue
        try:price=float(rec['price'])
        except (ValueError,TypeError):continue
        if price<=0:continue
        date=at.date().isoformat()
        by_date[date]=min(by_date.get(date,float('inf')),price)
    for days in (30,90,365):
        cutoff=(current_time-timedelta(days=days)).date().isoformat()
        values=[p for date,p in by_date.items() if date>=cutoff]
        if len(values)<3:continue
        med=round(float(statistics.median(values)),2)
        if med<=0:continue
        saving=round((med-best)/med*100,1)
        if saving>=20:level,label='excellent','Excelente oportunidade'
        elif saving>=10:level,label='good','Preço abaixo do habitual'
        elif saving>=5:level,label='interesting','Oportunidade interessante'
        elif saving>=-5:level,label='normal','Dentro da faixa habitual'
        else:level,label='above','Acima do histórico'
        return {'ready':True,'label':label,'level':level,'current':best,'median':med,
                'percent_below_median':saving,'sample_days':len(values),
                'period_days':days,'confidence':'maior' if len(values)>=12 else 'limitada',
                'method':'mediana dos menores preços diários realmente coletados; sem frete'}
    return {'ready':False,'reason':'insufficient_history','label':'Histórico insuficiente',
            'current':best,'sample_days':len(by_date),'required_days':3,
            'method':'Precisa de três dias anteriores distintos com cotações reais.'}


def next_due_product(cn,current_time=None):
    current_time=current_time or now_utc()
    cutoff=(current_time-timedelta(hours=24)).isoformat(timespec='seconds')
    row=cn.execute('''SELECT product,MIN(last_auto_at) AS last_auto_at,COUNT(*) AS watchers
        FROM watches GROUP BY product HAVING MIN(last_auto_at)='' OR MIN(last_auto_at)<?
        ORDER BY watchers DESC, last_auto_at ASC LIMIT 1''',(cutoff,)).fetchone()
    return row['product'] if row else None


def mark_scanned(cn,product,at):
    cn.execute('UPDATE watches SET last_auto_at=? WHERE product=?',(at,product))
