import pytest
from pydantic import BaseModel, Field

from tracefix.runtime.contracts import Contract, Phase, digest
from tracefix.tools.effects import make_operation_executor
from tracefix.runtime.smoke import make_engine
from tracefix.tools.core import (ToolDefinition, ToolOperationUnknown, ToolOutputError,
                                    ToolPipeline, ToolProtocolError, ToolRegistry,
                                    ToolResult, ToolSpec, build_tool)


class Input(Contract):
    value: str = Field(min_length=1)


class Output(Contract):
    value: str


async def test_build_tool_keeps_handler_and_spec_together():
    seen = []

    async def handler(arguments, call_id):
        seen.append((arguments, call_id))
        return Output(value=arguments.value)

    definition: ToolDefinition[Input, Output] = build_tool('typed.read', '读取值', Input, handler,
        output_model=Output, phases=frozenset({Phase.DIAGNOSE}),
        parallel_safe=True, search_hint='读取结构化值', aliases=('read_value',))
    assert definition.spec.output_model is Output
    assert definition.spec.search_hint == '读取结构化值'
    assert definition.spec.aliases == ('read_value',)
    registry = ToolRegistry([definition.spec])
    assert registry.get('read_value') is definition.spec
    pipeline = ToolPipeline(registry, {definition.spec.name: definition.handler}, Phase.DIAGNOSE)
    result = await pipeline.execute('read_value', {'value': 'x'}, 'typed')
    assert result.executed and result.result == {'value': 'x'}
    assert seen == [(Input(value='x'), 'typed')]
    assert definition.spec.side_effect == 'read'
    assert registry.native_tools(Phase.DIAGNOSE) == [definition.spec.to_native()]


@pytest.mark.parametrize('returned', [{'wrong': 'x'}, {'value': 1}, {'value': 'x', 'extra': True}])
async def test_output_model_rejects_invalid_success_result(returned):
    async def handler(arguments, call_id):
        return returned

    definition = build_tool('typed.read', '读取值', Input, handler,
                            output_model=Output, phases=frozenset({Phase.DIAGNOSE}))
    pipeline = ToolPipeline(ToolRegistry([definition.spec]),
                            {definition.spec.name: definition.handler}, Phase.DIAGNOSE)
    result = await pipeline.execute('typed.read', {'value': 'x'}, 'bad-output')
    assert result.is_error and not result.executed
    assert result.error['type'] == 'ToolOutputError'
    assert '输出校验失败' in result.error['message']


@pytest.mark.parametrize('alias', ['alpha.one', 'AlphaOne', 'alpha_one', 'shared'])
@pytest.mark.parametrize('reverse', [False, True])
def test_alias_collisions_are_rejected_regardless_of_registration_order(alias, reverse):
    definitions = [ToolSpec('alpha.one', '一个工具', Input, aliases=('shared',)),
                   ToolSpec('beta.two', '另一个工具', Input, aliases=(alias,))]
    registry = ToolRegistry([definitions[1] if reverse else definitions[0]])
    with pytest.raises(ValueError, match='别名重复'):
        registry.register(definitions[0] if reverse else definitions[1])
    assert len(registry.specs) == 1


def test_disabled_tools_are_not_visible_or_callable():
    spec = ToolSpec('disabled.read', '停用工具', Input, enabled=False, aliases=('old_disabled',))
    registry = ToolRegistry([spec])
    for name in ['disabled.read', 'DisabledRead', 'disabled_read', 'old_disabled']:
        assert not registry.contains(name)
        with pytest.raises(ToolProtocolError):
            registry.get(name)
    assert not registry.contains_wire(spec.wire_name)
    with pytest.raises(ToolProtocolError):
        registry.get_wire(spec.wire_name)
    assert registry.visible(Phase.DIAGNOSE) == ()
    assert registry.native_tools(Phase.DIAGNOSE) == []


def test_build_tool_rejects_invalid_handler_and_unsafe_write_defaults():
    with pytest.raises(TypeError, match='可调用'):
        build_tool('typed.read', '读取值', Input, None)
    with pytest.raises(ValueError, match='幂等键'):
        build_tool('typed.write', '写入值', Input, lambda arguments, call_id: arguments,
                   side_effect='write')
    definition = build_tool('typed.read', '读取值', Input, lambda arguments, call_id: arguments)
    assert definition.spec.parallel_safe is False


