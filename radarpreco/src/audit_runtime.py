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
    return {'accepted':counts.get('title_matched',0),'pending':counts.get('pending_review',0),
            'rejected':counts.get('rejected',0),
            'description':'Eventos de auditoria do mês atual. Status verificado não significa certificação externa.'}

def usage(cn):
    month=datetime.now(timezone.utc).date().isoformat()[:7]+'-01'
    rows=cn.execute("""SELECT source, status,COUNT(*) AS attempts,
         SUM(request_units) AS requests,SUM(returned_offers) AS offers
         FROM provider_usage WHERE checked_at>=?
         GROUP BY source,status ORDER BY source,status""",(month,)).fetchall()
    return [dict(x) for x in rows]
