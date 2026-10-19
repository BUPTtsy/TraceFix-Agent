"""Authoritative repository registration and immutable execution configuration."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path, PurePosixPath
import re
from tempfile import NamedTemporaryFile
import threading
import time
from urllib.parse import urlsplit

import yaml

from tracefix.config import Profile, Project
from tracefix.execution.repository import canonical_remote, plain_root, repository_binding, run_git
from tracefix.runtime.contracts import digest
from tracefix.remote import validate_branch, validate_repository


class RepositoryError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

    def as_dict(self):
        return {'code': self.code, 'message': self.message, 'status': self.status}


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class RegistryDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


@contextmanager
def registry_lock(path: Path, timeout: float = 15):
    lock_path = Path(str(path.resolve()) + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _locks_guard:
        thread_lock = _locks.setdefault(str(lock_path).casefold(), threading.RLock())
    with thread_lock:
        with lock_path.open('a+b') as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b'0')
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RepositoryError('REGISTRY_BUSY', '仓库配置写入繁忙', 409) from None
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_yaml(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            yaml.dump(payload, handle, Dumper=RegistryDumper, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def normalize_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value.strip()):
        raise RepositoryError('INVALID_REPO_ID', 'repo_id 必须是 1 至 64 位字母、数字、下划线或短横线')
    return value.strip().casefold()


def config_digest(config: dict) -> str:
    return digest({key: value for key, value in config.items()
                   if key not in {'config_digest', 'frozen_run_id', 'snapshot_digest'}})


def validate_stored_config(project: Project):
    config = project.repository_config
    required = {'version', 'repo_id', 'projectId', 'root', 'owner', 'repo', 'default_ref',
                'commit', 'profile', 'allowed_files', 'allowed_urls', 'credential_ref',
                'enabled', 'config_digest'}
    if not isinstance(config, dict) or not required <= config.keys() or config.get('version') != 1:
        raise RepositoryError('INVALID_REPOSITORY_CONFIG', '仓库配置记录不完整')
    if (config['repo_id'] != project.id or config['projectId'] != project.id
            or config['root'] != str(project.root)
            or canonical_remote(project.repo_id) != canonical_remote(config['owner'] + '/' + config['repo'])
            or config['allowed_files'] != project.allowed_files
            or config['config_digest'] != config_digest(config)):
        raise RepositoryError('CONFIG_DIGEST_MISMATCH', '仓库配置与项目绑定或摘要不一致')
    profile = Profile(**config['profile'])
    if profile.project != project.id or profile.source_commit != config['commit']:
        raise RepositoryError('PROFILE_MISMATCH', '仓库 Profile 与冻结的项目或 commit 不一致')
    if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', config['commit']):
        raise RepositoryError('INVALID_COMMIT', '仓库配置缺少具体 commit')
    if config.get('frozen_run_id') and config.get('snapshot_digest') != digest({
            key: value for key, value in config.items() if key != 'snapshot_digest'}):
        raise RepositoryError('CONFIG_DIGEST_MISMATCH', 'Run 仓库快照摘要不一致')
    return config


class RepositoryRegistry:
    def __init__(self, path: Path, *, authorized_roots=None, data_root: Path | None = None):
        self.path = plain_root(Path(path))
        raw_roots = authorized_roots
        if raw_roots is None:
            encoded = os.getenv('TRACEFIX_REPOSITORY_ROOTS')
            try:
                raw_roots = json.loads(encoded) if encoded else [str(self.path.parent)]
            except (TypeError, json.JSONDecodeError):
                raise RepositoryError('DEPLOYMENT_CONFIG_INVALID', '部署仓库 roots 配置无效', 503) from None
        if not isinstance(raw_roots, (list, tuple)) or not raw_roots:
            raise RepositoryError('DEPLOYMENT_CONFIG_INVALID', '部署必须授权仓库 roots', 503)
        self.authorized_roots = tuple(plain_root(Path(root)) for root in raw_roots)
        self.data_root = plain_root(Path(data_root or os.getenv('TRACEFIX_DATA', '.tracefix')))

    def _read(self):
        if not self.path.exists():
            return {'projects': []}
        try:
            raw = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        except (OSError, yaml.YAMLError):
            raise RepositoryError('REGISTRY_INVALID', '仓库注册表无法读取', 503) from None
        if not isinstance(raw, dict) or not isinstance(raw.get('projects'), list):
            raise RepositoryError('REGISTRY_INVALID', '仓库注册表格式无效', 503)
        return raw

    def _assert_mutable(self, raw):
        if raw.get('frozen_run_id'):
            raise RepositoryError('FROZEN_CONFIG', 'Run 配置快照不可修改', 409)

    def _root(self, value, *, enforce_authorized=True):
        if not isinstance(value, str) or not value:
            raise RepositoryError('INVALID_ROOT', '仓库 root 不能为空')
        candidate = Path(value)
        if '..' in candidate.parts:
            raise RepositoryError('ROOT_NOT_AUTHORIZED', '仓库 root 不允许路径遍历', 403)
        root = plain_root(candidate if candidate.is_absolute() else self.path.parent / candidate)
        if enforce_authorized and not any(root.is_relative_to(allowed) for allowed in self.authorized_roots):
            raise RepositoryError('ROOT_NOT_AUTHORIZED', '仓库 root 不在部署授权范围内', 403)
        return root

    @staticmethod
    def _files(value):
        if not isinstance(value, list) or not value:
            raise RepositoryError('INVALID_ALLOWED_FILES', 'allowed_files 必须是非空相对路径列表')
        for pattern in value:
            if (not isinstance(pattern, str) or not pattern or '\\' in pattern or ':' in pattern
                    or pattern.startswith('/') or '..' in PurePosixPath(pattern).parts
                    or any(part in {'.git', '.env', '.tracefix', 'node_modules'}
                           or part.startswith('.env.') for part in PurePosixPath(pattern).parts)):
                raise RepositoryError('INVALID_ALLOWED_FILES', 'allowed_files 含越界或受保护路径')
        return list(dict.fromkeys(value))

    @staticmethod
    def _urls(value):
        if not isinstance(value, list) or not value:
            raise RepositoryError('INVALID_ALLOWED_URLS', 'allowed_urls 必须是非空 http(s) 来源列表')
        for origin in value:
            if not isinstance(origin, str):
                raise RepositoryError('INVALID_ALLOWED_URLS', 'allowed_urls 必须是 http(s) 来源列表')
            parsed = urlsplit(origin)
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in {'', '/'}):
                raise RepositoryError('INVALID_ALLOWED_URLS', 'allowed_urls 必须是精确 http(s) 来源')
        return list(dict.fromkeys(origin.rstrip('/') for origin in value))

    def _validate_record(self, fields: dict, identifier: str):
        allowed = {'repo_id', 'projectId', 'id', 'root', 'owner', 'repo', 'default_ref', 'profile',
                   'profile_ref', 'allowed_files', 'allowed_urls', 'credential_ref', 'enabled'}
        if set(fields) - allowed:
            raise RepositoryError('INVALID_FIELDS', '仓库注册包含未知字段')
        for alias in ('repo_id', 'projectId', 'id'):
            if alias in fields and normalize_id(fields[alias]) != identifier:
                raise RepositoryError('REPO_ID_IMMUTABLE', 'repo_id 与 projectId 必须一致且不可修改', 409)
        root = self._root(fields.get('root'))
        owner, repository = fields.get('owner'), fields.get('repo')
        if not isinstance(owner, str) or not isinstance(repository, str):
            raise RepositoryError('INVALID_REPOSITORY', '必须提供 GitHub owner 和 repo')
        github = validate_repository(owner + '/' + repository).casefold()
        if owner in {'.', '..'} or repository in {'.', '..'}:
            raise RepositoryError('INVALID_REPOSITORY', 'GitHub owner 和 repo 无效')
        owner, repository = github.split('/')
        ref = fields.get('default_ref')
        if not isinstance(ref, str):
            raise RepositoryError('INVALID_REF', '必须提供 default_ref')
        validate_branch(ref)
        try:
            identity = repository_binding(root, github, ref, require_clean=False)
        except PermissionError:
            raise RepositoryError('REPOSITORY_BINDING_MISMATCH', '仓库 origin、root 或 Git 身份与注册绑定不一致', 403) from None
        except (RuntimeError, OSError):
            raise RepositoryError('INVALID_REF', 'default_ref 无法在预置 checkout 解析为 commit') from None
        commit = identity['base']
        allowed_files = self._files(fields.get('allowed_files'))
        allowed_urls = self._urls(fields.get('allowed_urls'))
        profile_ref = fields.get('profile_ref')
        payload = fields.get('profile')
        if profile_ref is not None and (not isinstance(profile_ref, str) or not profile_ref):
            raise RepositoryError('INVALID_PROFILE_REF', 'Profile 引用必须是相对文件路径')
        if profile_ref:
            reference = plain_root(self.path.parent / profile_ref)
            if not reference.is_relative_to(self.path.parent) or not reference.is_file():
                raise RepositoryError('INVALID_PROFILE_REF', 'Profile 引用必须是注册目录内的普通文件', 403)
            if payload is not None:
                raise RepositoryError('INVALID_PROFILE', 'Profile 只能使用一个权威输入')
            try:
                payload = yaml.safe_load(reference.read_text(encoding='utf-8'))
            except (OSError, yaml.YAMLError):
                raise RepositoryError('INVALID_PROFILE', 'Profile 无法读取') from None
            profile_ref = str(reference)
        if not isinstance(payload, dict):
            raise RepositoryError('INVALID_PROFILE', '必须提供有效 Profile 或 profile_ref')
        payload = dict(payload)
        if normalize_id(payload.get('project', identifier)) != identifier:
            raise RepositoryError('PROFILE_MISMATCH', 'Profile 不属于注册项目')
        payload.update(project=identifier, source_commit=commit)
        try:
            profile = Profile(**payload)
        except ValueError:
            raise RepositoryError('INVALID_PROFILE', 'Profile 缺少有效的运行命令或配置') from None
        if (not set(origin.rstrip('/') for origin in profile.allowed_origins) <= set(allowed_urls)
                or not any(profile.url == origin or profile.url.startswith(origin + '/') for origin in allowed_urls)):
            raise RepositoryError('PROFILE_URL_NOT_AUTHORIZED', 'Profile URL 或来源超出 allowed_urls', 403)
        credential_ref = fields.get('credential_ref')
        if credential_ref is not None and (not isinstance(credential_ref, str)
                or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', credential_ref)):
            raise RepositoryError('INVALID_CREDENTIAL_REF', 'credential_ref 必须是部署凭据引用')
        enabled = fields.get('enabled', True)
        if not isinstance(enabled, bool):
            raise RepositoryError('INVALID_ENABLED', 'enabled 必须是布尔值')
        config = {'version': 1, 'repo_id': identifier, 'projectId': identifier, 'root': str(root),
                  'owner': owner, 'repo': repository, 'default_ref': ref, 'commit': commit,
                  'profile_ref': profile_ref, 'profile': profile.model_dump(mode='json'),
                  'allowed_files': allowed_files, 'allowed_urls': allowed_urls,
                  'credential_ref': credential_ref, 'enabled': enabled}
        config['config_digest'] = config_digest(config)
        return config

    @staticmethod
    def _unique(entries, identifier, root, *, replacing=None):
        for item in entries:
            if item.get('id') == replacing:
                continue
            if str(item.get('id', '')).casefold() == identifier.casefold():
                raise RepositoryError('REPO_ID_CONFLICT', 'repo_id 忽略大小写后已存在', 409)
            existing_root = Path(item['root'])
            if str(existing_root.resolve()).casefold() == str(root).casefold():
                raise RepositoryError('ROOT_CONFLICT', '仓库真实 root 已注册', 409)

    def _check_unique(self, entries, identifier, root, *, replacing=None):
        expanded = [{**item, 'root': str(plain_root(self.path.parent / item['root']))} for item in entries]
        self._unique(expanded, identifier, root, replacing=replacing)

    def register(self, fields):
        if not isinstance(fields, dict):
            raise RepositoryError('INVALID_FIELDS', '仓库注册字段必须是对象')
        identifier = normalize_id(fields.get('repo_id', fields.get('projectId', fields.get('id'))))
        with registry_lock(self.path):
            raw = self._read()
            self._assert_mutable(raw)
            config = self._validate_record(fields, identifier)
            self._check_unique(raw['projects'], identifier, Path(config['root']))
            item = Project(id=identifier, repo_id=config['owner'] + '/' + config['repo'],
                           root=Path(config['root']), allowed_files=config['allowed_files'],
                           repository_config=config).model_dump(mode='json')
            raw['projects'].append(item)
            atomic_yaml(self.path, raw)
        self.prepare_codegraph(identifier)
        return deepcopy(config)

    def prepare_codegraph(self, identifier):
        from tracefix.config import load_projects
        from tracefix.rules.codegraph import prepare_project_index
        return prepare_project_index(load_projects(self.path), identifier, self.data_root)

    def list(self):
        from tracefix.config import load_projects
        if not self.path.exists():
            return []
        return [deepcopy(project.repository_config) for project in load_projects(self.path).values()
                if project.repository_config is not None]

    def get(self, identifier):
        identifier = normalize_id(identifier)
        for config in self.list():
            if config['repo_id'] == identifier:
                return config
        raise RepositoryError('REPOSITORY_NOT_FOUND', '未注册的仓库', 404)

    def update(self, identifier, patch):
        identifier = normalize_id(identifier)
        if not isinstance(patch, dict):
            raise RepositoryError('INVALID_FIELDS', '仓库更新字段必须是对象')
        with registry_lock(self.path):
            raw = self._read()
            self._assert_mutable(raw)
            item = next((item for item in raw['projects'] if item['id'] == identifier), None)
            if not item or not item.get('repository_config'):
                raise RepositoryError('REPOSITORY_NOT_FOUND', '未注册的仓库', 404)
            old = item['repository_config']
            validate_stored_config(Project(**{**item, 'root': plain_root(self.path.parent / item['root'])}))
            fields = {key: deepcopy(value) for key, value in old.items()
                      if key not in {'version', 'commit', 'config_digest', 'frozen_run_id'}}
            fields.update(patch)
            if patch.get('profile_ref'):
                fields.pop('profile', None)
            else:
                fields['profile_ref'] = None
            config = self._validate_record(fields, identifier)
            self._check_unique(raw['projects'], identifier, Path(config['root']), replacing=identifier)
            item.update(repo_id=config['owner'] + '/' + config['repo'], root=config['root'],
                        allowed_files=config['allowed_files'], repository_config=config)
            atomic_yaml(self.path, raw)
        self.prepare_codegraph(identifier)
        return deepcopy(config)

    def freeze(self, identifier, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run_id):
            raise RepositoryError('INVALID_RUN_ID', 'freeze 必须绑定安全且非空的 run_id')
        target = self.data_root / 'frozen-repositories' / run_id
        frozen_registry = target / 'projects.yaml'
        with registry_lock(self.path):
            config = self.get(identifier)
            if not config['enabled']:
                raise RepositoryError('REPOSITORY_DISABLED', '仓库已禁用，不能启动新 Run', 409)
            if config.get('frozen_run_id'):
                if config['frozen_run_id'] != run_id:
                    raise RepositoryError('RUN_ID_CONFLICT', '冻结配置只可用于绑定的 Run', 409)
                return self._frozen_result(config, self.path)
            if frozen_registry.exists():
                from tracefix.config import load_projects
                frozen = load_projects(frozen_registry)
                project = frozen.get(config['repo_id'])
                if len(frozen) != 1 or not project or project.repository_config.get('frozen_run_id') != run_id:
                    raise RepositoryError('RUN_ID_CONFLICT', 'run_id 已绑定其他仓库', 409)
                return self._frozen_result(project.repository_config, frozen_registry)
            fields = {key: deepcopy(value) for key, value in config.items()
                      if key not in {'version', 'commit', 'config_digest', 'frozen_run_id'}}
            fields['profile_ref'] = None
            frozen_config = self._validate_record(fields, config['repo_id'])
            frozen_config['frozen_run_id'] = run_id
            frozen_config['snapshot_digest'] = digest(frozen_config)
            item = Project(id=config['repo_id'], repo_id=config['owner'] + '/' + config['repo'],
                           root=Path(frozen_config['root']), allowed_files=frozen_config['allowed_files'],
                           repository_config=frozen_config).model_dump(mode='json')
            with registry_lock(frozen_registry):
                if frozen_registry.exists():
                    raise RepositoryError('RUN_ID_CONFLICT', 'run_id 配置快照已被其他启动占用', 409)
                atomic_yaml(target / (config['repo_id'] + '.yaml'), frozen_config['profile'])
                atomic_yaml(frozen_registry, {'frozen_run_id': run_id, 'projects': [item]})
        return self._frozen_result(frozen_config, frozen_registry)

    @staticmethod
    def _frozen_result(config, registry):
        profile = registry.parent / (config['repo_id'] + '.yaml')
        try:
            profile = plain_root(profile)
            saved_profile = Profile(**yaml.safe_load(profile.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            raise RepositoryError('FROZEN_PROFILE_MISMATCH', '冻结 Profile 文件无效或不可读取', 409) from None
        if saved_profile.model_dump(mode='json') != config['profile']:
            raise RepositoryError('FROZEN_PROFILE_MISMATCH', '冻结 Profile 文件与仓库快照不一致', 409)
        return {'repo_id': config['repo_id'], 'projectId': config['repo_id'], 'commit': config['commit'],
                'config_digest': config['config_digest'], 'repository_config': deepcopy(config),
                'projects_path': str(registry), 'profile_path': str(profile),
                'snapshot': deepcopy(config), 'registry': str(registry), 'profile': str(profile)}

    def create_legacy(self, identifier, root, repo_id, allowed_files=None):
        identifier = normalize_id(identifier)
        with registry_lock(self.path):
            raw = self._read()
            self._assert_mutable(raw)
            destination = self._root(root, enforce_authorized=False)
            self._check_unique(raw['projects'], identifier, destination)
            project = Project(id=identifier, repo_id=repo_id, root=destination,
                              allowed_files=allowed_files or ['src/**', 'server/**'])
            destination.mkdir(parents=True, exist_ok=True)
            raw['projects'].append(project.model_dump(mode='json'))
            atomic_yaml(self.path, raw)
        self.prepare_codegraph(identifier)
        return project

    def configure_legacy(self, identifier, fields, profile_path=None):
        identifier = normalize_id(identifier)
        payload = dict(fields)
        payload['project'] = identifier
        profile = Profile(**payload)
        profile_path = plain_root(profile_path or self.path.parent / (identifier + '.yaml'))
        if not profile_path.is_relative_to(self.path.parent):
            raise RepositoryError('INVALID_PROFILE_REF', 'Profile 必须写入注册目录', 403)
        with registry_lock(self.path):
            raw = self._read()
            self._assert_mutable(raw)
            item = next((item for item in raw['projects'] if item['id'] == identifier), None)
            if not item:
                raise RepositoryError('REPOSITORY_NOT_FOUND', '未注册的项目', 404)
            if item.get('repository_config'):
                raise RepositoryError('MANAGED_REPOSITORY', '已注册仓库必须通过 repository.update 修改 Profile', 409)
            atomic_yaml(profile_path, profile.model_dump(mode='json'))
        return profile