def test_output_models_require_strict_objects_and_copy_json_schema():
    class LooseOutput(BaseModel):
        value: str

    for output_model in [LooseOutput, {'type': 'object'}, str]:
        with pytest.raises(ValueError, match='输出'):
            ToolSpec('typed.read', '读取值', Input, output_model=output_model)
    output_schema = {'type': 'object', 'properties': {'value': {'type': 'string'}},
                     'required': ['value'], 'additionalProperties': False}
    spec = ToolSpec('typed.read', '读取值', Input, output_model=output_schema)
    output_schema['properties']['value']['type'] = 'integer'
    assert spec.validate_output(Output(value='x')) == {'value': 'x'}
    with pytest.raises(ToolOutputError):
        spec.validate_output({'value': 1})


@pytest.mark.parametrize('wrapped', [False, True])
async def test_write_with_invalid_output_remains_unknown(tmp_path, wrapped):
    engine, state = make_engine(tmp_path / 'run')
    state.phase = Phase.DIAGNOSE
    changed = []

    async def handler(arguments, call_id):
        changed.append(arguments.value)
        value = {'value': 3}
        if wrapped:
            return ToolResult(call_id=call_id, name='typed.write', result=value, executed=True)
        return value

    definition = build_tool('typed.write', '写入值', Input, handler, output_model=Output,
                            side_effect='write', idempotency_key=digest)
    pipeline = ToolPipeline(ToolRegistry([definition.spec]),
        {definition.spec.name: definition.handler}, Phase.DIAGNOSE,
        operation=make_operation_executor(engine.store, state))
    with pytest.raises(ToolOperationUnknown) as error:
        await pipeline.execute('typed.write', {'value': 'x'}, 'invalid-write')
    assert error.value.status == 'UNKNOWN_OPERATION'
    assert error.value.details['requires_manual_review'] is True
    assert changed == ['x']
    assert 'invalid-write' not in pipeline.completed_calls
    assert next(iter(engine.store.operations.values()))['status'] == 'UNKNOWN'


async def test_error_results_preserve_diagnostic_payload_without_output_validation():
    async def handler(arguments, call_id):
        return ToolResult(call_id=call_id, name='typed.read', result={'diagnostic': 'missing'},
                          isError=True, error={'type': 'MissingValue'})

    definition = build_tool('typed.read', '读取值', Input, handler, output_model=Output)
    pipeline = ToolPipeline(ToolRegistry([definition.spec]),
                            {definition.spec.name: definition.handler}, Phase.DIAGNOSE)
    result = await pipeline.execute('typed.read', {'value': 'x'}, 'error-output')
    assert result.is_error and result.result == {'diagnostic': 'missing'}


async def test_aliases_share_canonical_identity_and_preserve_phase_gate():
    calls = []

    async def handler(arguments, call_id):
        calls.append(call_id)
        return {'value': arguments.value}

    definition = build_tool('typed.read', '读取值', Input, handler, output_model=Output,
                            aliases=('old.read',), phases=frozenset({Phase.DIAGNOSE}))
    registry = ToolRegistry([definition.spec])
    pipeline = ToolPipeline(registry, {'typed.read': definition.handler}, Phase.DIAGNOSE)
    first = await pipeline.execute('old.read', {'value': 'x'}, 'same')
    assert await pipeline.execute('TypedRead', {'value': 'x'}, 'same') == first
    assert pipeline.completed_calls['same'][0][0] == 'typed.read'
    assert calls == ['same']
    with pytest.raises(ToolProtocolError):
        registry.get('old.read', Phase.EXPLORE)
    with pytest.raises(ToolProtocolError):
        registry.get_wire('old.read')


def test_new_metadata_keeps_legacy_position_and_native_schema_compatible():
    spec = ToolSpec('typed.read', '读取值', Input, True, aliases=('old.read',),
                    output_model=Output, search_hint='结构化值')
    assert spec.parallel_safe and spec.output_limit_tokens == 4000
    assert spec.to_native() == {'type': 'function', 'function': {
        'name': 'TypedRead', 'description': '读取值', 'parameters': Input.model_json_schema()}}
