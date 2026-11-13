"""Offline database acceptance tests. Never touches the connected Render Postgres.

The running service swaps to a temporary SQLite file only during startup
before any HTTP listener or background worker exists.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
import alerts_engine

def run(server):
    previous_db=server.DB_PATH
    previous_backend=server.USE_POSTGRES
    counter=0
    def check(condition,label):
        nonlocal counter
        counter+=1
        if not condition:raise AssertionError('RADAR_DB_TEST '+label)
    with tempfile.TemporaryDirectory(prefix='radar-quality-test-') as folder:
        try:
            server.DB_PATH=Path(folder)/'quality.sqlite3'
            server.USE_POSTGRES=False
            server.initialize()
            name='Olympikus Corre 4 tamanho 42'
            source='Loja-teste'
            now=datetime.now(timezone.utc)
            earlier=(now-timedelta(minutes=3)).isoformat(timespec='seconds')
            later=(now-timedelta(minutes=2)).isoformat(timespec='seconds')
            old={'title':'Tênis Olympikus Corre 4 tamanho 42', 'price':400,
                 'source':source,'url':'https://example.org/corre4-42',
                 'provider':'test','checked_at':earlier}
            newer={**old,'price':280,'checked_at':later}
            bad={**old,'title':'Tênis Olympikus Corre 4 tamanho 41','price':29,'checked_at':later}
            with server.lock,server.db_conn() as cn:
                alerts_engine.change_watch(cn,'test-browser',name,'set',10,earlier,300)
            r1=server.record_merged(name,[old],earlier)
            check(len(r1)==1 and r1[0]['price']==400,'initial variant offer stored')
            check(r1[0]['variant_key']=='shoe_size=42','variant key')
            server.record_merged(name,[bad],later)
            still=server.read_offers(name)
            check(len(still)==1 and still[0]['price']==400,'wrong size must not change price')
            r2=server.record_merged(name,[newer],later)
            check(len(r2)==1 and r2[0]['price']==280,'lower valid price')
            history=server.read_history(name,'7')
            check(len(history['points'])==2,'two genuine price points in history')
            check(history['points'][0]['series'].endswith(' • shoe_size=42'),'variant series')
            check(history['insight']['ready'] is False,'insufficient history cannot become opportunity')
            with server.lock,server.db_conn() as cn:
                state=alerts_engine.watch_state(cn,'test-browser')
                audit=cn.execute("SELECT status,COUNT(*) AS n FROM offer_audit_events GROUP BY status").fetchall()
                variants=cn.execute("SELECT COUNT(*) FROM offer_variants").fetchone()[0]
                target=cn.execute("SELECT COUNT(*) FROM price_alerts WHERE merchant LIKE 'Meta:%'").fetchone()[0]
                percentage=cn.execute("SELECT COUNT(*) FROM price_alerts WHERE merchant NOT LIKE 'Meta:%'").fetchone()[0]
            check(state['unread']>=2,'both alerts delivered')
            check(target==1,'target triggered exactly once')
            check(percentage==1,'percentage triggered exactly once')
            check(variants==1,'variant price cache')
            check(any(x['status']=='rejected' for x in audit),'wrong-size audit event')
            server.record_merged(name,[newer],later)
            with server.lock,server.db_conn() as cn:
                target2=cn.execute("SELECT COUNT(*) FROM price_alerts WHERE merchant LIKE 'Meta:%'").fetchone()[0]
            check(target2==1,'same target must not notify again')
            # Two size variants on one model are different merchant/variant records.
            generic='Olympikus Corre 4'
            offer_41={**old,'title':'Tênis Olympikus Corre 4 tamanho 41','price':320}
            offer_42={**old,'title':'Tênis Olympikus Corre 4 tamanho 42','price':340}
            both=server.record_merged(generic,[offer_41,offer_42],earlier)
            check(len(both)==2,'two sizes from one shop must not overwrite')
            check(server.read_history(generic,'7')['insight']['reason']=='mixed_variants','do not compare mixed size history')
            with server.lock,server.db_conn() as cn:
                reports=cn.execute('SELECT COUNT(*) FROM provider_usage').fetchone()[0]
            check(reports==0,'offline tests never call paid providers')
            with server.lock,server.db_conn() as cn:
                # A review decision is traceable and must not publish an unverified price.
                server.audit_runtime.record_audit(cn,name,bad,'pending_review','anomaly','shoe_size=42')
                pending=server.audit_runtime.pending_reviews(cn)
                check(len(pending)==1,'new unresolved audit appears in queue')
                resolved=server.audit_runtime.resolve_pending(
                    cn,pending[0]['id'],'recheck','Confirmar preço na origem')
                check(resolved['published'] is False,'review cannot bypass validation')
                check(server.audit_runtime.pending_reviews(cn)==[],'resolved audit leaves queue')
                try:
                    server.audit_runtime.resolve_pending(cn,pending[0]['id'],'reject','Decisão duplicada')
                except ValueError:
                    pass
                else:
                    raise AssertionError('duplicate audit decision accepted')
                check(True,'duplicate review blocked')
                # Failure is a retryable attempt, not a successful collection.
                due=alerts_engine.next_due_product(cn)
                check(bool(due),'a watched item is initially due')
                server.audit_runtime.record_collection_attempt(cn,due,'error',120)
                check(alerts_engine.next_due_product(cn)!=due,'failed item held in retry window')
                cn.execute('UPDATE collection_attempts SET retry_after=? WHERE product=?',
                    ((now-timedelta(minutes=1)).isoformat(timespec='seconds'),due))
                check(alerts_engine.next_due_product(cn)==due,'retry eligible after cooldown')
                alerts_engine.mark_scanned(cn,due,server.timestamp())
                check(alerts_engine.next_due_product(cn)!=due,'successful scan is no longer due')
            return counter
        finally:
            server.DB_PATH=previous_db
            server.USE_POSTGRES=previous_backend
