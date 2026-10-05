"""MCP 浏览器传输、快照解析以及动作结果的不确定性处理。"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

from tracefix.runtime.contracts import digest, new_id
from tracefix.storage.artifacts import sanitize

ELEMENT = re.compile(r'^\s*- (?P<role>[\w-]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>[^\n]*?)(?:\s*:\s*.*)?$', re.MULTILINE)
UPSTREAM_TRUNCATION = re.compile(
    r'(?im)^\s*(?:\.{3}|…)?\s*(?:\[[^\n]*(?:truncat(?:ed|ion)|内容已省略)[^\n]*\]'
    r'|(?:snapshot|output|content|response)\s+(?:is\s+|was\s+)?truncated\b)')
SNAPSHOT_LIMIT = 40000
SNAPSHOT_OMISSION = '页面快照中间内容已省略'


def elements(snapshot):
    """将可访问性快照解析为带角色、名称和引用的元素记录。"""
    items = []
    for m in ELEMENT.finditer(snapshot):
        attrs = m['attrs']
        ref = re.search(r'\[ref=([^\]]+)\]', attrs)
        items.append({'role': m['role'], 'name': m['name'] or '', 'ref': ref[1] if ref else None, 'attrs': attrs})
    return items


def resolve_locator(snapshot, locator):
    """把模型定位器解析为唯一元素引用；零个或多个匹配均视为错误。"""
    matches = [e for e in elements(snapshot) if e['role'] == locator.role and e['name'] == locator.name]
    if len(matches) != 1 or not matches[0]['ref']:
        raise ValueError(f"定位器无法唯一匹配元素：{locator.role} / {locator.name}")
    return matches[0]['ref']


def resolve_action_locator(snapshot, locator):
    """在新快照中唯一定位，并阻止点击活动弹窗外的背景元素。"""
    target_ref = resolve_locator(snapshot, locator)
    dialog_ancestors = []
    has_dialog = False
    target_in_dialog = False
    for line in snapshot.splitlines():
        match = ELEMENT.fullmatch(line)
        if not match:
            continue
        indentation = len(line) - len(line.lstrip())
        while dialog_ancestors and indentation <= dialog_ancestors[-1]:
            dialog_ancestors.pop()
        if match['role'] in {'dialog', 'alertdialog'} and '[hidden]' not in match['attrs']:
            has_dialog = True
            dialog_ancestors.append(indentation)
        if f'[ref={target_ref}]' in match['attrs']:
            target_in_dialog = bool(dialog_ancestors)
    if has_dialog and not target_in_dialog:
        raise ValueError('目标元素位于活动弹窗之外')
    return target_ref


def validate_spec_observation(spec, observation):
    """检查规范中的定位器是否能在初始快照中精确对应页面元素。"""
    observed = elements(observation['snapshot'])
    locators = [check.locator for check in spec.assertions + spec.regression_assertions
                if check.condition != 'absent']
    locators += [action.locator for action in spec.regression_plan if action.locator]
    for scenario in spec.behavior_scenarios:
        locators += [step.action.locator for step in scenario.steps if step.action.locator]
        locators += [check.locator for step in scenario.steps for check in step.assertions
                     if check.condition != 'absent']
    for locator in locators:
        same_role = [item for item in observed if item['role'] == locator.role]
        exact = [item for item in same_role if item['name'] == locator.name]
        if len(exact) > 1:
            raise ValueError(f'locator {locator.role} / {locator.name} 在初始页面不唯一')
        if exact:
            continue
        similar = [item['name'] for item in same_role if locator.name and
                   (locator.name.casefold() in item['name'].casefold()
                    or item['name'] and item['name'].casefold() in locator.name.casefold())]
        if similar:
            raise ValueError(f'locator.name 必须精确匹配页面可访问名称；{locator.role} / {locator.name} '
                             f'未匹配，页面中相近的真实名称为 {similar}。请核对原始目标，保留断言条件。')


def assertions(snapshot, checks):
    """在单个快照上执行元素存在、状态和禁用条件断言。"""
    results = []
    for a in checks:
        found = [e for e in elements(snapshot) if e['role'] == a.locator.role and e['name'] == a.locator.name]
        passed = len(found) == 1
        if a.condition == 'absent':
            passed = not found
        elif passed and a.condition in {'checked', 'disabled'}:
            passed = f'[{a.condition}]' in found[0]['attrs']
        elif passed and a.condition == 'enabled':
            passed = '[disabled]' not in found[0]['attrs']
        elif passed and a.condition == 'unchecked':
            # 仅可勾选角色能证明取消勾选；普通元素缺少 checked 或控件为 mixed 都不能冒充 unchecked。
            passed = (found[0]['role'] in {'checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'}
                      and re.search(r'\[checked(?:[=\s][^\]]*)?\]', found[0]['attrs']) is None)
        results.append({'assertion': a.model_dump(), 'passed': passed, 'matches': len(found)})
    return {'passed': bool(results) and all(r['passed'] for r in results), 'assertions': results}


class MCPConnectionError(RuntimeError):
    """MCP 传输失败；请求可能已经到达浏览器但结果尚不可知。"""

    status = 'WAITING_NETWORK'
    category = 'browser_transport_error'
    retryable = False

    def __init__(self, message, *, details=None):
        self.details = {
            'status': self.status,
            'operation_status': self.status,
            'category': self.category,
            'retryable': self.retryable,
            'requires_manual_review': True,
            **(details or {}),
        }
        super().__init__(message)


class MCPActionUnknown(MCPConnectionError):
    """浏览器副作用可能已经发生，但动作结果无法确定。"""

    status = 'UNKNOWN_OPERATION'
    category = 'browser_action_unknown'
    retryable = False

    def __init__(self, action_id, kind, cause, *, details=None):
        self.action_id = action_id
        self.kind = kind
        self.cause = cause
        cause_details = getattr(cause, 'details', {})
        super().__init__(f'MCP 动作结果未知：{kind}（{action_id}）：{cause}', details={
            'action_id': action_id,
            'kind': kind,
            'cause_type': type(cause).__name__,
            'cause_status': getattr(cause, 'status', None),
            'cause_category': getattr(cause, 'category', None),
            'cause_details': cause_details,
            'requires_new_run': True,
            **(details or {}),
        })


@dataclass
class _MCPRequest:
    name: str
    args: dict
    result: asyncio.Future
    dispatched: bool = False


class MCPBrowser:
    """通过单独的服务任务串行化 MCP 会话，避免跨任务使用取消作用域。"""

    # Names are accepted only after actual discovery + input schema validation.
    MAP = {'snapshot': 'browser_snapshot', 'screenshot': 'browser_take_screenshot',
           'navigate': 'browser_navigate', 'click': 'browser_click', 'type': 'browser_type',
           'select': 'browser_select_option', 'press': 'browser_press_key',
           'console': 'browser_console_messages', 'network': 'browser_network_requests'}
    DIAGNOSTIC_DEFAULTS = {'console': {'level': 'info'}, 'network': {'includeStatic': False}}

    def __init__(self, command, policy, timeout=45, env=None):
        """初始化传输队列、观测缓存和连接状态机。"""
        self.command, self.policy, self.timeout = command, policy, timeout
        self.stack, self.session = None, None
        self.tools = {}
        self.env = env or {}
        self.observation = None
        self.worker = None
        self.queue = asyncio.Queue(maxsize=1)
        self.connection_state = 'closed'
        self.connection_error = None
        self.last_action = None
        self._active_request = None
        self._action_dispatched = False
        self._operation_lock = asyncio.Lock()
        self._snapshot_guard = None
        self._page_generation = 0
        self._page_version = None
        self._generation_invalidated = False

    async def open(self):
        """启动会话所有者任务，并等待工具发现和初始化完成。"""
        if self.worker and not self.worker.done():
            raise RuntimeError('浏览器已处于打开状态')
        self.connection_state = 'connecting'
        self.connection_error = None
        self.observation = None
        self._snapshot_guard = None
        self._page_version = None
        self._generation_invalidated = True
        ready = asyncio.get_running_loop().create_future()
        self.worker = asyncio.create_task(self._serve(ready))
        try:
            await ready
        except BaseException:
            await self.close()
            raise

    async def _serve(self, ready):
        # AnyIO transport cancel scopes must enter and exit in the SAME task.
        # LangGraph runs separate nodes in separate tasks, so this dedicated
        # owner holds MCP lifetime and serializes calls through a bounded queue.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        try:
            async with AsyncExitStack() as stack:
                transport = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=self.command[0], args=self.command[1:], env={'PATH': __import__('os').environ.get('PATH', ''), **self.env})))
                session = await stack.enter_async_context(ClientSession(*transport))
                await asyncio.wait_for(session.initialize(), self.timeout)
                listing = await session.list_tools()
                self.tools = {t.name: t.inputSchema for t in listing.tools}
                for k in ('snapshot', 'navigate', 'click', 'type', 'screenshot'):
                    if self.MAP[k] not in self.tools:
                        raise RuntimeError(f'缺少 MCP 能力：{k}')
                self.connection_state = 'ready'
                ready.set_result(True)
                while True:
                    item = await self.queue.get()
                    if item is None:
                        break
                    request = item
                    name, args, result = request.name, request.args, request.result
                    if result.cancelled():
                        continue
                    try:
                        request.dispatched = True
                        response = await asyncio.wait_for(session.call_tool(name, args), self.timeout)
                        if not result.done():
                            result.set_result(response)
                    except Exception as e:
                        if self._is_connection_error(e):
                            self._mark_connection_lost(e, details={
                                'tool': name, 'dispatched': request.dispatched,
                            })
                            if not result.done():
                                result.set_exception(self.connection_error)
                            break
                        if not result.done():
                            result.set_exception(e)
        except BaseException as e:
            if not ready.done():
                self.connection_state = 'closed'
                ready.set_exception(e)
            elif not isinstance(e, asyncio.CancelledError):
                self._mark_connection_lost(e)

    async def close(self):
        """停止服务任务并清空活动请求、工具发现和观测缓存。"""
        worker = self.worker
        try:
            if worker and not worker.done():
                try:
                    self.queue.put_nowait(None)
                except asyncio.QueueFull:
                    worker.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(worker), min(self.timeout + 5, 10))
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                except BaseException:
                    await asyncio.gather(worker, return_exceptions=True)
        finally:
            if self._active_request and not self._active_request.result.done():
                self._active_request.result.cancel()
            if self.worker is worker:
                self.worker = None
            self.stack, self.session, self.observation = None, None, None
            self.tools = {}
            self.connection_state = 'closed'
            self.connection_error = None
            self._active_request = None
            self._action_dispatched = False
            self.queue = asyncio.Queue(maxsize=1)

    async def reconnect(self):
        """重建 MCP 会话；绝不自动重试已中断的动作。"""
        await self.close()
        await self.open()

    async def _recover_connection(self, error, *, dispatched):
        if error.details.get('reconnect_attempted'):
            return
        self._mark_connection_lost(error)
        if dispatched is not False or self._action_dispatched or self.worker is None:
            return
        error.details['reconnect_attempted'] = True
        try:
            await self.reconnect()
        except Exception as reconnect_error:
            self._mark_connection_lost(error)
            error.details.update(reconnected=False, reconnect_error=sanitize(
                f'{type(reconnect_error).__name__}: {reconnect_error}'))
        else:
            error.details.update(reconnected=True, requires_new_observation=True)

    @staticmethod
    def _is_connection_error(error):
        if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True
        details = getattr(error, 'error', None)
        code = getattr(details, 'code', None)
        if code in {-32000, -32001, 408}:
            return True
        message = str(error).casefold()
        return any(term in message for term in (
            'connection closed', 'connection reset', 'broken pipe',
            'closed resource', 'transport closed', 'eof'))

    def _mark_connection_lost(self, error, *, details=None):
        """记录连接丢失并取消服务任务，使后续调用快速失败。"""
        self.connection_state = 'lost'
        self.connection_error = error if isinstance(error, MCPConnectionError) else MCPConnectionError(
            f'MCP 传输已断开：{type(error).__name__}：{error}', details=details)
        if isinstance(self.connection_error, MCPConnectionError) and details:
            self.connection_error.details.update(details)
        worker = self.worker
        if worker and not worker.done() and worker is not asyncio.current_task():
            worker.cancel()
        self.observation = None
        self.tools = {}

    async def call(self, kind, args=None):
        """校验工具参数后排队调用，并把传输错误转换为可审计异常。"""
        try:
            return await self._call(kind, args)
        except MCPConnectionError as error:
            await self._recover_connection(error, dispatched=error.details.get('dispatched'))
            raise

    async def _call(self, kind, args=None):
        import jsonschema
        name = self.MAP[kind]
        if self.connection_state == 'lost':
            raise MCPConnectionError('MCP 连接在派发前不可用', details={
                'tool': name, 'dispatched': False,
            })
        if name not in self.tools:
            raise RuntimeError(f'未发现该 MCP 能力：{name}')
        args = args or {}
        jsonschema.validate(args, self.tools[name])
        if self.connection_state != 'ready' or not self.worker or self.worker.done():
            raise MCPConnectionError('MCP 连接未处于活动状态', details={
                'tool': name, 'dispatched': False,
            })
        future = asyncio.get_running_loop().create_future()
        request = _MCPRequest(name, args, future)
        self._active_request = request
        try:
            await self.queue.put(request)
            result = await asyncio.wait_for(future, self.timeout+2)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._is_connection_error(error):
                self._mark_connection_lost(error, details={
                    'tool': name, 'dispatched': request.dispatched,
                })
                raise self.connection_error from error
            raise
        finally:
            if self._active_request is request:
                self._active_request = None
        if result.isError:
            raise RuntimeError('MCP 工具执行失败：' + sanitize(' '.join(c.text for c in result.content if c.type == 'text'))[:1000])
        return result.content

    def diagnostic_args(self, kind):
        """根据已发现的 JSON Schema 生成诊断工具所需的最小参数。"""
        schema = self.tools[self.MAP[kind]]
        defaults = self.DIAGNOSTIC_DEFAULTS[kind]
        required = schema.get('required', [])
        unknown = [name for name in required if name not in defaults]
        if unknown:
            raise RuntimeError(f'MCP {kind} 工具出现未知必填参数：{", ".join(unknown)}')
        return {name: defaults[name] for name in required}

    async def observe(self):
        """获取快照、截图和诊断信息，并缓存带唯一 ID 的观测。"""
        async with self._operation_lock:
            return await self._observe()

    async def _observe(self):
        try:
            return await self._collect_observation()
        except MCPConnectionError:
            self.observation = None
            raise

    @staticmethod
    def _collect_text(blocks, captured_at):
        text_blocks = [block for block in blocks if block.type == 'text']
        raw = '\n'.join(block.text for block in text_blocks)
        text = sanitize(raw)
        collector_truncated = False
        if len(text) > SNAPSHOT_LIMIT:
            marker = f'\n{SNAPSHOT_OMISSION}\n'
            available = SNAPSHOT_LIMIT - len(marker)
            head_limit = available // 2
            tail_limit = available - head_limit
            head_end = text.rfind('\n', 0, head_limit)
            head = text[:head_end + 1] if head_end >= 0 else text[:head_limit]
            tail_start = text.find('\n', max(0, len(text) - tail_limit))
            tail = text[tail_start + 1:] if tail_start >= 0 else text[-tail_limit:]
            text = head + marker + tail
            collector_truncated = True
        upstream_truncated = bool(UPSTREAM_TRUNCATION.search(raw))
        for block in text_blocks:
            metadata = getattr(block, 'meta', None) or getattr(block, '_meta', None) or {}
            if isinstance(metadata, dict):
                upstream_truncated = upstream_truncated or any(
                    metadata.get(key) is True for key in ('truncated', 'isTruncated', 'upstream_truncated'))
                upstream_truncated = upstream_truncated or metadata.get('status') == 'truncated'
        status = 'truncated' if upstream_truncated else 'available' if text_blocks else 'unavailable'
        return text, {
            'status': status, 'captured_at': captured_at,
            'content_version': digest(text) if text_blocks else None,
            'provider_characters': len(raw), 'collector_characters': len(text),
            'upstream_truncated': upstream_truncated, 'collector_truncated': collector_truncated,
        }

    async def _collect_snapshot(self):
        blocks = await self.call('snapshot')
        captured_at = time.time()
        text, metadata = self._collect_text(blocks, captured_at)
        match = re.search(r'(?:Page URL:|URL:)\s*(https?://\S+)', text)
        if not match:
            raise RuntimeError('MCP 快照中缺少页面 URL')
        url = match[1].rstrip('`')
        self.policy.url(url)
        page_version = digest({'url': url, 'snapshot': text})
        if self._generation_invalidated or page_version != self._page_version:
            self._page_generation += 1
        self._page_version = page_version
        self._generation_invalidated = False
        self._snapshot_guard = text
        observation = {
            'id': new_id('obs'), 'url': url, 'snapshot': text,
            'page_generation': self._page_generation, 'observed_at': captured_at,
            'collection': {
                'version': 1, 'captured_at': captured_at,
                'content_version': metadata['content_version'],
                'channels': {'snapshot': metadata, 'a11y': metadata},
            },
        }
        self.observation = observation
        return observation

    async def _collect_observation(self):
        observation = await self._collect_snapshot()
        channels = observation['collection']['channels']
        images = await self.call('screenshot', {'type': 'png'})
        png = next((base64.b64decode(c.data) for c in images if c.type == 'image'), None)
        if not png:
            raise RuntimeError('MCP 结果中缺少所需的截图')
        channels['screenshot'] = {
            'status': 'available', 'captured_at': time.time(), 'content_version': digest(png),
            'collector_bytes': len(png), 'upstream_truncated': False, 'collector_truncated': False,
        }
        extra = {}
        for channel in ('console', 'network'):
            if self.MAP[channel] in self.tools:
                result = await self.call(channel, self.diagnostic_args(channel))
                extra[channel], channels[channel] = self._collect_text(result, time.time())
            else:
                extra[channel] = '该能力不可用'
                channels[channel] = {
                    'status': 'unavailable', 'captured_at': None, 'content_version': None,
                    'provider_characters': None, 'collector_characters': 0,
                    'upstream_truncated': False, 'collector_truncated': False,
                }
        self.observation = {**observation, **extra}
        return {**self.observation, 'png': png}

    async def action(self, action):
        """串行执行标准动作，区分工具完成与尚未断言的业务状态。"""
        async with self._operation_lock:
            return await self._action(action)

    async def _action(self, action):
        if action.kind in {'observe', 'finish'}:
            return await self._observe()
        if action.kind not in {'navigate', 'click', 'type', 'select', 'press'}:
            raise PermissionError('不支持的浏览器动作')
        if action.kind in {'click', 'type', 'select', 'press'}:
            if not self.observation or action.observation_id != self.observation['id']:
                raise PermissionError('MCP 页面观测已过期；请重新观测后再执行动作')
            generation = getattr(action, 'page_generation', None)
            if generation is not None and generation != self.observation.get('page_generation'):
                raise PermissionError('MCP 页面 generation 已过期；请重新观测后再执行动作')
            if action.kind in {'click', 'type', 'select'}:
                try:
                    current_ref = resolve_locator(self._snapshot_guard or self.observation['snapshot'], action.locator)
                except ValueError as error:
                    raise PermissionError('MCP 元素定位器已不在当前页面') from error
                if action.element_ref != current_ref:
                    raise PermissionError('MCP 元素引用已失效')
        action_id = new_id('mcp-action')
        self.last_action = {
            'action_id': action_id,
            'kind': action.kind,
            'observation_id': action.observation_id,
            'element_ref': action.element_ref,
            'args_digest': digest(action.model_dump()),
            'started_at': time.time(),
            'status': 'IN_FLIGHT',
            'pre_observation_id': self.observation['id'] if self.observation else None,
            'pre_page_generation': self.observation.get('page_generation') if self.observation else None,
            'before_observation_id': self.observation['id'] if self.observation else None,
            'before_page_generation': self.observation.get('page_generation') if self.observation else None,
            'post_observation_id': None,
            'post_page_generation': None,
            'after_observation_id': None,
            'after_page_generation': None,
            'business_status': 'unknown',
        }
        self._action_dispatched = False
        try:
            result = await self._execute_action(action)
        except MCPConnectionError as error:
            dispatched = self._action_dispatched or error.details.get('dispatched')
            await self._recover_connection(error, dispatched=dispatched)
            self.observation = None
            if dispatched is False:
                self.last_action.update(status='WAITING_NETWORK', finished_at=time.time(),
                                         error=type(error).__name__, details=error.details)
                raise
            self.last_action.update(status='UNKNOWN', finished_at=time.time(),
                                    error=type(error).__name__, details=error.details)
            if isinstance(error, MCPActionUnknown):
                raise
            raise MCPActionUnknown(action_id, action.kind, error, details={
                'observation_id': action.observation_id,
                'element_ref': action.element_ref,
                'action_args_digest': self.last_action['args_digest'],
                'started_at': self.last_action['started_at'],
                'finished_at': self.last_action['finished_at'],
                'dispatched': dispatched,
            }) from error
        except Exception as error:
            self.last_action.update(status='FAILED', finished_at=time.time(), error=type(error).__name__)
            raise
        self.last_action.update(status='DONE', finished_at=time.time(),
                                post_observation_id=result['id'],
                                post_page_generation=result.get('page_generation'),
                                after_observation_id=result['id'],
                                after_page_generation=result.get('page_generation'))
        self._action_dispatched = False
        return result

    async def _execute_action(self, action):
        """将领域动作映射到 MCP 工具调用，再返回动作后的新观测。"""
        if action.kind == 'navigate':
            self.policy.url(action.value)
            await self.call('navigate', {'url': action.value})
            self._action_dispatched = True
            self._generation_invalidated = True
        elif action.kind in {'click', 'type', 'select'}:
            if self.MAP['snapshot'] in self.tools:
                observation = await self._collect_snapshot()
            else:
                observation = self.observation
                if not observation:
                    raise MCPConnectionError('MCP 动作缺少可用于定位的页面观测', details={
                        'tool': self.MAP[action.kind], 'dispatched': False,
                    })
            try:
                fresh_ref = resolve_action_locator(
                    self._snapshot_guard or observation['snapshot'], action.locator)
            except ValueError as error:
                raise PermissionError('MCP 新快照无法安全唯一重定位；请重新观测') from error
            self.last_action.update(grounding_observation_id=observation['id'],
                                    grounding_page_generation=observation['page_generation'],
                                    dispatched_element_ref=fresh_ref)
            args = {'element': f'{action.locator.role} {action.locator.name}', 'ref': fresh_ref}
            if action.kind == 'type':
                args['text'] = action.value
            if action.kind == 'select':
                args['values'] = [action.value]
            await self.call(action.kind, args)
            self._action_dispatched = True
        elif action.kind == 'press':
            await self.call('press', {'key': action.value})
            self._action_dispatched = True
            if action.value.casefold().replace(' ', '') in {'f5', 'control+r', 'ctrl+r', 'meta+r', 'command+r'}:
                self._generation_invalidated = True
        elif action.kind not in {'observe', 'finish'}:
            raise PermissionError('不支持的标准动作')
        observer_override = self.__dict__.get('observe')
        if observer_override is not None:
            return await observer_override()
        return await self._observe()
