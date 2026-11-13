import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from run import Handler
from store import Store


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.store=Store(Path(self.temp.name)/'quota.sqlite3',lambda:1000000)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,method,path,body=None,headers=None):
        connection=HTTPConnection(*self.server.server_address,timeout=3)
        data=None if body is None else json.dumps(body)
        connection.request(method,path,data,headers or ({'Content-Type':'application/json'} if data else {}))
        response=connection.getresponse();raw=response.read();status=response.status;fields=dict(response.getheaders());connection.close()
        return status,fields,json.loads(raw) if raw and fields.get('Content-Type','').startswith('application/json') else raw

    def test_http_denial_retry_and_conflict_contract(self):
        self.assertEqual(self.request('POST','/api/policies',{'name':'api','capacity':1,'rate':2})[0],201)
        request={'request_id':'first','client':'alpha','cost':1}
        self.assertEqual(self.request('POST','/api/policies/api/consume',request)[0],200)
        request['request_id']='second'
        status,headers,body=self.request('POST','/api/policies/api/consume',request)
        self.assertEqual(status,429);self.assertEqual(headers['Retry-After'],'1');self.assertEqual(body['retry_after_us'],500000)
        self.assertTrue(self.request('POST','/api/policies/api/consume',request)[2]['replayed'])
        request['client']='beta';self.assertEqual(self.request('POST','/api/policies/api/consume',request)[0],409)
        self.assertEqual(self.request('GET','/api/policies/api/export')[2]['receipt_count'],2)
        self.assertTrue(self.request('GET','/api/policies/api/audit')[2]['ok'])

    def test_asset_and_request_boundaries(self):
        self.assertEqual(self.request('GET','/')[0],200)
        self.assertEqual(self.request('HEAD','/')[2],b'')
        for path in ['/store.py','/data/quota.sqlite3','/.git/config','/api/policies/missing']:
            self.assertEqual(self.request('GET',path)[0],404)
        self.assertEqual(self.request('GET','/',headers={'Host':'attacker.test'})[0],400)
        self.assertEqual(self.request('POST','/api/policies',{},headers={'Origin':'https://other.test','Content-Type':'application/json'})[0],403)
        self.assertEqual(self.request('POST','/api/policies',{},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('POST','/api/policies',{'padding':'a'*9000})[0],413)
        self.assertEqual(self.request('POST','/api/policies',[])[0],400)
        self.assertEqual(self.request('DELETE','/api/policies')[0],405)


if __name__=='__main__':unittest.main()
