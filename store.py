"""Atomic, durable token buckets with replayable decision receipts."""
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCALE = 1_000_000


class Conflict(ValueError):
    pass


class Missing(ValueError):
    pass


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be an integer from {low} through {high}.')
    return value


def name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}', value):
        raise ValueError(f'{label} must be 1–64 ASCII identifier characters, starting with a letter or digit.')
    return value


def fields(body, expected):
    if not isinstance(body, dict) or set(body) != set(expected):
        raise ValueError('Expected exactly: ' + ', '.join(expected))


def decision(capacity, rate, balance, previous_us, now_us, cost):
    """Pure integer-microtoken transition; backward time never creates credit."""
    effective = max(previous_us, now_us)
    available = min(capacity * SCALE, balance + (effective - previous_us) * rate)
    allowed = available >= cost * SCALE
    remaining = available - cost * SCALE if allowed else available
    deficit = max(0, cost * SCALE - available)
    retry = (deficit + rate - 1) // rate
    return {'observed_us': now_us, 'effective_us': effective,
            'clock_clamped': now_us < previous_us, 'before_microtokens': available,
            'remaining_microtokens': remaining, 'allowed': allowed,
            'retry_after_us': retry}


class Store:
    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: time.time_ns() // 1000)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS policies (
                    name TEXT PRIMARY KEY, capacity INTEGER NOT NULL,
                    rate INTEGER NOT NULL, created_us INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS buckets (
                    policy TEXT NOT NULL REFERENCES policies(name), client TEXT NOT NULL,
                    balance INTEGER NOT NULL, last_us INTEGER NOT NULL,
                    PRIMARY KEY(policy, client));
                CREATE TABLE IF NOT EXISTS receipts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    policy TEXT NOT NULL REFERENCES policies(name),
                    request_id TEXT NOT NULL, client TEXT NOT NULL, cost INTEGER NOT NULL,
                    result TEXT NOT NULL, UNIQUE(policy, request_id));
                CREATE INDEX IF NOT EXISTS receipt_policy ON receipts(policy,seq);
            ''')

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction:
                db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def now(self):
        return integer(self.clock(), 0, 9_000_000_000_000_000, 'Server clock')

    def policy(self, db, key):
        name(key, 'Policy')
        row = db.execute('SELECT * FROM policies WHERE name=?', (key,)).fetchone()
        if row is None:
            raise Missing('Policy not found.')
        return dict(row)

    def create(self, body):
        fields(body, ['name', 'capacity', 'rate'])
        key = name(body['name'], 'Policy')
        capacity = integer(body['capacity'], 1, 1_000_000, 'Capacity')
        rate = integer(body['rate'], 1, 1_000_000, 'Refill tokens per second')
        with self.connect(write=True) as db:
            if db.execute('SELECT 1 FROM policies WHERE name=?', (key,)).fetchone():
                raise Conflict('Policy already exists. Policies are immutable; create another name.')
            if db.execute('SELECT COUNT(*) FROM policies').fetchone()[0] >= 100:
                raise Conflict('This workspace is limited to 100 policies.')
            db.execute('INSERT INTO policies VALUES (?,?,?,?)', (key, capacity, rate, self.now()))
            return self.policy(db, key)

    def list(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM policies ORDER BY name')]

    def consume(self, key, body):
        fields(body, ['request_id', 'client', 'cost'])
        request_id = name(body['request_id'], 'Request ID')
        client = name(body['client'], 'Client')
        with self.connect(write=True) as db:
            policy = self.policy(db, key)
            cost = integer(body['cost'], 1, policy['capacity'], 'Cost')
            previous = db.execute('SELECT * FROM receipts WHERE policy=? AND request_id=?', (key, request_id)).fetchone()
            if previous:
                if previous['client'] != client or previous['cost'] != cost:
                    raise Conflict('Request ID was already used with a different client or cost.')
                return {**json.loads(previous['result']), 'replayed': True}
            if db.execute('SELECT COUNT(*) FROM receipts WHERE policy=?', (key,)).fetchone()[0] >= 10000:
                raise Conflict('Policy receipt limit reached (10,000). Create a new policy.')
            now = self.now()
            bucket = db.execute('SELECT * FROM buckets WHERE policy=? AND client=?', (key, client)).fetchone()
            if bucket is None:
                if db.execute('SELECT COUNT(*) FROM buckets WHERE policy=?', (key,)).fetchone()[0] >= 2000:
                    raise Conflict('Policy client limit reached (2,000).')
                balance, last = policy['capacity'] * SCALE, now
            else:
                balance, last = bucket['balance'], bucket['last_us']
            outcome = decision(policy['capacity'], policy['rate'], balance, last, now, cost)
            db.execute('INSERT INTO buckets VALUES (?,?,?,?) ON CONFLICT(policy,client) DO UPDATE SET balance=excluded.balance,last_us=excluded.last_us', (key, client, outcome['remaining_microtokens'], outcome['effective_us']))
            receipt = {'policy': key, 'request_id': request_id, 'client': client, 'cost': cost, **outcome}
            cursor = db.execute('INSERT INTO receipts(policy,request_id,client,cost,result) VALUES (?,?,?,?,?)', (key, request_id, client, cost, '{}'))
            receipt['seq'] = cursor.lastrowid
            db.execute('UPDATE receipts SET result=? WHERE seq=?', (json.dumps(receipt, sort_keys=True), receipt['seq']))
            return {**receipt, 'replayed': False}

    def snapshot(self, key, export=False):
        with self.connect() as db:
            db.execute('BEGIN')
            policy = self.policy(db, key)
            now = self.now()
            buckets = [dict(r) for r in db.execute('SELECT * FROM buckets WHERE policy=? ORDER BY client', (key,))]
            for bucket in buckets:
                bucket['projected_microtokens'] = min(policy['capacity'] * SCALE, bucket['balance'] + max(0, now-bucket['last_us']) * policy['rate'])
            count = db.execute('SELECT COUNT(*) FROM receipts WHERE policy=?', (key,)).fetchone()[0]
            receipts = [json.loads(r[0]) for r in db.execute('SELECT result FROM receipts WHERE policy=? ORDER BY seq DESC LIMIT ?', (key, 10000 if export else 100))]
            receipts.reverse()
            totals = db.execute("SELECT COUNT(*),COALESCE(SUM(CASE WHEN json_extract(result,'$.allowed') THEN 1 ELSE 0 END),0),COALESCE(SUM(CASE WHEN json_extract(result,'$.allowed') THEN cost ELSE 0 END),0) FROM receipts WHERE policy=?", (key,)).fetchone()
            return {'format': 'quota-quay-evidence-v1', 'policy': policy, 'sampled_us': now,
                    'buckets': buckets, 'receipt_count': count, 'receipts': receipts,
                    'accepted': totals[1], 'denied': totals[0]-totals[1], 'consumed_tokens': totals[2],
                    'receipt_limit': 10000, 'visible_receipts': len(receipts)}

    def audit(self, key):
        data = self.snapshot(key, export=True)
        p = data['policy']; states = {}; errors = []
        for receipt in data['receipts']:
            client = receipt['client']; now = receipt['observed_us']
            balance, last = states.get(client, (p['capacity']*SCALE, now))
            expected = decision(p['capacity'], p['rate'], balance, last, now, receipt['cost'])
            if any(receipt.get(k) != value for k, value in expected.items()):
                errors.append(f"Receipt {receipt['seq']} disagrees with replay.")
            states[client] = (expected['remaining_microtokens'], expected['effective_us'])
        actual = {b['client']: (b['balance'], b['last_us']) for b in data['buckets']}
        if states != actual:
            errors.append('Stored buckets disagree with replayed receipts.')
        return {'ok': not errors, 'checked_receipts': len(data['receipts']), 'checked_clients': len(states), 'errors': errors}
