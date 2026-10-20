"""Resolve and freeze the repair strategy at a new Run boundary."""
import os
from collections.abc import Mapping


REPAIR_MODES = ('hybrid', 'pipeline')
DEFAULT_REPAIR_MODE = 'hybrid'
HYBRID_AVAILABLE = True
FINDING_REPAIR_AVAILABLE = True
REPAIR_MODE_FIELDS = ('repair_mode', 'repair_mode_requested', 'repair_mode_reason')


def resolve_repair_mode(*, run_mode, requested, project_config, model, repair_source):
    """Return requested/actual modes without re-resolving an existing Run."""
    project_config = project_config or {}
    if not isinstance(project_config, Mapping):
        raise ValueError('项目修复模式配置必须为对象')
    configured = next((value for value in (requested, project_config.get('repair_mode'),
        os.getenv('TRACEFIX_REPAIR_MODE'), DEFAULT_REPAIR_MODE) if value is not None), None)
    if not isinstance(configured, str) or configured not in REPAIR_MODES:
        raise ValueError('修复模式必须为 hybrid 或 pipeline')
    reason = None
    if configured == 'hybrid':
        if run_mode == 'test':
            reason = 'test_mode'
        elif not HYBRID_AVAILABLE:
            reason = 'not_implemented'
        elif not getattr(model, 'supports_tool_executor', False):
            reason = 'model_without_tools'
        elif repair_source is not None and not FINDING_REPAIR_AVAILABLE:
            reason = 'finding_repair_pending'
    return ('pipeline' if reason else configured, configured, reason)


def resolve_repair_mode_fields(**options):
    return dict(zip(REPAIR_MODE_FIELDS, resolve_repair_mode(**options)))


def repair_mode_fields(state):
    return {name: getattr(state, name) for name in REPAIR_MODE_FIELDS}
