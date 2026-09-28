"""Tool schemas exposed to supervisors."""

from tracefix.model.gateway import ToolSpec


def supervisor_tools():
    phases = ['PREPARE', 'EXPLORE', 'REPRODUCE', 'DIAGNOSE', 'PATCH', 'VERIFY', 'REVIEW', 'FINALIZE']
    delegate = {
        'type': 'object', 'properties': {
            'task_id': {'type': 'string', 'minLength': 1, 'maxLength': 120},
            'role': {'type': 'string', 'minLength': 1, 'maxLength': 160},
            'role_label': {'type': 'string', 'minLength': 1, 'maxLength': 160},
            'objective': {'type': 'string', 'minLength': 20, 'maxLength': 8000},
            'prompt': {'type': 'string', 'minLength': 120, 'maxLength': 40000},
            'phase': {'type': 'string', 'enum': phases},
            'allowed_tools': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 64},
            'allowed_files': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2000},
            'allowed_artifacts': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2000},
            'writable_files': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2000},
            'depends_on': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2000},
            'expected_output': {'type': 'string', 'minLength': 20, 'maxLength': 4000},
            'completion_criteria': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 30},
            'constraints': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 30},
            'retry_limit': {'type': 'integer', 'minimum': 0, 'maximum': 3},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400},
        },
        'required': ['task_id', 'role', 'role_label', 'objective', 'prompt', 'phase',
                     'allowed_tools', 'expected_output', 'completion_criteria', 'constraints'],
        'additionalProperties': False,
    }
    join = {
        'type': 'object', 'properties': {
            'task_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 2000},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400},
        }, 'required': ['task_ids'], 'additionalProperties': False,
    }
    return [ToolSpec('agent.delegate', '派发一个详细、可验收且不可递归的 Worker 任务。', delegate, True),
            ToolSpec('agent.join', '等待已经派发的 Worker 并返回结构化结果。', join, False)]
