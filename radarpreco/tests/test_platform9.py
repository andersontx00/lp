import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime,timedelta,timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import server,accounts,pg_store,variant_rules

class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        server.USE_POSTGRES=False
        server.DB_PATH=Path(self.tmp.name)/'platform9.db'
        server.auth_attempts.clear()
        server.initialize()
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.worker=threading.Thread(target=self.http.serve_forever,daemon=True)
        self.worker.start()
        self.base=f'http://127.0.0.1:{self.http.server_address[1]}'
        self.cookie='';self.csrf=''

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.worker.join(2)
        self.tmp.cleanup()

    def call(self,path,body=None,csrf=False,cookie=True):
        h={'Origin':self.base,'X-Radar-Device':'ab'*24}
        if cookie and self.cookie:h['Cookie']=self.cookie
        if csrf:h['X-Radar-CSRF']=self.csrf
        if body is not None:h['Content-Type']='application/json'
        r=Request(self.base+path,headers=h,data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(r,timeout=8) as res:
                payload=json.loads(res.read())
                if res.headers.get('Set-Cookie'):self.cookie=res.headers.get('Set-Cookie').split(';')[0]
                if payload.get('csrf'):self.csrf=payload['csrf']
                return res.status,payload
        except HTTPError as e:
            return e.code,json.loads(e.read())

    def test_registration_login_favorites_alerts_and_logout(self):
        email='teste@exemplo.com';pwd='a-SENHA-longa-987'
        self.assertEqual(self.call('/api/me')[1]['user'],None)
        code,session=self.call('/api/auth/register',{'email':email,'password':pwd})
        self.assertEqual(code,200);self.assertEqual(session['user']['plan'],'free')
        self.assertEqual(self.call('/api/me')[1]['user']['email'],email)
        self.assertEqual(self.call('/api/favorites',{'favorites':['Logitech G305 LIGHTSPEED']} )[0],403)
        code,r=self.call('/api/favorites',{'favorites':['Logitech G305 LIGHTSPEED','Logitech G305 LIGHTSPEED']},csrf=True)
        self.assertEqual(code,200);self.assertEqual(len(r['favorites']),1)
        code,w=self.call('/api/watch',{'action':'set','product':'Logitech G305 LIGHTSPEED','threshold':10},csrf=True)
        self.assertEqual(code,200);self.assertEqual(w['watches'][0]['threshold'],10)
        self.assertEqual(self.call('/api/watch',{'action':'set','product':'Logitech G305 LIGHTSPEED'},csrf=False)[0],403)
        code,_=self.call('/api/auth/logout',{},csrf=True)
        self.assertEqual(code,200);self.assertIsNone(self.call('/api/me')[1]['user'])
        self.assertEqual(self.call('/api/auth/login',{'email':email,'password':'incorrect-long-pass'})[0],400)
        code,session=self.call('/api/auth/login',{'email':email,'password':pwd})
        self.assertEqual(code,200)
        self.assertEqual(self.call('/api/favorites')[1]['favorites'],['Logitech G305 LIGHTSPEED'])
        self.assertEqual(len(self.call('/api/watch')[1]['watches']),1)

    def test_variants_and_no_fabricated_latest(self):
        self.assertEqual(server.match_score('Lava-louças 10 serviços 220V','Lava louças 10 serviços 127V'),0)
        self.assertEqual(server.match_score('Galaxy S24 FE 256GB','Galaxy S24 FE 128GB'),0)
        self.assertTrue(variant_rules.incompatible('Mouse Logitech G305 branco','Mouse Logitech G305 preto'))
        self.assertEqual(server.shipping_cost({'shipping':'Frete grátis'}),0)
        self.assertIsNone(server.shipping_cost({'shipping':'Consultar CEP'}))
        self.assertFalse(self.call('/api/highlights')[1]['offers'])
        server.store_offer_cache('Mouse teste',[{'source':'Loja teste','price':99,'url':'https://x.test/a',
            'checked_at':(datetime.now(timezone.utc)-timedelta(days=3)).isoformat(timespec='seconds')}])
        self.assertEqual(server.read_offers('Mouse teste'),[])

    def test_postgres_translation(self):
        self.assertEqual(pg_store.translate('SELECT x FROM a WHERE id=?'), 'SELECT x FROM a WHERE id=%s')
        self.assertIn('ON CONFLICT DO NOTHING',pg_store.translate('INSERT OR IGNORE INTO prices(x) VALUES(?)'))
        self.assertIn('ON CONFLICT(product) DO UPDATE',pg_store.translate('INSERT OR REPLACE INTO bright_jobs(product,snapshot_id,created_at,checked_at) VALUES(?,?,?,?)'))
        self.assertEqual(pg_store.HybridRow([('id',4),('name','Loja')])[0],4)

if __name__=='__main__':unittest.main()
