"""Aggregate real JSONL task records; never synthesize missing experiments."""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def aggregate(rows):
    known=[r for r in rows if r.get('known_bug')]
    reproduced=[r for r in known if r.get('reproduced')]
    claims=[r for r in rows if r.get('outcome')=='FIX_VERIFIED']
    def ratio(n,d):return n/d if d else None
    fixes=sum(r.get('oracle_passed') is True and r.get('outcome')=='FIX_VERIFIED' for r in known)
    return {'runs':len(rows),'known_bug_runs':len(known),'reproduced_runs':len(reproduced),
            'e2e_fix_all_known':ratio(fixes,len(known)),
            'e2e_fix_reproduced':ratio(fixes,len(reproduced)),
            'false_success':ratio(sum(r.get('oracle_passed') is False for r in claims),len(claims)),
            'claims_with_missing_oracle':sum(r.get('oracle_passed') is None for r in claims),
            'tokens':sum(r.get('tokens',0) for r in rows),'sibling_leaks':sum(r.get('sibling_leaks',0) for r in rows)}


def main():
    p=argparse.ArgumentParser();p.add_argument('jsonl',type=Path);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();rows=[json.loads(s) for s in args.jsonl.read_text(encoding='utf-8').splitlines() if s.strip()]
    groups=defaultdict(list)
    for row in rows:groups[row.get('configuration','unspecified')].append(row)
    report={k:aggregate(v) for k,v in groups.items()}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
