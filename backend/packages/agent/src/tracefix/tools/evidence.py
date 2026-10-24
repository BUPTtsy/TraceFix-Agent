"""将检查使用的源码工具结果绑定到当前 Run 证据。"""
from tracefix.runtime.contracts import CheckJudgement, digest


def attach_source_evidence_tools(engine, state, schema, context_provider, runtime_tools):
    if schema is not CheckJudgement:
        return
    for name in ('Read', 'Grep', 'Glob', 'code.analyze'):
        handler = runtime_tools.handlers.get(name)
        if handler is None:
            continue

        async def read_evidence(arguments, call_id, handler=handler, name=name):
            result = await handler(arguments, call_id)
            record = {'tool': name, 'arguments': arguments.model_dump(mode='json'),
                'source_manifest': state.source_manifest, 'patch_hash': state.patch_hash,
                'result_hash': digest(result)}
            reference = engine.put(state, record, name='代码工具检查证据')
            state.evidence_refs.append(reference)
            engine.store.save(state)
            context = context_provider()
            context['available_evidence_refs'] = list(dict.fromkeys(
                context.get('available_evidence_refs', []) + [reference]))
            context['evidence_refs'] = context['available_evidence_refs']
            engine.event(state, 'check.source.read', {'tool': name, 'artifact_ref': reference})
            return {**result, 'artifact_ref': reference}

        runtime_tools.handlers[name] = read_evidence
