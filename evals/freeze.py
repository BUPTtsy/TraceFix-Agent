"""Freeze movable evaluator-private cases and public inputs without running models."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from evals.cases import case_mutations, frozen_cases
from evals.config import EvaluationConfig
from tracefix.runtime.contracts import TestSpec, digest

ROOT = Path(__file__).resolve().parents[1]
PATH_FIELDS = ('source_root', 'profile', 'spec', 'public_protocol', 'skills_root', 'initial_experience')


def _git(source, *arguments):
    return subprocess.run(['git', '-C', str(source), *arguments], check=True,
                          capture_output=True, timeout=180).stdout


def verify_anchors(source, revision, mutations):
    actual = _git(source, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    if actual != revision:
        raise ValueError('源码修订必须为已存在的完整 SHA')
    contents, evidence = {}, []
    for index, mutation in enumerate(mutations):
        path = mutation['file']
        if path not in contents:
            mode = _git(source, 'ls-tree', revision, '--', path).decode().split(' ', 1)[0]
            if mode not in {'100644', '100755'}:
                raise ValueError('注入目标必须是源码内普通文件：' + path)
            contents[path] = _git(source, 'show', revision + ':' + path).decode('utf-8')
        if contents[path].count(mutation['before']) != 1:
            raise ValueError('注入锚点必须恰好出现一次：' + path)
        contents[path] = contents[path].replace(mutation['before'], mutation['after'], 1)
        evidence.append({'index': index, 'file': path, 'before_hash': digest(mutation['before']),
                         'after_hash': digest(mutation['after'])})
    return evidence


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _inventory(root):
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('冻结资产不允许符号链接')
        if path.is_file() and path != root / 'manifest.json':
            files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return files


def _copy_file(source, destination):
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError('冻结输入必须为普通文件：' + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def freeze_suite(configs, output, *, tools_manifest, recovery_manifest, oracle_cases=None,
                 private_assets=None):
    configs = list(configs)
    if not configs or len({config.case_id for config in configs}) != len(configs):
        raise ValueError('冻结配置必须非空且 case_id 不重复')
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('冻结输出目录已存在，拒绝复用：' + str(output))
    tools_hash, recovery_hash = digest(tools_manifest), digest(recovery_manifest)
    metadata = {case['id']: dict(case) for case in oracle_cases or []}
    for config in configs:
        if config.tools_hash != tools_hash or config.recovery_rules_hash != recovery_hash:
            raise ValueError('工具或恢复规则摘要与配置不一致')
        TestSpec.model_validate_json(config.spec.read_bytes())
        verify_anchors(config.source_root, config.source_revision, config.case_mutations())
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / 'private/tools.json', tools_manifest)
    _write_json(output / 'private/recovery.json', recovery_manifest)
    repositories, entries = {}, []
    for index, config in enumerate(configs):
        source = config.source_root.resolve()
        if source not in repositories:
            mirror = output / 'sources' / f'repository-{len(repositories)}.git'
            mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'clone', '--bare', '--no-hardlinks', '--', str(source), str(mirror)],
                           check=True, capture_output=True, timeout=180)
            repositories[source] = mirror
        public, copied = output / 'public' / f'case-{index}', {'source_root': repositories[source]}
        for field in PATH_FIELDS[1:]:
            origin = getattr(config, field)
            destination = public / (field if field == 'skills_root' else field + origin.suffix)
            if field == 'skills_root':
                if origin.is_symlink() or any(path.is_symlink() for path in origin.rglob('*')):
                    raise ValueError('技能资产不允许符号链接')
                shutil.copytree(origin, destination)
            else:
                _copy_file(origin, destination)
            copied[field] = destination
        frozen = replace(config, **copied)
        config_path = output / 'private/configs' / f'case-{index}.json'
        payload = frozen.payload()
        for field in PATH_FIELDS:
            payload[field] = '../../' + getattr(frozen, field).relative_to(output).as_posix()
        _write_json(config_path, payload)
        case = metadata.get(config.case_id, {})
        good_revision = case.get('good_revision', config.source_revision)
        verify_anchors(config.source_root, good_revision, ())
        entries.append({'case_id': config.case_id,
                        'source_repository': repositories[source].relative_to(output).as_posix(),
                        'source_revision': config.source_revision, 'fullrev': config.source_revision,
                        'good_revision': good_revision, 'mutations': list(config.case_mutations()),
                        'oracle_cases': [] if config.known_bug is False else case.get('oracle_cases', [config.case_id]),
                        'oracle_status': case.get('oracle_status', 'existing'),
                        'public_spec_validation': 'schema_only_not_browser_replayed',
                        'admission_status': 'pending_correct_and_defective_oracle_checks',
                        'config': config_path.relative_to(output).as_posix(),
                        'category': config.category or config.family, 'track': config.track,
                        'anchors': verify_anchors(config.source_root, config.source_revision, config.case_mutations()),
                        'fixed_inputs': frozen.fixed_inputs(),
                        'model_hash': digest({'model': config.model, 'vision_model': config.vision_model,
                                              'effort': config.effort, 'thinking': config.thinking})})
    for relative, source in (private_assets or {}).items():
        destination = output / 'private' / relative
        if not destination.resolve().is_relative_to(output / 'private'):
            raise ValueError('私有执行器资产路径越界')
        _copy_file(source, destination)
    _write_json(output / 'private/cases.json', entries)
    manifest = {'format': 'tracefix-frozen-eval/1', 'status': 'prepared_not_evaluated',
                'tools_hash': tools_hash, 'recovery_rules_hash': recovery_hash,
                'cases': entries, 'files': _inventory(output)}
    manifest['manifest_hash'] = digest(manifest)
    _write_json(output / 'manifest.json', manifest)
    verify_frozen_suite(output)
    return manifest


def verify_frozen_suite(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    unsigned = {key: value for key, value in manifest.items() if key != 'manifest_hash'}
    if manifest.get('format') != 'tracefix-frozen-eval/1' or digest(unsigned) != manifest.get('manifest_hash'):
        raise ValueError('冻结 manifest 摘要不匹配')
    if _inventory(root) != manifest['files']:
        raise ValueError('冻结资产发生漂移')
    for entry in manifest['cases']:
        path = (root / entry['config']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('冻结配置路径越界')
        config = EvaluationConfig.load(path)
        if any(not getattr(config, field).resolve().is_relative_to(root) for field in PATH_FIELDS):
            raise ValueError('冻结输入路径越界')
        if config.fixed_inputs() != entry['fixed_inputs']:
            raise ValueError('固定评测输入发生漂移')
        verify_anchors(config.source_root, config.source_revision, config.case_mutations())
    return manifest


def load_frozen_suite(root):
    root = Path(root).resolve()
    manifest = verify_frozen_suite(root)
    return [EvaluationConfig.load(root / entry['config']) for entry in manifest['cases']]


def freeze_cases(source_root, output, *, profile, skills_root, model, tools_manifest,
                 recovery_manifest, tier='smoke', private_assets=None, vision_model='',
                 thinking='disabled', effort='provider-default'):
    cases = frozen_cases(tier)
    specifications = json.loads((ROOT / 'evals/specs/public.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='tracefix-public-spec-') as directory:
        configs = []
        for case in cases:
            spec = Path(directory) / (case['id'] + '.json')
            _write_json(spec, specifications[case['id']])
            configs.append(EvaluationConfig(case_id=case['id'], family=case['family'],
                source_root=Path(source_root).resolve(), source_revision=case['revision'],
                profile=Path(profile).resolve(), spec=spec,
                public_protocol=ROOT / 'evals/fixtures/public-protocol.json',
                skills_root=Path(skills_root).resolve(),
                initial_experience=ROOT / 'evals/fixtures/empty-experience.json', goal=case['goal'],
                model=model, vision_model=vision_model, thinking=thinking, effort=effort,
                seed=0, tools_hash=digest(tools_manifest),
                recovery_rules_hash=digest(recovery_manifest), mutations=case_mutations(case),
                category=case['category'], track=case['track'], known_bug=case['known_bug']))
        return freeze_suite(configs, output, tools_manifest=tools_manifest,
                            recovery_manifest=recovery_manifest, oracle_cases=cases,
                            private_assets=private_assets)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-root', 'profile', 'skills', 'tools-manifest', 'recovery-manifest', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--tier', choices=('smoke', 'standard'), default='smoke')
    args = parser.parse_args(argv)
    manifest = freeze_cases(args.source_root, args.output, profile=args.profile, skills_root=args.skills,
        model=args.model, tools_manifest=json.loads(args.tools_manifest.read_text(encoding='utf-8')),
        recovery_manifest=json.loads(args.recovery_manifest.read_text(encoding='utf-8')), tier=args.tier)
    print(json.dumps({'manifest_hash': manifest['manifest_hash'],
                      'case_ids': [case['case_id'] for case in manifest['cases']]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
