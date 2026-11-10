import json
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import server

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        server.DB_PATH=Path(self.temp.name)/'alerts_test.db'
        server.initialize()
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.url='http://127.0.0.1:'+str(self.http.server_address[1])
        self.device='aa'*24
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join(timeout=2)
        self.temp.cleanup()
    def ask(self,endpoint,body=None):
        headers={'X-Radar-Device':self.device}
        if body is not None:headers['Content-Type']='application/json'
        req=Request(self.url+endpoint,data=json.dumps(body).encode('utf8') if body is not None else None,headers=headers)
        with urlopen(req,timeout=5) as r:return r.status,json.loads(r.read())
    def test_create_update_remove_and_read_alerts(self):
        name='Logitech G305 LIGHTSPEED'
        self.assertEqual(self.ask('/api/status')[1]['version'],'9.0')
        code,data=self.ask('/api/watch',{'action':'set','product':name,'threshold':10})
        self.assertEqual(code,200)
        self.assertEqual(len(data['watches']),1)
        server.record_merged(name,[{'source':'KaBuM!','price':250,'title':name,'url':'https://example.org/a'}],
            '2026-11-09T12:00:00+00:00')
        server.record_merged(name,[{'source':'KaBuM!','price':200,'title':name,'url':'https://example.org/a'}],
            '2026-11-10T12:00:00+00:00')
        code,state=self.ask('/api/watch')
        self.assertEqual(state['unread'],1)
        self.assertAlmostEqual(state['alerts'][0]['percent'],20)
        self.assertEqual(self.ask('/api/watch',{'action':'read','id':'all'})[1]['unread'],0)
        self.assertEqual(len(self.ask('/api/watch',{'action':'remove','product':name})[1]['watches']),0)
    def test_history_includes_insight_status(self):
        code,data=self.ask('/api/history?q=Logitech%20G305%20LIGHTSPEED&days=7')
        self.assertEqual(code,200)
        self.assertIn('insight',data)
        self.assertFalse(data['insight']['ready'])

if __name__=='__main__':unittest.main()
