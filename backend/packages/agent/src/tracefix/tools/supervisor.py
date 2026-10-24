"""向主管 Agent 暴露结构化委派与等待工具，不暴露任意命令入口。"""

from tracefix.tools.core import ToolSpec


def supervisor_tools():
    # schema 描述委派范围和验收条件，实际角色权限仍须由运行时二次校验。
    phases = ['PREPARE', 'EXPLORE', 'REPRODUCE', 'DIAGNOSE', 'PATCH', 'VERIFY', 'REVIEW', 'FINALIZE']
    delegate = {
        'type': 'object', 'properties': {
            'task_id': {'type': 'string', 'minLength': 1, 'maxLength': 120},
            'role': {'type': 'string', 'minLength': 1, 'maxLength': 160},
            'role_label': {'type': 'string', 'minLength': 1, 'maxLength': 160},
            'objective': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
            'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 40000},
            'phase': {'type': 'string', 'enum': phases},
            'allowed_tools': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 64},
            'allowed_files': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2000},
            'allowed_artifacts': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2000},
            'shell_mode': {'type': 'string', 'enum': ['disabled', 'readonly']},
            'depends_on': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2000},
            'expected_output': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
            'completion_criteria': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 30},
            'constraints': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 30},
            'retry_limit': {'type': 'integer', 'minimum': 0, 'maximum': 3},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400},
        },
        'required': ['task_id', 'role', 'objective', 'prompt', 'phase',
                     'allowed_tools', 'expected_output', 'completion_criteria', 'constraints'],
        'additionalProperties': False,
    }
    join = {
        'type': 'object', 'properties': {
            'task_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 2000},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400},
        }, 'required': ['task_ids'], 'additionalProperties': False,
    }
    return [ToolSpec('agent.delegate', '派发一个详细、可验收且不可递归的只读 Worker 任务；Worker 不能修改源码。',
                     delegate, True),
            ToolSpec('agent.join', '等待已经派发的 Worker 并返回结构化结果。', join, False)]
