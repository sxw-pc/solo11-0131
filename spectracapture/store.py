"""SQLite ledger storage with content-addressed, cross-replica evidence identity.

Local ids (``export_file.id`` and the business-table ``id`` columns) never leave
one ledger.  Anything crossing a replica boundary is addressed by a digest::

    source hash     sha256(raw bytes)
    commit id       sha256(replica, seq, previous commit id, source hash)
    evidence id     sha256(family, entity key, body digest, source hash,
                           version, status, reason)
    decision id     sha256(subsystem, subject, status, reason, source hash)

``commit_chain`` is the per-replica append-only commit chain.  ``evidence`` is
the merged, append-only evidence graph: immutable nodes, per-replica origins
and explicit parent links.  Business tables remain the local materialised view
used by the analyst commands; merging never rewrites them.

Two replicas importing identical bytes converge on one source and one evidence
node (two arrival paths).  The same business key with different raw bytes
yields two concurrent branches that are never overwritten.
"""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

TABLES = ['calibration_export', 'calibration_point', 'sample_export', 'peak_assignment', 'result_batch',
          'released_result', 'qc_batch', 'qc_control', 'peak_review', 'integration_revision',
          'release_manifest', 'release_item', 'custody_case', 'custody_transfer', 'release_recall',
          'review_resolution', 'retraction']

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS export_file(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,hash TEXT NOT NULL,body BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,replica_id TEXT,seq INTEGER,prev TEXT,commit_id TEXT,UNIQUE(kind,hash));
CREATE TABLE IF NOT EXISTS export_record(id INTEGER PRIMARY KEY,source_id INT NOT NULL REFERENCES export_file(id),line_no INT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,source_id INT REFERENCES export_file(id),code TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_id INT REFERENCES export_file(id),created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS calibration_export(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calibration_point(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sample_export(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peak_assignment(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS result_batch(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS released_result(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qc_batch(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qc_control(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peak_review(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS integration_revision(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_manifest(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_item(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custody_case(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custody_transfer(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_recall(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_resolution(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS retraction(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
"""

EXCHANGE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS replica_state(replica_id TEXT PRIMARY KEY,seq INTEGER NOT NULL DEFAULT 0,head TEXT NOT NULL,updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS remote_state(replica_id TEXT PRIMARY KEY,seq INTEGER NOT NULL,head TEXT NOT NULL,updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS commit_chain(replica_id TEXT NOT NULL,seq INTEGER NOT NULL,prev TEXT NOT NULL,commit_id TEXT NOT NULL,source_hash TEXT NOT NULL,kind TEXT NOT NULL,version TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(replica_id,seq),UNIQUE(commit_id));
CREATE TABLE IF NOT EXISTS arrival(source_hash TEXT NOT NULL,commit_id TEXT NOT NULL,replica_id TEXT NOT NULL,seq INTEGER NOT NULL,local_source_id INT,received TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(source_hash,commit_id));
CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY,family TEXT NOT NULL,entity_key TEXT NOT NULL,source_hash TEXT NOT NULL,content_hash TEXT NOT NULL,body TEXT NOT NULL,version TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_origin(evidence_id TEXT NOT NULL,replica_id TEXT NOT NULL,seq INTEGER NOT NULL,ordinal INTEGER NOT NULL,commit_id TEXT NOT NULL,PRIMARY KEY(evidence_id,replica_id));
CREATE TABLE IF NOT EXISTS evidence_link(child TEXT NOT NULL,parent TEXT NOT NULL,relation TEXT NOT NULL DEFAULT 'precedes',PRIMARY KEY(child,parent,relation));
CREATE TABLE IF NOT EXISTS decision_evidence(decision_id TEXT NOT NULL,replica_id TEXT NOT NULL,seq INTEGER NOT NULL,commit_id TEXT NOT NULL,subsystem TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_hash TEXT NOT NULL,PRIMARY KEY(decision_id,replica_id));
CREATE TABLE IF NOT EXISTS merge_log(id INTEGER PRIMARY KEY,package_id TEXT,origin TEXT,status TEXT NOT NULL,reason TEXT NOT NULL,cursor TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS exchange_receipt(package_id TEXT PRIMARY KEY,origin TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,applied INTEGER NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS pending_package(package_id TEXT PRIMARY KEY,origin TEXT NOT NULL,payload TEXT NOT NULL,missing TEXT NOT NULL,received TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def digest_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def body_digest(body):
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()


def genesis(replica):
    return digest_text('genesis:' + replica)


def commit_id(replica, seq, prev, source_hash):
    return digest_text(canonical(['commit', replica, seq, prev, source_hash]))


def evidence_id(family, key, body, source_hash, version, status, reason):
    if family not in TABLES:
        raise ValueError('unknown_table')
    return digest_text(canonical([family, str(key), body_digest(body), source_hash,
                                  version, status, reason]))


def decision_id(subsystem, subject, status, reason, source_hash):
    return digest_text(canonical(['decision', str(subsystem), str(subject), status, reason, source_hash]))


def _add_columns(db, table, columns):
    have = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
    for name, decl in columns.items():
        if name not in have:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')


def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.executescript(EXCHANGE_SCHEMA)
    # Ledgers written before the exchange feature have no replica columns.
    _add_columns(db, 'export_file', {'replica_id': 'TEXT', 'seq': 'INTEGER', 'prev': 'TEXT', 'commit_id': 'TEXT'})
    db.execute('CREATE INDEX IF NOT EXISTS idx_export_file_hash ON export_file(hash)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_export_file_hash_unique ON export_file(hash)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_entity ON evidence(family,entity_key,evidence_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_origin_commit ON evidence_origin(replica_id,seq,ordinal)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_link_child ON evidence_link(child)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_link_parent ON evidence_link(parent)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_decision_identity ON decision_log(subsystem,subject,status,reason,source_id)')
    rid = 'replica-' + uuid.uuid4().hex
    if db.execute('SELECT 1 FROM replica_state').fetchone() is None:
        db.execute('INSERT INTO replica_state(replica_id,seq,head) VALUES (?,?,?)', (rid, 0, genesis(rid)))
        db.commit()
    return db


# --------------------------------------------------------------------------
# Replica identity and the local commit chain
# --------------------------------------------------------------------------

def replica_id(db):
    return db.execute('SELECT replica_id FROM replica_state ORDER BY rowid LIMIT 1').fetchone()['replica_id']


def state(db):
    rid = replica_id(db)
    row = db.execute('SELECT * FROM replica_state WHERE replica_id=?', (rid,)).fetchone()
    return {'replica_id': rid, 'seq': row['seq'], 'head': row['head']}


def source_row(db, source_id):
    return db.execute('SELECT * FROM export_file WHERE id=?', (source_id,)).fetchone()


def commit_capture(db, kind, version, raw):
    """Insert raw bytes as one atomic, chained local commit.

    Returns ``(source_id, created)``.  Identical bytes are a no-op and never
    advance the chain.
    """
    digest = hashlib.sha256(raw).hexdigest()
    old = db.execute('SELECT id FROM export_file WHERE hash=?', (digest,)).fetchone()
    if old:
        return old['id'], False
    rid = replica_id(db)
    current = state(db)
    seq = current['seq'] + 1
    cid = commit_id(rid, seq, current['head'], digest)
    source_id = db.execute('INSERT INTO export_file(kind,version,hash,body,replica_id,seq,prev,commit_id) '
                           'VALUES (?,?,?,?,?,?,?,?)',
                           (kind, version, digest, raw, rid, seq, current['head'], cid)).lastrowid
    payload = json.loads(raw)
    for i, item in enumerate(payload.get('rows', []), 1):
        identity = str(item.get('id', i))
        db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                   (source_id, i, identity, canonical(item), 'captured'))
    db.execute('INSERT OR IGNORE INTO commit_chain(replica_id,seq,prev,commit_id,source_hash,kind,version) '
               'VALUES (?,?,?,?,?,?,?)', (rid, seq, current['head'], cid, digest, kind, version))
    db.execute('INSERT OR IGNORE INTO arrival(source_hash,commit_id,replica_id,seq,local_source_id) '
               'VALUES (?,?,?,?,?)', (digest, cid, rid, seq, source_id))
    db.execute('UPDATE replica_state SET seq=?,head=?,updated=CURRENT_TIMESTAMP WHERE replica_id=?',
               (seq, cid, rid))
    return source_id, True


# --------------------------------------------------------------------------
# Evidence graph
# --------------------------------------------------------------------------

def _source_context(db, source_id):
    row = source_row(db, source_id)
    if not row or not row['replica_id']:
        raise ValueError('source_not_committed')
    return row


def _next_ordinal(db, replica, seq):
    row = db.execute('SELECT COALESCE(MAX(ordinal),0) AS n FROM evidence_origin WHERE replica_id=? AND seq=?',
                     (replica, seq)).fetchone()
    return row['n'] + 1


def emit_evidence(db, family, key, body, status, reason, version, source_id, parents=()):
    """Append one evidence node to the graph (idempotent) and link parents."""
    if family not in TABLES:
        raise ValueError('unknown_table')
    src = _source_context(db, source_id)
    eid = evidence_id(family, key, body, src['hash'], version, status, reason)
    db.execute('INSERT OR IGNORE INTO evidence(evidence_id,family,entity_key,source_hash,content_hash,body,version,status,reason) '
               'VALUES (?,?,?,?,?,?,?,?,?)',
               (eid, family, str(key), src['hash'], body_digest(body), canonical(body), version, status, reason))
    db.execute('INSERT OR IGNORE INTO evidence_origin(evidence_id,replica_id,seq,ordinal,commit_id) '
               'VALUES (?,?,?,?,?)',
               (eid, src['replica_id'], src['seq'], _next_ordinal(db, src['replica_id'], src['seq']),
                src['commit_id']))
    for parent in parents:
        if parent and parent != eid and db.execute('SELECT 1 FROM evidence WHERE evidence_id=?', (parent,)).fetchone():
            db.execute('INSERT OR IGNORE INTO evidence_link(child,parent,relation) VALUES (?,?,?)',
                       (eid, parent, 'precedes'))
    return eid


def emit_decision(db, subsystem, subject, status, reason, source_id):
    src = _source_context(db, source_id)
    did = decision_id(subsystem, subject, status, reason, src['hash'])
    db.execute('INSERT OR IGNORE INTO decision_evidence(decision_id,replica_id,seq,commit_id,subsystem,subject,status,reason,source_hash) '
               'VALUES (?,?,?,?,?,?,?,?,?)',
               (did, src['replica_id'], src['seq'], src['commit_id'], subsystem, str(subject), status, reason, src['hash']))
    return did


def prior_evidence(db, family, key, replica):
    row = db.execute('SELECT e.evidence_id FROM evidence e JOIN evidence_origin o ON o.evidence_id=e.evidence_id '
                     'WHERE e.family=? AND e.entity_key=? AND o.replica_id=? '
                     'ORDER BY o.seq DESC, o.ordinal DESC LIMIT 1',
                     (family, str(key), replica)).fetchone()
    return row['evidence_id'] if row else None


def save(db, table, key, source_id, version, body, status, reason):
    if table not in TABLES:
        raise ValueError('unknown_table')
    encoded = canonical(body)
    old = db.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    if old and (old['version'], old['body']) != (version, encoded):
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                   (source_id, 'identity_conflict', f'{table}:{key}'))
        raise ValueError('identity_conflict')
    src = _source_context(db, source_id)
    new_id = evidence_id(table, key, body, src['hash'], version, status, reason)
    existing = db.execute('SELECT 1 FROM evidence WHERE evidence_id=?', (new_id,)).fetchone()
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
               (key, source_id, version, encoded, status, reason))
    if existing:
        return new_id
    parent = prior_evidence(db, table, key, src['replica_id'])
    parents = (parent,) if parent and parent != new_id else ()
    return emit_evidence(db, table, key, body, status, reason, version, source_id, parents)


def revise(db, table, key, source_id, version, body, status, reason, parents=()):
    """Record an explicit revision (e.g. a dual-analyst peak decision).

    The previous version stays in the evidence graph; the new node is linked to
    it.  Local materialised rows are updated so subsequent local commands keep
    working.
    """
    if table not in TABLES:
        raise ValueError('unknown_table')
    src = _source_context(db, source_id)
    previous = prior_evidence(db, table, key, src['replica_id'])
    chain = ([previous] if previous else []) + [p for p in parents if p]
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET body=excluded.body,status=excluded.status,'
               'reason=excluded.reason,version=excluded.version',
               (key, source_id, version, canonical(body), status, reason))
    return emit_evidence(db, table, key, body, status, reason, version, source_id, chain)


def head_evidence(db, table, key, replica=None, status=None):
    """Latest evidence id for a business key, optionally scoped to a replica."""
    sql = ('SELECT e.evidence_id FROM evidence e JOIN evidence_origin o ON o.evidence_id=e.evidence_id '
           'WHERE e.family=? AND e.entity_key=?')
    args = [table, str(key)]
    if replica is not None:
        sql += ' AND o.replica_id=?'
        args.append(replica)
    if status is not None:
        sql += ' AND e.status=?'
        args.append(status)
    sql += ' ORDER BY o.seq DESC, o.ordinal DESC LIMIT 1'
    row = db.execute(sql, args).fetchone()
    return row['evidence_id'] if row else None


def decide(db, subsystem, subject, status, reason, source_id):
    old = db.execute('SELECT 1 FROM decision_log WHERE subsystem=? AND subject=? AND status=? AND reason=? AND source_id=?',
                     (subsystem, str(subject), status, reason, source_id)).fetchone()
    if not old:
        db.execute('INSERT INTO decision_log(subsystem,subject,status,reason,source_id) VALUES (?,?,?,?,?)',
                   (subsystem, str(subject), status, reason, source_id))
    did = emit_decision(db, subsystem, subject, status, reason, source_id)
    return {'subject': subject, 'status': status, 'reason': reason, 'decision_id': did}


def link(db, child, parent, relation='precedes'):
    db.execute('INSERT OR IGNORE INTO evidence_link(child,parent,relation) VALUES (?,?,?)',
               (child, parent, relation))

