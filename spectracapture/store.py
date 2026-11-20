import sqlite3
from pathlib import Path
TABLES=['calibration_export', 'calibration_point', 'sample_export', 'peak_assignment', 'result_batch', 'released_result', 'qc_batch', 'qc_control', 'peak_review', 'integration_revision', 'release_manifest', 'release_item', 'custody_case', 'custody_transfer', 'release_recall']
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS export_file(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,hash TEXT NOT NULL,body BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(kind,hash));
CREATE TABLE IF NOT EXISTS export_record(id INTEGER PRIMARY KEY,source_id INT NOT NULL REFERENCES export_file(id),line_no INT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,source_id INT REFERENCES export_file(id),code TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_id INT REFERENCES export_file(id),created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(subsystem,subject,status,reason,source_id));
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
-- Pre-installed instrument trust roots (one Ed25519 root public key per instrument).
CREATE TABLE IF NOT EXISTS trust_root(instrument_id TEXT PRIMARY KEY,public_key BLOB NOT NULL,key_id TEXT NOT NULL,imported_at TEXT DEFAULT CURRENT_TIMESTAMP);
-- Keys currently authorized to sign exports for an instrument, including the root key at sequence 1.
CREATE TABLE IF NOT EXISTS signing_key(instrument_id TEXT NOT NULL,key_id TEXT NOT NULL,public_key BLOB NOT NULL,from_sequence INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'active',PRIMARY KEY(instrument_id,key_id));
-- Key rotation proofs: valid only when both the old key and the trust root sign the same statement.
CREATE TABLE IF NOT EXISTS key_rotation(id INTEGER PRIMARY KEY,instrument_id TEXT NOT NULL,old_key_id TEXT NOT NULL,new_key_id TEXT NOT NULL,new_public_key BLOB NOT NULL,from_sequence INTEGER NOT NULL,old_signature BLOB,root_signature BLOB,status TEXT NOT NULL,reason TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(instrument_id,new_key_id));
-- Cryptographic source proof attached to each captured export (present=0 when a strict store receives an unsigned file).
CREATE TABLE IF NOT EXISTS source_proof(source_id INTEGER PRIMARY KEY REFERENCES export_file(id),instrument_id TEXT,sequence INTEGER,key_id TEXT,prev_digest TEXT,payload_digest TEXT NOT NULL,signature BLOB,present INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_proof_identity ON source_proof(instrument_id,sequence,payload_digest) WHERE instrument_id IS NOT NULL;
-- Same instrument + sequence with two distinct validly signed payloads; every successor is frozen.
CREATE TABLE IF NOT EXISTS device_fork(id INTEGER PRIMARY KEY,instrument_id TEXT NOT NULL,sequence INTEGER NOT NULL,source_id_a INTEGER NOT NULL,source_id_b INTEGER NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(instrument_id,sequence,source_id_a,source_id_b));
-- Current two-contract source verdict for each export; never collapsed into one boolean.
CREATE TABLE IF NOT EXISTS source_verdict(source_id INTEGER PRIMARY KEY REFERENCES export_file(id),archive_status TEXT NOT NULL,archive_reason TEXT NOT NULL,business_status TEXT NOT NULL,business_reason TEXT NOT NULL,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
-- Append-only verdict history: late rotations/revocations add rows, old verdicts and usage records stay.
CREATE TABLE IF NOT EXISTS source_verdict_history(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL,contract TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,detail TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP);
-- Key theft bulletins. Impact is the instrument sequence range, never wall-clock time claimed by exports.
CREATE TABLE IF NOT EXISTS revocation_bulletin(id INTEGER PRIMARY KEY,bulletin_id TEXT NOT NULL UNIQUE,instrument_id TEXT NOT NULL,issuer TEXT NOT NULL,key_id TEXT,first_sequence INTEGER NOT NULL,last_sequence INTEGER NOT NULL,signature BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
def save(db,table,key,source_id,version,body,status,reason):
    import json
    if table not in TABLES:raise ValueError('unknown_table')
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    old=db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    if old and (old['version'],old['body'])!=(version,encoded):
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',(source_id,'identity_conflict',f'{table}:{key}'))
        raise ValueError('identity_conflict')
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
               (key,source_id,version,encoded,status,reason))
def decide(db,subsystem,subject,status,reason,source_id):
    db.execute('INSERT OR IGNORE INTO decision_log(subsystem,subject,status,reason,source_id) VALUES (?,?,?,?,?)',
               (subsystem,subject,status,reason,source_id))
    return {'subject':subject,'status':status,'reason':reason}
