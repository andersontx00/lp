import sys
import sqlite3
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import alerts_engine as a

class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        self.c.row_factory=sqlite3.Row
        a.init_schema(self.c)
        self.c.execute('''CREATE TABLE prices (product TEXT,series TEXT,price REAL,at TEXT,source TEXT,kind TEXT)''')
        self.t=datetime(2026,11,10,11,0,tzinfo=timezone.utc)
        self.name='Logitech G305 LIGHTSPEED'
        self.device=a.device_key('1f'*24)
        self.c.execute('INSERT INTO watches VALUES(?,?,?,?,?)',(self.device,self.name,10,self.t.isoformat(),''))
    def tearDown(self):self.c.close()
    def test_device_token_privacy_and_length(self):
        self.assertEqual(len(self.device),64)
        with self.assertRaises(ValueError):a.device_key('short')
        with self.assertRaises(ValueError):a.device_key('secret-key-is-not-allowed')
    def test_watch_threshold_validation(self):
        with self.assertRaises(ValueError):a.change_watch(self.c,self.device,self.name,'set',3,self.t.isoformat())
        a.change_watch(self.c,self.device,self.name,'set',15,self.t.isoformat())
        self.assertEqual(a.watch_state(self.c,self.device)['watches'][0]['threshold'],15)
    def test_same_shop_drop_trigger_deduplicates(self):
        self.c.execute('INSERT INTO prices VALUES(?,?,?,?,?,?)',
            (self.name,'Loja • KaBuM!',250,(self.t-timedelta(days=1)).isoformat(),'serpapi','snapshot'))
        updated=(self.t).isoformat()
        n=a.store_drop_alerts(self.c,self.name,[{'source':'KaBuM!','price':199.00}],updated)
        self.assertEqual(n,1)
        self.assertEqual(a.store_drop_alerts(self.c,self.name,[{'source':'KaBuM!','price':199.00}],updated),0)
        state=a.watch_state(self.c,self.device)
        self.assertEqual(state['unread'],1)
        self.assertAlmostEqual(state['alerts'][0]['percent'],20.4,places=1)
        a.mark_read(self.c,self.device,'all',updated)
        self.assertEqual(a.watch_state(self.c,self.device)['unread'],0)
    def test_different_store_not_compared(self):
        self.c.execute('INSERT INTO prices VALUES(?,?,?,?,?,?)',
            (self.name,'Loja • Loja A',250,(self.t-timedelta(days=1)).isoformat(),'serpapi','snapshot'))
        self.assertEqual(a.store_drop_alerts(self.c,self.name,[{'source':'Loja B','price':100}],self.t.isoformat()),0)
    def test_no_alert_for_increase_small_drop_or_too_old(self):
        for i,(before,after) in enumerate([(250,260),(250,240)]):
            self.c.execute('INSERT INTO prices VALUES(?,?,?,?,?,?)',
                (self.name,f'Loja • Shop {i}',before,(self.t-timedelta(days=1)).isoformat(),'serpapi','snapshot'))
            self.assertEqual(a.store_drop_alerts(self.c,self.name,[{'source':f'Shop {i}','price':after}],self.t.isoformat()),0)
        self.c.execute('INSERT INTO prices VALUES(?,?,?,?,?,?)',
            (self.name,'Loja • Loja C',250,(self.t-timedelta(days=40)).isoformat(),'serpapi','snapshot'))
        self.assertEqual(a.store_drop_alerts(self.c,self.name,[{'source':'Loja C','price':150}],self.t.isoformat()),0)
    def test_insight_uses_distinct_real_days_not_quotes_count(self):
        for d in (1,4,9):
            for price in (250,270,300):
                self.c.execute('INSERT INTO prices VALUES(?,?,?,?,?,?)',
                    (self.name,'Loja • Loja A',price,(self.t-timedelta(days=d)).isoformat(),'serpapi','snapshot'))
        res=a.opportunity(self.c,self.name,[{'price':180,'checked_at':self.t.isoformat()}],self.t)
        self.assertTrue(res['ready'])
        self.assertEqual(res['sample_days'],3)
        self.assertEqual(res['median'],250)
        self.assertEqual(res['level'],'excellent')
        self.assertAlmostEqual(res['percent_below_median'],28)
    def test_insufficient_days_and_stale_price(self):
        for d in [1,1,1,2]:
            self.c.execute('INSERT INTO prices VALUES(?,?,?,?,?,?)',
                    (self.name,'Loja • A',250,(self.t-timedelta(days=d)).isoformat(),'serpapi','snapshot'))
        res=a.opportunity(self.c,self.name,[{'price':200,'checked_at':self.t.isoformat()}],self.t)
        self.assertFalse(res['ready'])
        self.assertEqual(res['sample_days'],2)
        res=a.opportunity(self.c,self.name,[{'price':200,'checked_at':(self.t-timedelta(days=2)).isoformat()}],self.t)
        self.assertFalse(res['ready'])
        self.assertEqual(res['reason'],'no_recent_price')
    def test_scheduler_prioritizes_watches_not_other_products(self):
        name=a.next_due_product(self.c,self.t)
        self.assertEqual(name,self.name)
        a.mark_scanned(self.c,self.name,self.t.isoformat())
        self.assertIsNone(a.next_due_product(self.c,self.t))

if __name__=='__main__': unittest.main()
