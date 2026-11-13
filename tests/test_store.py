import concurrent.futures
import json
import random
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from store import Store, Conflict, SCALE


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_000_000
        self.path = Path(self.temp.name)/'quota.sqlite3'
        self.store = Store(self.path, lambda: self.now)
        self.store.create({'name': 'api', 'capacity': 5, 'rate': 2})

    def consume(self, request='r1', client='alpha', cost=1):
        return self.store.consume('api', {'request_id': request, 'client': client, 'cost': cost})

    def test_capacity_refill_and_exact_retry_boundary(self):
        self.assertTrue(self.consume(cost=5)['allowed'])
        denied = self.consume('r2')
        self.assertFalse(denied['allowed'])
        self.assertEqual(denied['retry_after_us'], 500000)
        self.now += 499999
        self.assertFalse(self.consume('r3')['allowed'])
        self.now += 1
        self.assertTrue(self.consume('r4')['allowed'])
        self.assertEqual(self.consume('r5')['remaining_microtokens'], 0)

    def test_idle_refill_is_capped_and_client_buckets_are_independent(self):
        self.consume(cost=5)
        self.assertTrue(self.consume('r2', 'beta', 5)['allowed'])
        self.now += 10**12
        self.assertEqual(self.consume('r3', cost=1)['remaining_microtokens'], 4*SCALE)

    def test_backward_clock_clamps_without_double_refill(self):
        self.consume(cost=5)
        self.now -= 500000
        receipt = self.consume('r2')
        self.assertTrue(receipt['clock_clamped'])
        self.assertEqual(receipt['effective_us'], 1000000)
        self.now = 1500000
        self.assertTrue(self.consume('r3')['allowed'])
        self.assertTrue(self.store.audit('api')['ok'])

    def test_idempotency_replays_allowed_and_denied_without_new_credit_effects(self):
        first = self.consume(cost=5)
        denied = self.consume('denied')
        self.now += 10_000_000
        replay = self.consume(cost=5)
        self.assertTrue(replay.pop('replayed'))
        first.pop('replayed')
        self.assertEqual(first, replay)
        self.assertFalse(self.consume('denied')['allowed'])
        self.assertEqual(self.store.snapshot('api')['receipt_count'], 2)
        self.assertTrue(self.consume('fresh')['allowed'])

    def test_conflicting_retry_is_atomic(self):
        self.consume()
        before = self.store.snapshot('api', export=True)
        with self.assertRaises(Conflict):
            self.consume(cost=2)
        self.assertEqual(before, self.store.snapshot('api', export=True))

    def test_reopen_persists_balance_and_receipts(self):
        self.consume(cost=4)
        reopened = Store(self.path, lambda: self.now)
        self.assertEqual(reopened.snapshot('api'), self.store.snapshot('api'))
        self.assertFalse(reopened.consume('api', {'request_id':'r2','client':'alpha','cost':2})['allowed'])

    def test_concurrent_connections_cannot_overspend(self):
        def consume(i):
            return self.consume(f'concurrent-{i}')
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            receipts = list(pool.map(consume, range(20)))
        self.assertEqual(sum(r['allowed'] for r in receipts), 5)
        self.assertEqual(len({r['seq'] for r in receipts}), 20)
        self.assertTrue(self.store.audit('api')['ok'])

    def test_concurrent_identical_retry_consumes_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            receipts = list(pool.map(lambda _: self.consume(), range(12)))
        self.assertEqual(sum(not r['replayed'] for r in receipts), 1)
        self.assertEqual(self.store.snapshot('api')['consumed_tokens'], 1)

    def test_receipt_failure_rolls_back_bucket_change(self):
        self.consume()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        before = self.store.snapshot('api')
        with self.assertRaises(sqlite3.IntegrityError):
            self.consume('r2')
        self.assertEqual(before, self.store.snapshot('api'))

    def test_1000_random_decisions_match_fraction_arithmetic_oracle(self):
        rng = random.Random(41)
        balances = {}; times = {}
        for i in range(1000):
            self.now = max(0, self.now+rng.randint(-50000,300000))
            client = f'client-{i%4}';cost = rng.randint(1,5)
            last = times.get(client,self.now);effective = max(last,self.now)
            balance = min(Fraction(5),balances.get(client,Fraction(5))+Fraction((effective-last)*2,SCALE))
            allowed = balance >= cost
            if allowed: balance -= cost
            balances[client] = balance;times[client] = effective
            actual = self.consume(f'r{i}',client,cost)
            self.assertEqual(actual['allowed'],allowed)
            self.assertEqual(Fraction(actual['remaining_microtokens'],SCALE),balance)
        self.assertTrue(self.store.audit('api')['ok'])

    def test_audit_detects_derived_state_corruption_and_receipt_changes(self):
        self.consume()
        with self.store.connect() as db:
            db.execute('UPDATE buckets SET balance=0')
        self.assertFalse(self.store.audit('api')['ok'])
        with self.store.connect() as db:
            result=json.loads(db.execute('SELECT result FROM receipts').fetchone()[0]);result['allowed']=False
            db.execute('UPDATE receipts SET result=?',(json.dumps(result),))
        self.assertIn('Receipt 1',self.store.audit('api')['errors'][0])

    def test_projection_does_not_mutate_storage(self):
        self.consume(cost=5);self.now+=100000
        snap=self.store.snapshot('api');self.assertEqual(snap['buckets'][0]['balance'],0)
        self.assertEqual(snap['buckets'][0]['projected_microtokens'],200000)
        self.assertEqual(self.store.snapshot('api')['receipt_count'],1)

    def test_strict_validation_and_immutable_policies(self):
        for body in [{'name':'bad name','capacity':5,'rate':2},{'name':'x','capacity':True,'rate':2},{'name':'x','capacity':5,'rate':0},{'name':'x','capacity':5,'rate':2,'extra':1}]:
            with self.assertRaises(ValueError):self.store.create(body)
        with self.assertRaises(Conflict):self.store.create({'name':'api','capacity':5,'rate':2})
        for cost in [0,6,True,1.5]:
            with self.assertRaises(ValueError):self.consume(cost=cost)
        self.assertEqual(self.store.snapshot('api')['receipt_count'],0)


if __name__ == '__main__':
    unittest.main()
