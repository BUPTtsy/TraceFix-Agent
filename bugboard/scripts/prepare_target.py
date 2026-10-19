"""Prepare the pinned external BugBoard repository without a product submodule."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = 'https://github.com/BUPTtsy/Test-Bug-repo.git'
DEFAULT_REVISION = 'e7a9c04002a2d51a14d37da1f4808c2ee4695c57'


def repository_settings(url=None, revision=None):
    url = url or os.getenv('TRACEFIX_TEST_REPOSITORY_URL') or DEFAULT_URL
    revision = revision or os.getenv('TRACEFIX_TEST_REPOSITORY_REF') or DEFAULT_REVISION
    parsed = urlsplit(url)
    if parsed.username or parsed.password or any(character in url + revision for character in '\r\n\0'):
        raise ValueError('测试仓库地址不能包含凭据或控制字符')
    if url.startswith('-') or revision.startswith('-'):
        raise ValueError('测试仓库地址和版本不能以短横线开头')
    return url, revision


def target_cache(root=ROOT, *, url=None, revision=None):
    url, revision = repository_settings(url, revision)
    identity = hashlib.sha256((url + '\0' + revision).encode('utf-8')).hexdigest()[:16]
    return Path(root) / '.tracefix' / 'repositories' / f'bugboard-{identity}'


def git(root, *arguments):
    environment = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
    environment['GIT_TERMINAL_PROMPT'] = '0'
    result = subprocess.run(['git', '-C', str(root), *arguments], env=environment,
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    if result.returncode:
        raise RuntimeError(f'测试仓库 Git 操作失败：{arguments[0]}（退出码 {result.returncode}），请检查仓库访问权限及版本')
    return result.stdout.strip()


def verify_cases(source, root=ROOT):
    specification = importlib.util.spec_from_file_location('tracefix_target_cases', Path(root) / 'evals/cases.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    for case in module.CASES:
        target = Path(source) / case['file']
        if target.read_text(encoding='utf-8').count(case['before']) != 1:
            raise ValueError(f'用例 {case["id"]} 的变异基线不匹配：{case["file"]}')


def validate_target(source, url, revision):
    if not (source / '.git').is_dir() or Path(git(source, 'rev-parse', '--show-toplevel')).resolve() != source.resolve():
        raise ValueError('测试仓库缓存不是独立 Git 工作区，请选择新的缓存目录')
    if git(source, 'remote', 'get-url', 'origin') != url:
        raise ValueError('测试仓库缓存来源与配置不一致')
    expected = git(source, 'rev-parse', '--verify', revision + '^{commit}')
    if git(source, 'rev-parse', 'HEAD') != expected:
        raise ValueError('测试仓库缓存版本与配置不一致')
    if git(source, 'status', '--porcelain', '--untracked-files=all', '--', '.',
           ':(exclude)node_modules', ':(exclude)dist'):
        raise ValueError('测试仓库缓存存在已修改文件，拒绝覆盖')
    if not all((source / filename).is_file() for filename in ('package.json', 'package-lock.json')):
        raise ValueError('测试仓库缺少 package.json 或 package-lock.json')


def prepare_target(root=ROOT, *, url=None, revision=None, cache=None, check_cases=False):
    url, revision = repository_settings(url, revision)
    destination = Path(cache) if cache is not None else target_cache(root, url=url, revision=revision)
    destination = destination.resolve()
    if destination.exists():
        validate_target(destination, url, revision)
        if check_cases:
            verify_cases(destination, root)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.bugboard-clone-', dir=destination.parent))
    try:
        git(destination.parent, 'clone', '--no-checkout', '--', url, str(staging))
        git(staging, 'checkout', '--detach', revision)
        validate_target(staging, url, revision)
        if check_cases:
            verify_cases(staging, root)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, onexc=remove_readonly)
    return destination


def remove_readonly(operation, path, error):
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    operation(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description='拉取固定版本的独立 BugBoard 测试仓库')
    parser.add_argument('--repo-url')
    parser.add_argument('--revision')
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--verify-cases', action='store_true')
    options = parser.parse_args(argv)
    print(prepare_target(url=options.repo_url, revision=options.revision, cache=options.cache,
                         check_cases=options.verify_cases))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
