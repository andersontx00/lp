"""Append-only validation events, outlier quarantine, usage accounting.

A title match is internal evidence, not proof of inventory, barcode,
seller reputation, or purchase completion.
"""
from __future__ import annotations
from datetime import datetime, timezone
from statistics import median
import hashlib

def init_schema(cn,postgres=False):
    pk="BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cn.execute(f"""CREATE TABLE IF NOT EXISTS offer_audit_events (
       id {pk}, product TEXT NOT NULL, merchant TEXT NOT NULL,
       variant_key TEXT NOT NULL, title TEXT NOT NULL,
       price DOUBLE PRECISION NOT NULL, url TEXT NOT NULL,
       provider TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
       checked_at TEXT NOT NULL, validator_version TEXT NOT NULL)""")
    cn.execute('CREATE INDEX IF NOT EXISTS audit_by_product ON offer_audit_events(product,checked_at)')
    cn.execute(f"""CREATE TABLE IF NOT EXISTS provider_usage (
       id {pk}, source TEXT NOT NULL, status TEXT NOT NULL,
       product TEXT NOT NULL, checked_at TEXT NOT NULL,
       duration_ms INTEGER NOT NULL, request_units INTEGER NOT NULL,
       returned_offers INTEGER NOT NULL)""")
    cn.execute('CREATE INDEX IF NOT EXISTS usage_by_date ON provider_usage(checked_at,source)')
    cn.execute("""CREATE TABLE IF NOT EXISTS offer_audit_decisions (
        audit_id BIGINT PRIMARY KEY, action TEXT NOT NULL, note TEXT NOT NULL,
        decided_at TEXT NOT NULL)""")
    cn.execute("""CREATE TABLE IF NOT EXISTS collection_attempts (
        product TEXT PRIMARY KEY, last_attempt TEXT NOT NULL,
        retry_after TEXT NOT NULL, status TEXT NOT NULL)""")

def record_audit(cn,name,row,status,reason,variant):
    cn.execute('''INSERT INTO offer_audit_events
        (product,merchant,variant_key,title,price,url,provider,status,reason,checked_at,validator_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (name,str(row.get('source') or '')[:100],str(variant)[:90],
         str(row.get('title') or '')[:210],float(row.get('price') or 0),
         str(row.get('url') or '')[:1600],str(row.get('provider') or '')[:30],
         status,str(reason)[:200],str(row.get('checked_at') or datetime.now(timezone.utc).isoformat()),
         'radar-identity-16.1'))

def log_provider(cn,source,status,product,duration_ms,request_units=1,returned_offers=0):
    cn.execute('''INSERT INTO provider_usage
      (source,status,product,checked_at,duration_ms,request_units,returned_offers)
      VALUES(?,?,?,?,?,?,?)''',
      (str(source)[:50],str(status)[:30],str(product)[:160],
       datetime.now(timezone.utc).isoformat(timespec='seconds'),max(0,int(duration_ms)),
       max(0,int(request_units)),max(0,int(returned_offers))))

def outlier(offer,peers):
    """Quarantine extreme one-off price, without calling it fake."""
    try:
        price=float(offer.get('price'))
        same=[float(x.get('price')) for x in peers
              if x.get('variant_key')==offer.get('variant_key') and float(x.get('price') or 0)>0]
    except (ValueError,TypeError,OverflowError):
        return 'preco_invalido'
    if len(same)<3:return None
    reference=median(same)
    if reference>0 and price < reference*.38:
        return 'preco_muito_abaixo_das_ofertas_comparaveis'
    return None

def summary(cn,days=30):
    cutoff=datetime.now(timezone.utc).date().isoformat()
    rows=cn.execute("""SELECT status,COUNT(*) AS count FROM offer_audit_events
      WHERE checked_at>=? GROUP BY status""",(cutoff[:7]+'-01',)).fetchall()
    counts={r['status']:int(r['count']) for r in rows}
    unresolved=cn.execute("""SELECT COUNT(*) FROM offer_audit_events a
        LEFT JOIN offer_audit_decisions d ON d.audit_id=a.id
        WHERE a.status='pending_review' AND d.audit_id IS NULL
        AND a.checked_at>=?""",(cutoff[:7]+'-01',)).fetchone()[0]
    return {'accepted':counts.get('title_matched',0),'pending':int(unresolved),
            'rejected':counts.get('rejected',0),
            'resolved':counts.get('pending_review',0)-int(unresolved),
            'description':'Eventos do mês atual. Correspondência por título não certifica preço, estoque ou entrega.'}

def usage(cn):
    month=datetime.now(timezone.utc).date().isoformat()[:7]+'-01'
    rows=cn.execute("""SELECT source, status,COUNT(*) AS attempts,
         SUM(request_units) AS requests,SUM(returned_offers) AS offers
         FROM provider_usage WHERE checked_at>=?
         GROUP BY source,status ORDER BY source,status""",(month,)).fetchall()
    return [dict(x) for x in rows]

def resolve_pending(cn,audit_id,action,note):
    """Record a decision without promoting an unverified listing to a live offer."""
    if action not in ('reject','recheck'):
        raise ValueError('Ação de auditoria inválida.')
    if not isinstance(audit_id,int) or audit_id<1:
        raise ValueError('Identificador inválido.')
    note=str(note or '').strip()
    if not 8<=len(note)<=300:
        raise ValueError('Justificativa deve ter de 8 a 300 caracteres.')
    row=cn.execute("SELECT id,status FROM offer_audit_events WHERE id=?",(audit_id,)).fetchone()
    if not row or row['status']!='pending_review':
        raise ValueError('Evento pendente não encontrado.')
    previous=cn.execute('SELECT audit_id FROM offer_audit_decisions WHERE audit_id=?',(audit_id,)).fetchone()
    if previous:
        raise ValueError('Evento já revisado.')
    cn.execute('INSERT INTO offer_audit_decisions(audit_id,action,note,decided_at) VALUES(?,?,?,?)',
        (audit_id,action,note,datetime.now(timezone.utc).isoformat(timespec='seconds')))
    return {'audit_id':audit_id,'action':action,'note':note,
            'published':False,'message':'Decisão registrada; nova coleta é necessária para publicar.'}

def pending_reviews(cn,limit=100):
    rows=cn.execute("""SELECT a.id,a.product,a.merchant,a.variant_key,a.title,
        a.price,a.reason,a.checked_at
        FROM offer_audit_events a LEFT JOIN offer_audit_decisions d ON d.audit_id=a.id
        WHERE a.status='pending_review' AND d.audit_id IS NULL
        ORDER BY a.id DESC LIMIT ?""",(min(100,max(1,int(limit))),)).fetchall()
    return [dict(r) for r in rows]

def record_collection_attempt(cn,product,status,retry_minutes=120):
    """Keep retries durable; a failure must not be recorded as a successful scan."""
    now=datetime.now(timezone.utc)
    from datetime import timedelta
    retry=(now+timedelta(minutes=retry_minutes)).isoformat(timespec='seconds')
    cn.execute("""INSERT INTO collection_attempts(product,last_attempt,retry_after,status)
        VALUES(?,?,?,?) ON CONFLICT(product) DO UPDATE SET
        last_attempt=excluded.last_attempt,retry_after=excluded.retry_after,status=excluded.status""",
        (product,now.isoformat(timespec='seconds'),retry,str(status)[:30]))
