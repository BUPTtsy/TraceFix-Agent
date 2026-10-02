"""Validate authorized pre-action examples; reject future leakage and bad refs."""
import json
from pathlib import Path

from tracefix.execution.browser import resolve_locator
from tracefix.messages import ChineseArgumentParser
from tracefix.runtime.contracts import BrowserAction


def validate(record, root):
    if record.get('split') not in {'dev','validation','test'}:
        raise ValueError('必须指定有效的数据划分 split：dev、validation 或 test')
    if not record.get('training_authorized') or record.get('privacy')!='public_or_shared':
        raise PermissionError('全局适配器不得使用私有数据或未经授权的数据')
    if record.get('screenshot_redacted') is not True:
        raise ValueError('缺少截图脱敏状态，或截图尚未完成脱敏')
    if record['observation_seq']>=record['action_seq']:
        raise ValueError('观测序号必须早于动作序号，禁止泄露未来观测')
    forbidden={'final_patch','golden_patch','root_cause','future_observation','final_report'}
    if forbidden & record.keys():
        raise ValueError('样本包含动作执行后的数据，存在数据泄露')
    action=BrowserAction(**record['action'])
    if action.locator:
        if action.observation_id!=record['observation']['id']:
            raise ValueError('动作引用的观测已过期或与当前观测不一致')
        if action.element_ref!=resolve_locator(record['observation']['snapshot'],action.locator):
            raise ValueError('动作引用的元素已过期或与定位结果不一致')
    image=(root/record['image']).resolve()
    if not image.is_relative_to(root.resolve()) or not image.is_file():
        raise ValueError('图像位于数据集目录之外，或图像文件不存在')
    return record


def load(path, split='dev'):
    families={};rows=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():continue
        r=validate(json.loads(line),path.parent)
        if r['family'] in families and families[r['family']]!=r['split']:
            raise ValueError('同一根因或模板族出现在不同数据划分中，存在数据泄露')
        families[r['family']]=r['split']
        if r['split']==split:
            r['image']=str((path.parent/r['image']).resolve());rows.append(r)
    if not rows:raise ValueError(f'数据划分 {split} 中没有有效样本')
    return rows


if __name__=='__main__':
    p=ChineseArgumentParser(description='校验已授权的训练数据，检查数据泄露与引用有效性')
    p.add_argument('dataset',type=Path,help='JSONL 数据集路径');p.add_argument('--split',default='dev',help='需要校验的数据划分，默认为 dev')
    a=p.parse_args();print(json.dumps({'valid_samples':len(load(a.dataset,a.split)),'message':'数据集校验通过'},ensure_ascii=False))
