import argparse,json,tempfile
from pathlib import Path
from . import operations,report,qc,integration,batch,custody,transfer
COMMANDS=['calibration-import', 'sample-assign', 'result-release','qc-assess','peak-review','batch-release','custody-assess',
          'transfer-plan-register','transfer-bridge-import']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('transfer-evaluate');arg.add_argument('--db',required=True);arg.add_argument('--plan')
    arg=sub.add_parser('transfer-check');arg.add_argument('--db',required=True)
    for name in ('--old','--new','--method','--at'):arg.add_argument(name,required=True)
    arg.add_argument('--low',required=True,type=float);arg.add_argument('--high',required=True,type=float)
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='transfer-evaluate':print(json.dumps(transfer.evaluate(x.db,x.plan),ensure_ascii=False))
    elif x.command=='transfer-check':
        print(json.dumps(transfer.check(x.db,x.old,x.new,x.method,x.low,x.high,x.at),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,qc.assess,integration.review,
            batch.release,custody.assess,transfer.register,transfer.bridge][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
