"""原生浏览器工具定义、参数校验及受控动作执行。"""
from __future__ import annotations

import copy

import jsonschema

from tracefix.model.contracts import ModelOutputError
from tracefix.runtime.contracts import BrowserAction, Decision, digest
from tracefix.storage.artifacts import sanitize
from tracefix.tools.core import model_tool_name


def native_browser_tools(schema):
    if schema not in {BrowserAction, Decision}:
        return []
    action_schema = BrowserAction.model_json_schema()
    tools = []
    for name, fields in {
        'browser_navigate': ['value'],
        'browser_click': ['observation_id', 'element_ref', 'locator'],
        'browser_type': ['observation_id', 'element_ref', 'locator', 'value'],
        'browser_select': ['observation_id', 'element_ref', 'locator', 'value'],
        'browser_press': ['observation_id', 'value'],
        'browser_snapshot': [],
    }.items():
        optional_fields = ['page_generation', 'preconditions', 'postconditions', 'wait']
        properties = {field: copy.deepcopy(action_schema['properties'][field])
                      for field in fields + optional_fields}
        for field in fields:
            property_schema = properties[field]
            property_schema.pop('default', None)
            if 'anyOf' in property_schema:
                property_schema.update(property_schema.pop('anyOf')[0])
        tools.append({'type': 'function', 'function': {
            'name': model_tool_name(name),
            'description': 'Execute through TraceFix policy and MCP. Returns a fresh observation and screenshot evidence reference. value is the URL, text, selected value, or key for the named action.',
            'parameters': {'type': 'object', 'properties': properties,
                'required': fields, 'additionalProperties': False, '$defs': action_schema.get('$defs', {})},
        }})
    return tools


def browser_action(name, arguments):
    definitions = {tool['function']['name']: tool['function']['parameters']
                   for tool in native_browser_tools(BrowserAction)}
    name = model_tool_name(name) if isinstance(name, str) else name
    if not isinstance(name, str) or name not in definitions:
        raise ValueError('未知或未授权的浏览器工具：' + str(name))
    try:
        jsonschema.validate(arguments, definitions[name])
    except jsonschema.ValidationError as error:
        raise ValueError('工具参数校验失败：' + error.message) from error
    kind = {'BrowserNavigate': 'navigate', 'BrowserClick': 'click',
            'BrowserType': 'type', 'BrowserSelect': 'select', 'BrowserPress': 'press',
            'BrowserSnapshot': 'observe'}[name]
    return BrowserAction(kind=kind, **arguments)


async def execute_browser_tool(engine, state, context, name, arguments, call_id, logical_call):
    from tracefix.runtime.engine import ActionBusinessFailure, stable_snapshot

    if context.get('worker_readonly_investigation'):
        raise PermissionError('只读调查 Worker 未获浏览器动作权限')
    engine.sync_guidance(state)
    engine.scopes.assert_current(engine.context)
    action = browser_action(name, arguments)
    observation = engine.get(state, state.observation_ref) if state.observation_ref else None
    try:
        engine.browser.policy.browser(state, action, engine.spec(state), observation)
    except (ValueError, PermissionError, ModelOutputError) as error:
        if isinstance(error, ModelOutputError) and (error.status != 'FAILED'
                or error.details.get('requires_manual_review')):
            raise
        engine.event(state, 'tool.rejected', {'tool_call_id': call_id,
            'action': action.model_dump(), 'error': sanitize(str(error))})
        return {'isError': True, 'error': {'type': type(error).__name__,
            'message': sanitize(str(error)), 'executed': False},
            'observation_ref': state.observation_ref, 'observation': observation}, context
    business_failure = None
    try:
        reference = await engine.act(state, action, tool_call_id=call_id)
    except ActionBusinessFailure as error:
        business_failure = error
        reference = error.observation_ref
    canonical = action.model_copy(update={'observation_id': None, 'element_ref': None,
                                          'page_generation': None})
    plan = engine.get(state, state.replay_plan_ref) if state.replay_plan_ref else []
    plan.append(canonical.model_dump())
    plan_reference = engine.put(state, plan, name='操作重放计划')
    fingerprint = digest([canonical.model_dump(),
                          stable_snapshot(observation['snapshot']) if observation else ''])
    updated = engine.changed(state, observation_ref=reference, replay_plan_ref=plan_reference,
        step=state.step+1, action_fingerprints=(state.action_fingerprints+[fingerprint])[-12:])
    for field in type(state).model_fields:
        setattr(state, field, getattr(updated, field))
    observation = engine.get(state, reference)
    if engine.rule_resolver:
        context = engine.inject_rules(state, {**context, 'observation': observation})
    context = engine.guidance_context(state, {**context, 'observation': observation}, logical_call)
    if business_failure is not None:
        return {'isError': True, 'executed': True,
            'error': {'type': type(business_failure).__name__,
                'message': sanitize(str(business_failure)), 'executed': True,
                'category': 'business_assertion', 'check': business_failure.check},
            'business_outcome': {'status': 'failed', 'passed': False,
                'check': business_failure.check, 'assertions': business_failure.assertions},
            'observation_ref': reference, 'observation': observation}, context
    return {'executed': True, 'observation_ref': reference, 'observation': observation}, context
