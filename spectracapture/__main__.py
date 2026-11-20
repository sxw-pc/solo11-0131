import argparse,json,tempfile
from pathlib import Path
from . import operations,report,qc,integration,batch,custody,trust
from .ingest import capture,verify_source
from .store import connect
COMMANDS=['calibration-import', 'sample-assign', 'result-release','qc-assess','peak-review','batch-release','custody-assess']
def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))
def _trust_import(db_path,file):
    doc=_read(file)
    db=connect(db_path)
    try:
        with db:
            trust.import_trust_root(db,str(doc['instrument_id']),
                                    trust.b64d(str(doc['root_public_key'])))
        return {'status':'installed','instrument_id':doc['instrument_id']}
    finally:db.close()
def _rotation_import(db_path,file):
    db=connect(db_path)
    try:
        with db:
            result=trust.import_rotation(db,_read(file))
        return result
    finally:db.close()
def _revocation_import(db_path,file):
    db=connect(db_path)
    try:
        with db:
            result=trust.import_revocation(db,_read(file))
        return result
    finally:db.close()
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    arg=sub.add_parser('trust-import',help='pre-install an instrument root public key trust anchor')
    arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('rotation-import',help='validate and record a dual-signed key rotation proof')
    arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('revocation-import',help='apply a root-signed key theft bulletin by sequence range')
    arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('verify-source',help='adjudicate one export under both archive and business contracts')
    arg.add_argument('--db',required=True);arg.add_argument('file')
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='trust-import':print(json.dumps(_trust_import(x.db,x.file),ensure_ascii=False))
    elif x.command=='rotation-import':print(json.dumps(_rotation_import(x.db,x.file),ensure_ascii=False))
    elif x.command=='revocation-import':print(json.dumps(_revocation_import(x.db,x.file),ensure_ascii=False))
    elif x.command=='verify-source':print(json.dumps(verify_source(x.db,x.file),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,qc.assess,integration.review,
            batch.release,custody.assess][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
