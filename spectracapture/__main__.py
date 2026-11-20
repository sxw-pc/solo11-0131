import argparse,json,tempfile
from pathlib import Path
from . import operations,report,qc,integration,batch,custody,archive
COMMANDS=['calibration-import', 'sample-assign', 'result-release','qc-assess','peak-review','batch-release','custody-assess']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    arg=sub.add_parser('archive-migrate');arg.add_argument('--db',required=True);arg.add_argument('--store',required=True)
    arg.add_argument('--source',type=int)
    arg=sub.add_parser('archive-assess');arg.add_argument('--db',required=True);arg.add_argument('--source',type=int)
    arg.add_argument('--now')
    arg=sub.add_parser('retention-attach');arg.add_argument('--db',required=True);arg.add_argument('--source',type=int,required=True)
    arg.add_argument('--ref',required=True);arg.add_argument('--until',required=True)
    arg=sub.add_parser('hold-submit');arg.add_argument('--db',required=True);arg.add_argument('--source',type=int,required=True)
    arg.add_argument('--order',required=True);arg.add_argument('--reason',default='preservation_order')
    arg=sub.add_parser('hold-release');arg.add_argument('--db',required=True);arg.add_argument('--order',required=True)
    arg.add_argument('--reason',default='hold_released')
    arg=sub.add_parser('destroy-request');arg.add_argument('--db',required=True);arg.add_argument('--source',type=int,required=True)
    arg.add_argument('--request',required=True)
    arg=sub.add_parser('cleanup');arg.add_argument('--db',required=True);arg.add_argument('--store',required=True)
    arg.add_argument('--now')
    arg=sub.add_parser('key-register');arg.add_argument('--db',required=True);arg.add_argument('--digest',required=True)
    arg.add_argument('--key',required=True);arg.add_argument('--policy',default='erase')
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='archive-migrate':print(json.dumps(archive.migrate(x.db,x.store,source_id=x.source),ensure_ascii=False))
    elif x.command=='archive-assess':print(json.dumps(archive.assess(x.db,source_id=x.source,now=x.now),ensure_ascii=False))
    elif x.command=='retention-attach':print(json.dumps(archive.attach_ref(x.db,x.source,x.ref,x.until),ensure_ascii=False))
    elif x.command=='hold-submit':print(json.dumps(archive.submit_hold(x.db,x.order,x.source,x.reason),ensure_ascii=False))
    elif x.command=='hold-release':print(json.dumps(archive.release_hold(x.db,x.order,x.reason),ensure_ascii=False))
    elif x.command=='destroy-request':print(json.dumps(archive.request_disposal(x.db,x.request,x.source),ensure_ascii=False))
    elif x.command=='cleanup':print(json.dumps(archive.cleanup(x.db,x.store,now=x.now),ensure_ascii=False))
    elif x.command=='key-register':print(json.dumps(archive.register_key(x.db,x.digest,x.key,x.policy),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,qc.assess,integration.review,
            batch.release,custody.assess][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
