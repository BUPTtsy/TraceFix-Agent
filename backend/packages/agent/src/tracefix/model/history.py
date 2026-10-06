"""History validation, canonical tool names and evidence-aware request budgeting."""
import copy
import json
from tracefix.model.prompts import serialize_request
from tracefix.runtime.contracts import digest
from tracefix.runtime.tools import model_tool_name


class ModelProtocol:
    @staticmethod
    def message_history(messages):
        result = []
        for message in messages:
            if isinstance(message, dict) or not hasattr(message, 'parts'):
                result.append(copy.deepcopy(message))
                continue
            if getattr(message, 'kind', 'request') == 'response':
                item = {'role': 'assistant', 'content': None}
                for part in message.parts:
                    if part.part_kind == 'text':
                        item['content'] = (item['content'] or '') + part.content
                    elif part.part_kind == 'thinking':
                        item['reasoning_content'] = item.get('reasoning_content', '') + part.content
                    elif part.part_kind == 'tool-call':
                        arguments = part.args if isinstance(part.args, str) else json.dumps(part.args, ensure_ascii=False)
                        item.setdefault('tool_calls', []).append({'id': part.tool_call_id,
                            'type': 'function', 'function': {'name': part.tool_name, 'arguments': arguments}})
                result.append(item)
            else:
                for part in message.parts:
                    if part.part_kind in {'system-prompt', 'user-prompt'}:
                        content = part.content
                        if not isinstance(content, str):
                            import base64
                            content = [{'type': 'text', 'text': item} if isinstance(item, str)
                                else {'type': 'image_url', 'image_url': {'url': 'data:' + item.media_type
                                    + ';base64,' + base64.b64encode(item.data).decode()}}
                                for item in content]
                        result.append({'role': 'system' if part.part_kind == 'system-prompt' else 'user',
                                       'content': content})
                    elif part.part_kind in {'tool-return', 'retry-prompt'}:
                        raw_content = getattr(part, 'content', '')
                        content = raw_content if isinstance(raw_content, str) else json.dumps(
                            raw_content, ensure_ascii=False, default=str)
                        if getattr(part, 'tool_name', None):
                            result.append({'role': 'tool', 'tool_call_id': getattr(part, 'tool_call_id', ''),
                                           'name': part.tool_name, 'content': content})
                        else:
                            result.append({'role': 'user', 'content': content})
        return result

    @staticmethod
    def _native_tools(schema):
        # 工具 schema 只声明动作格式；实际执行仍受运行时策略控制。
        from tracefix.runtime.contracts import BrowserAction, Decision
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

    @classmethod
    def browser_action(cls, name, arguments):
        import jsonschema
        from tracefix.runtime.contracts import BrowserAction
        definitions = {tool['function']['name']: tool['function']['parameters']
                       for tool in cls._native_tools(BrowserAction)}
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

    @classmethod
    def _validated_calls(cls, calls, native_tools, completed_calls):
        # 整批校验通过后才允许执行，避免前半批已执行而后半批存在非法调用。
        if not native_tools:
            raise ValueError('当前模型输出类型或 tool_mode 不允许调用浏览器工具')
        if not isinstance(calls, list) or not calls:
            raise ValueError('tool_calls 必须为非空数组')
        validated = []
        seen = set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type') != 'function':
                raise ValueError('tool_call 必须为 function 类型')
            call_id = call.get('id')
            function = call.get('function')
            if (not isinstance(call_id, str) or not call_id.strip()
                    or call_id in seen or not isinstance(function, dict)):
                raise ValueError('tool_call 的 id/function 无效或同批 id 重复')
            seen.add(call_id)
            name = function.get('name')
            raw_arguments = function.get('arguments')
            if not isinstance(raw_arguments, str):
                raise ValueError('function.arguments 必须是 JSON 字符串')
            arguments = json.loads(raw_arguments)
            definitions = {tool['function']['name']: tool['function']['parameters'] for tool in native_tools}
            if name not in definitions:
                raise ValueError('未知或未授权的工具：' + str(name))
            import jsonschema
            try:
                jsonschema.validate(arguments, definitions[name])
            except jsonschema.ValidationError as error:
                raise ValueError('工具参数校验失败：' + error.message) from error
            if name.startswith('Browser'):
                cls.browser_action(name, arguments)
            identity = (name, json.dumps(arguments, sort_keys=True))
            if call_id in completed_calls and completed_calls[call_id][0] != identity:
                raise ValueError('tool_call_id 不可复用于不同参数')
            validated.append((call_id, name, arguments, identity))
        return validated

    @classmethod
    def _completed_history(cls, messages, native_tools):
        if not isinstance(messages, list) or not messages:
            raise ValueError('messages 必须为非空数组')
        completed = {}
        pending = {}
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError('历史消息必须为对象')
            role = message.get('role')
            if role == 'tool':
                call_id = message.get('tool_call_id')
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ValueError('tool 消息缺少配对的 assistant tool_call')
                identity = pending.pop(call_id)
                if (not isinstance(message.get('content'), str)
                        or message.get('name', identity[0]) != identity[0]):
                    raise ValueError('tool 消息的 content/name 无效')
                completed[call_id] = (identity, message['content'])
            else:
                if pending:
                    raise ValueError('assistant tool_calls 尚未收到完整工具结果')
                if role not in {'system', 'user', 'assistant'}:
                    raise ValueError('历史消息 role 无效')
                if message.get('tool_calls') is not None:
                    if role != 'assistant':
                        raise ValueError('只有 assistant 消息可以包含 tool_calls')
                    pending = {call_id: identity for call_id, name, arguments, identity in
                               cls._validated_calls(message['tool_calls'], native_tools, completed)}
        if pending:
            raise ValueError('assistant tool_calls 缺少工具结果，不能自动重放')
        return completed

    @staticmethod
    def _wire_history(messages, registry, phase):
        history = copy.deepcopy(messages)
        if not isinstance(history, list):
            return history
        for message in history:
            if not isinstance(message, dict):
                continue
            functions = [call.get('function') for call in message.get('tool_calls', [])
                         if isinstance(call, dict)]
            if message.get('role') == 'tool' and 'name' in message:
                functions.append(message)
            for function in functions:
                if not isinstance(function, dict) or not isinstance(function.get('name'), str):
                    continue
                name = function['name']
                if registry.contains(name, phase):
                    function['name'] = registry.get(name, phase).wire_name
        return history

    @staticmethod
    def _payload_tokens(counter, payload):
        bounded = copy.deepcopy(payload)
        image_count = 0
        for message in bounded.get('messages', []):
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get('type') != 'image_url':
                    continue
                image_count += 1
                part['image_url'] = {'url': '[IMAGE]'}
        return counter.count(bounded) + image_count * 4096

    @classmethod
    def _protected_tool_result(cls, value):
        if isinstance(value, list):
            return any(cls._protected_tool_result(item) for item in value)
        if not isinstance(value, dict):
            return False
        if (value.get('error') or value.get('isError') or value.get('is_error')
                or value.get('passed') is False or value.get('executed') is False):
            return True
        for field in ('status', 'operation_status', 'business_outcome'):
            status = value.get(field)
            if isinstance(status, str) and any(marker in status.casefold() for marker in
                    ('unknown', 'waiting', 'pending', 'running', 'failed', 'cancelled', 'canceled')):
                return True
        return any(cls._protected_tool_result(item) for item in value.values())

    @classmethod
    def _project_tool_message(cls, message, metadata):
        if not isinstance(message, dict) or message.get('role') != 'tool':
            return False
        reference = metadata.get('result_ref') if isinstance(metadata, dict) else None
        content = message.get('content')
        binding = metadata.get('binding') if isinstance(metadata, dict) else None
        if (not isinstance(reference, str) or not reference.strip() or not isinstance(content, str)
                or metadata.get('expandable') is not True
                or metadata.get('content_hash') != digest(content)
                or len(content) > 16000 or len(content.splitlines()) > 200
                or not isinstance(binding, dict)
                or not {'scope_id', 'source_manifest', 'patch_hash', 'environment_digest',
                        'test_spec_hash'}.issubset(binding)):
            return False
        try:
            original = json.loads(content)
        except (TypeError, ValueError):
            return False
        if not isinstance(original, dict) or cls._protected_tool_result(original):
            return False
        projected = copy.deepcopy(original)
        name = message.get('name', '')
        browser_fields = {'isError', 'is_error', 'executed', 'error', 'business_outcome',
                          'observation_ref', 'observation', 'receipt', 'operation_id',
                          'status', 'operation_status', 'artifact_ref', 'result_ref', 'truncated'}
        result_fields = {'call_id', 'name', 'result', 'isError', 'is_error', 'executed',
                         'error', 'artifact_ref', 'truncated'}
        if name in {'BrowserNavigate', 'BrowserClick', 'BrowserType', 'BrowserSelect',
                    'BrowserPress', 'BrowserSnapshot'}:
            observation = original.get('observation')
            if (not set(original).issubset(browser_fields) or original.get('executed') is not True
                    or not isinstance(observation, dict)
                    or not isinstance(observation.get('snapshot'), str)):
                return False
            projected['observation']['snapshot'] = '[历史观测正文已投影；按 tool_history_ref 展开，已结算动作不得重放]'
            omitted_fields = ['observation.snapshot']
        elif name == 'Bash':
            result = original.get('result')
            if (not set(original).issubset(result_fields)
                    or original.get('call_id') != message.get('tool_call_id')
                    or model_tool_name(str(original.get('name', ''))) != name
                    or original.get('executed') is not True or not isinstance(result, dict)
                    or not set(result).issubset({'passed', 'exit_code', 'output', 'files_changed',
                                                'discarded_changes', 'cwd'})
                    or result.get('passed') is not True or result.get('exit_code') != 0
                    or not isinstance(result.get('output'), str)):
                return False
            projected['result']['output'] = '[历史命令正文已投影；按 tool_history_ref 展开，已结算动作不得重放]'
            omitted_fields = ['result.output']
        else:
            return False
        projected['tool_history_ref'] = {
            'result_ref': reference, 'channel': 'tool_content',
            'content_hash': digest(content), 'content_length': len(content),
            'coverage': 'projected', 'omitted_fields': omitted_fields,
            'binding': copy.deepcopy(binding),
            'lookup_hint': {'tool': 'context.expand', 'ref': reference,
                            'channel': 'tool_content', 'max_chars': 16000}}
        message['content'] = json.dumps(projected, ensure_ascii=False, separators=(',', ':'))
        return True

    @staticmethod
    def _tool_history_candidates(messages):
        groups = []
        pending = set()
        indexes = []
        for index, message in enumerate(messages):
            if message.get('role') == 'assistant' and message.get('tool_calls'):
                if pending:
                    return []
                pending = {call['id'] for call in message['tool_calls']}
                indexes = []
            elif message.get('role') == 'tool':
                call_id = message.get('tool_call_id')
                if call_id not in pending:
                    return []
                pending.remove(call_id)
                indexes.append(index)
                if not pending:
                    groups.append(indexes)
            elif pending:
                return []
        if pending:
            return []
        return [index for group in groups[:-1] for index in group]

    @classmethod
    def _project_next_tool(cls, payload, tool_result_refs, counter, candidates):
        before = cls._payload_tokens(counter, payload)
        while candidates:
            index = candidates.pop(0)
            message = payload['messages'][index]
            call_id = message.get('tool_call_id')
            original_content = message.get('content')
            if not cls._project_tool_message(message, tool_result_refs.get(call_id)):
                continue
            if cls._payload_tokens(counter, payload) < before:
                return call_id
            message['content'] = original_content
        return None

    @staticmethod
    def _replace_context(messages, text):
        for message in messages:
            if message.get('role') != 'user':
                continue
            content = message.get('content')
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'text':
                        part['text'] = text
                        return
            else:
                message['content'] = text
                return

    @classmethod
    def _protocol_tokens(cls, payload, schema, counter):
        overhead = copy.deepcopy(payload)
        cls._replace_context(overhead['messages'], '')
        overhead['response_json_schema'] = schema.model_json_schema()
        return cls._payload_tokens(counter, overhead)

    @classmethod
    def _budget_payload(cls, payload, schema, context, assembler, tool_result_refs,
                        can_expand, *, preserve=False):
        from tracefix.knowledge.assembler import ContextWindowError

        counter = assembler.counter
        before = cls._payload_tokens(counter, payload)
        candidates = cls._tool_history_candidates(payload['messages']) if can_expand else []
        projected = []
        assembly = None
        extra_tokens = cls._protocol_tokens(payload, schema, counter)
        if not preserve:
            while True:
                try:
                    assembly = assembler.assemble(context, extra_tokens=extra_tokens)
                    break
                except ContextWindowError:
                    call_id = cls._project_next_tool(payload, tool_result_refs, counter, candidates)
                    if call_id is None:
                        raise
                    projected.append(call_id)
                    extra_tokens = cls._protocol_tokens(payload, schema, counter)
            cls._replace_context(payload['messages'], serialize_request(schema, assembly.context))
            for correction in range(3):
                required = cls._payload_tokens(counter, payload)
                if required <= assembler.available:
                    break
                adjusted_extra = extra_tokens + required - assembler.available
                try:
                    adjusted = assembler.assemble(context, extra_tokens=adjusted_extra)
                except ContextWindowError:
                    break
                proposal = copy.deepcopy(payload)
                cls._replace_context(proposal['messages'], serialize_request(schema, adjusted.context))
                if cls._payload_tokens(counter, proposal) >= required:
                    break
                payload['messages'] = proposal['messages']
                assembly = adjusted
                extra_tokens = adjusted_extra
        required = cls._payload_tokens(counter, payload)
        while required > assembler.available:
            call_id = cls._project_next_tool(payload, tool_result_refs, counter, candidates)
            if call_id is None:
                break
            projected.append(call_id)
            required = cls._payload_tokens(counter, payload)
        if assembly is not None:
            manifest = copy.deepcopy(assembly.manifest)
        else:
            manifest = {'version': 'tracefix/context/1', 'counter': counter.name,
                        'exact_tokenizer': counter.exact, 'context_window': assembler.context_window,
                        'input_limit': assembler.available, 'tokens_before': before,
                        'tokens_after': required, 'context_hash': digest(context),
                        'blocks': [], 'workset': {}}
        manifest.update(request_tokens=required,
                        protocol_tokens=cls._protocol_tokens(payload, schema, counter),
                        image_tokens_reserved=sum(4096 for message in payload['messages']
                            if isinstance(message.get('content'), list) for part in message['content']
                            if isinstance(part, dict) and part.get('type') == 'image_url'))
        if projected:
            manifest['tool_history_projection'] = {
                'call_ids': projected, 'coverage': 'artifact_ref',
                'result_refs': {call_id: tool_result_refs[call_id]['result_ref'] for call_id in projected},
                'unprojected_call_ids': [message['tool_call_id'] for message in payload['messages']
                    if message.get('role') == 'tool' and message['tool_call_id'] not in projected]}
            manifest['tool_history_projected'] = True
        compacted = bool(projected) or bool(assembly and assembly.compacted)
        return manifest, compacted
