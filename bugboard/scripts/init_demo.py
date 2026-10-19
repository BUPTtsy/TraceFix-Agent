"""Build a clean single-commit Agent repository; injection/oracles stay outside."""
import importlib.util
import shutil
from pathlib import Path

from tracefix.execution.workspace import git
from tracefix.messages import ChineseArgumentParser
from prepare_target import prepare_target

ROOT=Path(__file__).resolve().parents[2]
parser=ChineseArgumentParser(description='拉取独立测试仓库并初始化隔离的演示工作区')
parser.add_argument('--case',default='B01',help='演示用例编号，默认为 B01；clean 表示不注入故障')
parser.add_argument('--destination',type=Path,default=ROOT/'.tracefix/demo-repo',help='演示仓库的目标目录')
parser.add_argument('--repo-url', help='独立测试仓库 URL')
parser.add_argument('--revision', help='测试仓库版本，默认使用固定 commit')
parser.add_argument('--cache', type=Path, help='复用的干净测试仓库缓存')
args=parser.parse_args()
if args.destination.exists():
    print('演示仓库已存在，已保留。请为其他用例选择新的 --destination。')
    raise SystemExit(0)
spec=importlib.util.spec_from_file_location('cases',ROOT/'evals/cases.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
case=None
if args.case!='clean':
    case=next((c for c in module.CASES if c['id']==args.case),None)
    if not case: raise ValueError(f'未知用例：{args.case}')
source=prepare_target(ROOT, url=args.repo_url, revision=args.revision, cache=args.cache, check_cases=True)
shutil.copytree(source,args.destination,ignore=shutil.ignore_patterns('node_modules','dist','.git','Dockerfile'))
if args.case!='clean':
    case=next((c for c in module.CASES if c['id']==args.case),None)
    if not case: raise ValueError(f'未知用例：{args.case}')
    file=args.destination/case['file']
    old=file.read_text(encoding='utf-8')
    if old.count(case['before'])!=1: raise ValueError(f'用例 {args.case} 的变异基线不匹配：{case["file"]}')
    file.write_text(old.replace(case['before'],case['after'],1),encoding='utf-8',newline='\n')
git(args.destination,'init','-q');git(args.destination,'add','.')
git(args.destination,'-c','user.name=TraceFix Demo','-c','user.email=demo@localhost','commit','-qm','初始化独立测试故障基线')
print('已创建隔离的演示源码：',args.destination)
