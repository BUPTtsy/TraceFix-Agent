"""Build a clean single-commit Agent repository; injection/oracles stay outside."""
import importlib.util
import shutil
from pathlib import Path

from tracefix.execution.workspace import git
from tracefix.messages import ChineseArgumentParser

ROOT=Path(__file__).resolve().parents[2]
parser=ChineseArgumentParser(description='从独立模板初始化隔离的演示仓库')
parser.add_argument('--case',default='B01',help='演示用例编号，默认为 B01；clean 表示不注入故障')
parser.add_argument('--destination',type=Path,default=ROOT/'.tracefix/demo-repo',help='演示仓库的目标目录')
args=parser.parse_args()
if args.destination.exists():
    print('演示仓库已存在，已保留。请为其他用例选择新的 --destination。')
    raise SystemExit(0)
source=ROOT/'bugboard/target'
if not source.is_dir():
    raise FileNotFoundError(
        f'找不到独立目标模板：{source}。'
        '请创建 bugboard/target，或先配置远程项目再初始化演示。'
    )
shutil.copytree(source,args.destination,ignore=shutil.ignore_patterns('node_modules','dist','.git','Dockerfile'))
spec=importlib.util.spec_from_file_location('cases',ROOT/'evals/cases.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
if args.case!='clean':
    case=next((c for c in module.CASES if c['id']==args.case),None)
    if not case: raise ValueError(f'未知用例：{args.case}')
    file=args.destination/case['file']
    old=file.read_text(encoding='utf-8')
    if old.count(case['before'])!=1: raise ValueError(f'用例 {args.case} 的变异基线不匹配：{case["file"]}')
    file.write_text(old.replace(case['before'],case['after'],1),encoding='utf-8',newline='\n')
git(args.destination,'init','-q');git(args.destination,'add','.')
git(args.destination,'-c','user.name=TraceFix Demo','-c','user.email=demo@localhost','commit','-qm','Application baseline')
print('已创建隔离的演示源码：',args.destination)
