import pytest
from pydantic import ValidationError

from tracefix.repository_registry import RepositoryError, config_digest, validate_repair_config
from tracefix.service.contracts import ExecutionRequest


@pytest.mark.parametrize('public,internal', [('hybrid', 'hybrid'), ('pipeline', 'pipeline'), (None, None)])
def test_execution_repair_mode_aliases(public, internal):
    assert ExecutionRequest(repairMode=public).repair_mode == internal
    assert ExecutionRequest(repair_mode=internal).repair_mode == public
    assert ExecutionRequest(repairMode=public, repair_mode=internal).repair_mode == public
    assert ExecutionRequest().repair_mode is None


@pytest.mark.parametrize('fields', [
    {'repairMode': 'loop'}, {'repair_mode': 'HYBRID'}, {'repairMode': 1},
    {'repairMode': 'hybrid', 'repair_mode': 'pipeline'},
    {'repairMode': None, 'repair_mode': 'pipeline'},
])
def test_execution_rejects_invalid_modes_and_conflicting_aliases(fields):
    with pytest.raises(ValidationError):
        ExecutionRequest(**fields)


@pytest.mark.parametrize('settings,code', [
    ({'repair_mode': 'loop'}, 'INVALID_REPAIR_MODE'),
    ({'repair_mode': None}, 'INVALID_REPAIR_MODE'),
    ({'protected_paths': '../secrets'}, 'INVALID_PROTECTED_PATHS'),
    ({'protected_paths': ['../secrets']}, 'INVALID_PROTECTED_PATHS'),
    ({'protected_paths': ['/etc/config']}, 'INVALID_PROTECTED_PATHS'),
    ({'protected_paths': ['C:/config']}, 'INVALID_PROTECTED_PATHS'),
    ({'protected_paths': ['src\\permissions.ts']}, 'INVALID_PROTECTED_PATHS'),
    ({'protected_paths': [None]}, 'INVALID_PROTECTED_PATHS'),
])
def test_repository_repair_settings_validate(settings, code):
    with pytest.raises(RepositoryError) as caught:
        validate_repair_config(settings)
    assert caught.value.code == code


def test_optional_repository_settings_participate_in_digest():
    legacy = {'repo_id': 'app', 'profile': {'commands': {'unit': ['node', '--test']}}}
    baseline = config_digest(legacy)
    validate_repair_config(legacy)
    assert config_digest(legacy) == baseline
    assert 'repair_mode' not in legacy and 'protected_paths' not in legacy
    assert config_digest({**legacy, 'repair_mode': 'pipeline'}) != baseline
    assert config_digest({**legacy, 'protected_paths': ['src/permissions.ts']}) != baseline
