import argparse,json,tempfile
from pathlib import Path
from . import operations,report,qc,integration,batch,custody,exchange
COMMANDS=['calibration-import', 'sample-assign', 'result-release','qc-assess','peak-review','batch-release','custody-assess','review-resolve']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    arg=sub.add_parser('exchange-export');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True);arg.add_argument('--since',type=int,default=0)
    arg=sub.add_parser('exchange-import');arg.add_argument('--db',required=True);arg.add_argument('package')
    arg=sub.add_parser('exchange-status');arg.add_argument('--db',required=True)
    arg=sub.add_parser('evidence-graph');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='exchange-export':
        package=exchange.build_package(x.db,x.since)
        Path(x.out).parent.mkdir(parents=True,exist_ok=True)
        Path(x.out).write_text(json.dumps(package,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'package_id':package['package_id'],'commits':len(package['commits']),
                          'head':package['head']},ensure_ascii=False))
    elif x.command=='exchange-import':print(json.dumps(exchange.receive(x.db,x.package),ensure_ascii=False))
    elif x.command=='exchange-status':print(json.dumps(exchange.sync_status(x.db),ensure_ascii=False))
    elif x.command=='evidence-graph':
        graph=exchange.canonical_graph(x.db)
        Path(x.out).parent.mkdir(parents=True,exist_ok=True)
        Path(x.out).write_text(json.dumps(graph,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'sources':len(graph['sources']),'evidence':len(graph['evidence'])},ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,qc.assess,integration.review,
            batch.release,custody.assess,integration.resolve][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
