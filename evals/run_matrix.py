"""在已有评测固定输入上交错运行 pipeline/hybrid，输出成对模式门禁。"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from evals.config import AblationGroup, EvaluationConfig
from evals.mode_gate import evaluate_mode_gate
from evals.runner import FourCellRunner


def run_matrix(configs, output, *, arms=('pipeline', 'hybrid'), repeat=3, runner_factory=FourCellRunner):
    if type(repeat) is not int or repeat < 1:
        raise ValueError('repeat 必须为正整数')
    if not arms or len(set(arms)) != len(arms) or set(arms) - {'pipeline', 'hybrid'}:
        raise ValueError('本入口 arms 支持 pipeline、hybrid，且不得重复')
    configs = list(configs)
    if not configs or len({config.case_id for config in configs}) != len(configs):
        raise ValueError('配置必须非空且 case_id 不重复')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    with (output / 'rows.jsonl').open('w', encoding='utf-8') as stream:
        for config in configs:
            for seed in range(repeat):
                runner = runner_factory(dataclasses.replace(config, seed=seed), output / f'seed-{seed}')
                order = arms[seed % len(arms):] + arms[:seed % len(arms)]
                for arm in order:
                    row = runner.run_group(AblationGroup('multi', False, arm))
                    rows.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                    stream.flush()
    report = evaluate_mode_gate(rows)
    (output / 'mode-gate.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, action='append', required=True,
                        help='既有 EvaluationConfig JSON，可重复指定；不读取设计目录的评测子目录')
    parser.add_argument('--arms', default='pipeline,hybrid')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run_matrix([EvaluationConfig.load(path) for path in args.config], args.output,
                        arms=tuple(args.arms.split(',')), repeat=args.repeat)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
