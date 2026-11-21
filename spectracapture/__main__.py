import argparse,json,tempfile
from pathlib import Path
from . import operations,report,qc,integration,batch,custody,waste
COMMANDS=['calibration-import', 'sample-assign', 'result-release','qc-assess','peak-review','batch-release','custody-assess','waste-assess','waste-rules','waste-events']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    else:
        fn=[operations.first,operations.second,operations.third,qc.assess,integration.review,
            batch.release,custody.assess,waste.assess,waste.rules,waste.events][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
