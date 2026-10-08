from __future__ import annotations

import asyncio
import ctypes
import json
import multiprocessing
import os
import signal
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path

from tracefix.runtime.contracts import digest, new_id


_active_operation = ContextVar('tracefix_r05_operation', default=None)
_active_owners = ContextVar('tracefix_r05_owners', default=())


def apply_effect_receipt():
    guard = _active_operation.get()
    if guard is None:
        raise EffectBoundaryError('父进程回写缺少持久化资源 fence')
    return guard()


class EffectBoundaryError(RuntimeError):
    status = 'UNKNOWN_OPERATION'
    category = 'tool_execution'

    def __init__(self, message):
        super().__init__(message)
        self.details = dict(status=self.status, category=self.category,
                            requires_manual_review=True, retryable=False)


def file_resource(path):
    return 'file:' + os.path.normcase(str(Path(path).resolve())).replace(chr(92), '/').rstrip('/')


def operation_resources(intent, resources=None):
    values = resources if resources is not None else intent.get('resources')
    if values is None:
        arguments = intent.get('arguments', {})
        path = arguments.get('file_path') or arguments.get('notebook_path')
        values = [file_resource(path)] if path else ['*']
    if (not isinstance(values, (list, tuple)) or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise ValueError('操作必须声明非空资源集合')
    return sorted(set(values))


def resources_intersect(left, right):
    for first in left:
        for second in right:
            if first == '*' or second == '*' or first == second:
                return True
            if first.startswith('file:') and second.startswith('file:'):
                if first.startswith(second.rstrip('/') + '/') or second.startswith(first.rstrip('/') + '/'):
                    return True
    return False


def resource_fence_conflicts(scope_id, resources, record):
    existing = record.get('resources') or ['*']
    if '*' in existing or '*' in resources:
        return True
    if record['scope_id'] == scope_id:
        return resources_intersect(resources, existing)
    return resources_intersect([key for key in resources if key.startswith('file:')],
                               [key for key in existing if key.startswith('file:')])


def operation_epoch(state):
    return digest([state.revision, getattr(state, 'access_epoch', None),
                   getattr(state, 'continuation_count', 0)])


def operation_identity(state, name, intent, idempotency_key=None):
    logical_key = idempotency_key or digest([operation_epoch(state), intent])[:24]
    return f'{state.scope_id}:{state.run_id}:{name}:{logical_key}'


def recovery_identity(record, state, op_id, receipt, reviewer):
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(receipt, dict):
        raise PermissionError('人工恢复必须提供核对人和身份回执')
    expected = dict(operation_id=op_id, scope_id=record['scope_id'], run_id=record['run_id'],
        intent_hash=record['intent_hash'], resources=record['resources'],
        owner_token=record.get('owner_token'), epoch=record.get('epoch'))
    if (state.scope_id != record['scope_id'] or state.run_id != record['run_id']
            or any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get('execution_stopped') is not True
            or not isinstance(receipt.get('evidence'), str) or not receipt['evidence'].strip()):
        raise PermissionError('人工恢复缺少退出证据或操作 ownership 身份不匹配')
    json.dumps(receipt)


def reconciliation_result(record, state, op_id, receipt, reviewer):
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(receipt, dict):
        raise PermissionError('人工核对必须提供核对人和明确回执')
    expected = dict(operation_id=op_id, scope_id=record['scope_id'], run_id=record['run_id'],
                    intent_hash=record['intent_hash'], resources=record['resources'])
    if (state.scope_id != record['scope_id'] or state.run_id != record['run_id']
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise PermissionError('人工核对的操作身份或资源不匹配')
    if (receipt.get('outcome') not in {'completed', 'not_applied'}
            or not isinstance(receipt.get('evidence'), str) or not receipt['evidence'].strip()
            or receipt.get('execution_stopped') is not True
            or receipt.get('result') is None):
        raise PermissionError('人工核对缺少结果、退出证据或明确执行结论')
    if receipt['outcome'] == 'not_applied' and (
            not isinstance(receipt['result'], dict) or receipt['result'].get('executed') is not False):
        raise PermissionError('未执行回执必须明确 executed=false')
    json.dumps(receipt)
    return receipt['result']


def make_operation_executor(store, state, *, notify=None):
    async def operation(name, intent, perform, *, idempotency_key=None, reconcile=None):
        execution_state = state.model_copy(deep=True)
        op_id = operation_identity(execution_state, name, intent, idempotency_key)
        if idempotency_key:
            intent = {**intent, 'idempotency_key': idempotency_key}
        owner = new_id('owner')
        ancestors = [token for store_identity, token in _active_owners.get() if store_identity == id(store)]
        try:
            receipt = store.begin(execution_state, op_id, intent, owner=owner, ancestors=ancestors, notify=notify)
        except Exception as error:
            if getattr(error, 'status', None) == 'UNKNOWN_OPERATION' and reconcile is not None:
                # 自动回调无法证明执行已停止；解除 fence 必须走带证据的显式人工核对。
                error.details['recovery'] = ('store.reconcile with reviewer, identity, resources, '
                                             'execution_stopped, evidence and result')
            raise
        if receipt is not None:
            return receipt
        context_token = _active_operation.set(lambda: store.operation_guard(execution_state, op_id, owner=owner))
        owners_token = _active_owners.set((*_active_owners.get(), (id(store), owner)))
        try:
            result = await perform()
            store.finish(execution_state, op_id, result, owner=owner, notify=notify)
            return result
        except BaseException as error:
            error.tracefix_operation_id = op_id
            if isinstance(getattr(error, 'details', None), dict):
                error.details['operation_id'] = op_id
            # perform/finish 失败或取消都不能证明副作用未发生，所有模式均保留跨 epoch fence。
            try:
                store.mark_unknown(execution_state, op_id, owner=owner, reason=type(error).__name__, notify=notify)
            except Exception as ledger_error:
                raise EffectBoundaryError('副作用回执或 UNKNOWN 持久化失败') from ledger_error
            raise
        finally:
            _active_operation.reset(context_token)
            _active_owners.reset(owners_token)
    return operation


def _prepare_local(payload):
    text, arguments = payload['content'], payload['arguments']
    tool = payload['tool']
    if tool == 'Write':
        updated = arguments['content']
    elif tool == 'Edit':
        count = text.count(arguments['old_string'])
        if not count or count != 1 and not arguments['replace_all']:
            raise ValueError('old_string 必须存在且唯一；多处替换需指定 replace_all')
        updated = text.replace(arguments['old_string'], arguments['new_string'],
                               -1 if arguments['replace_all'] else 1)
    elif tool == 'NotebookEdit':
        notebook = json.loads(text)
        cells, index, mode = notebook['cells'], arguments['cell_number'], arguments['edit_mode']
        if index > len(cells) or index == len(cells) and mode != 'insert':
            raise ValueError('cell_number 超出范围')
        if mode == 'delete':
            cells.pop(index)
        else:
            cell = dict(cell_type=arguments['cell_type'], metadata={},
                        source=arguments['new_source'].splitlines(keepends=True))
            if arguments['cell_type'] == 'code':
                cell.update(outputs=[], execution_count=None)
            if mode == 'insert':
                cell['id'] = payload['cell_id']
                cells.insert(index, cell)
            else:
                cell['metadata'] = cells[index].get('metadata', {})
                if 'id' in cells[index]:
                    cell['id'] = cells[index]['id']
                cells[index] = cell
        updated = json.dumps(notebook, ensure_ascii=False, indent=1) + chr(10)
    else:
        raise ValueError('未知的本地写请求')
    return dict(content=updated, before_hash=digest(text.encode('utf-8')))


def _effect_worker(request, folder):
    directory = Path(folder)
    if os.name != 'nt':
        os.setsid()
    (directory / 'ready').touch()
    while not (directory / 'start').exists():
        time.sleep(0.005)
    try:
        if request['kind'] == 'local.prepare':
            result = _prepare_local(request['payload'])
        elif request['kind'] == 'memory.note':
            from tracefix.knowledge.memory import MemoryLibrary
            payload = dict(request['payload'])
            library = MemoryLibrary(payload.pop('path'))
            result = library.note(**payload)
        else:
            raise ValueError('未知的可序列化副作用请求')
        receipt = dict(nonce=request['nonce'], result=result)
    except (ValueError, PermissionError) as error:
        receipt = dict(nonce=request['nonce'], rejected=str(error))
    except BaseException as error:
        receipt = dict(nonce=request['nonce'], unknown=type(error).__name__)
    temporary = directory / 'receipt.part'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, directory / 'receipt.json')


class _WindowsJob:
    def __init__(self, worker):
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t),
                        ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('reads', 'writes', 'others', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(ctypes.Structure):
            _fields_ = [('limits', Limits), ('io', Counters), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]

        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Extended()
        limits.limits.flags = 0x2000
        process_handle = self.api.OpenProcess(0x0101, False, worker.pid)
        try:
            if (not self.handle or not process_handle
                    or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                    or not self.api.AssignProcessToJobObject(self.handle, process_handle)):
                raise EffectBoundaryError('无法建立 Windows 可终止副作用边界')
        except BaseException:
            self.close()
            raise
        finally:
            if process_handle:
                self.api.CloseHandle(process_handle)

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise EffectBoundaryError('Windows Job 终止未确认')
        deadline = time.monotonic() + 1.5
        while True:
            accounting = (ctypes.c_uint64 * 4)()
            counters = (ctypes.c_uint32 * 4)()
            buffer = ctypes.create_string_buffer(ctypes.sizeof(accounting) + ctypes.sizeof(counters))
            if not self.api.QueryInformationJobObject(self.handle, 1, buffer, ctypes.sizeof(buffer), None):
                raise EffectBoundaryError('Windows Job 退出查询失败')
            active = ctypes.c_uint32.from_buffer(buffer, 40).value
            if active == 0:
                return
            if time.monotonic() >= deadline:
                raise EffectBoundaryError('Windows Job 仍存在活动后代进程')
            time.sleep(0.005)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _stop_worker(worker, job, group_ready):
    failure = None
    try:
        if job is not None:
            job.terminate()
        elif group_ready:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif worker.is_alive():
            worker.terminate()
    except Exception as error:
        failure = error
    worker.join(timeout=0.75)
    if worker.is_alive():
        worker.kill()
        worker.join(timeout=0.75)
    if job is not None:
        job.close()
    if worker.is_alive():
        raise EffectBoundaryError('副作用 worker 退出未确认') from failure
    worker.close()
    if failure is not None:
        raise EffectBoundaryError('副作用边界清理未确认') from failure


async def run_effect(kind, payload, *, timeout_s=45):
    request = json.loads(json.dumps(dict(kind=kind, payload=payload, nonce=new_id('effect'))))
    with tempfile.TemporaryDirectory(prefix='tracefix-r05-effect-') as folder:
        directory = Path(folder)
        worker = multiprocessing.get_context('spawn').Process(
            target=_effect_worker, args=(request, folder), name='tracefix-r05-effect', daemon=True)
        job, group_ready, started = None, False, False
        try:
            worker.start()
            started = True
            if os.name == 'nt':
                job = _WindowsJob(worker)
            async with asyncio.timeout(timeout_s):
                while not (directory / 'ready').exists():
                    if not worker.is_alive():
                        raise EffectBoundaryError('副作用 worker 未就绪即退出')
                    await asyncio.sleep(0.005)
                group_ready = os.name != 'nt'
                (directory / 'start').touch()
                while worker.is_alive():
                    await asyncio.sleep(0.005)
                worker.join(timeout=0.75)
                if worker.exitcode != 0 or not (directory / 'receipt.json').is_file():
                    raise EffectBoundaryError('副作用 worker 无完整回执')
                try:
                    receipt = json.loads((directory / 'receipt.json').read_text(encoding='utf-8'))
                except (OSError, ValueError) as error:
                    raise EffectBoundaryError('副作用 IPC 回执不完整') from error
                if not isinstance(receipt, dict) or receipt.get('nonce') != request['nonce']:
                    raise EffectBoundaryError('副作用回执身份不匹配')
                if 'unknown' in receipt:
                    raise EffectBoundaryError('副作用 worker 异常：' + receipt['unknown'])
                if 'rejected' in receipt:
                    from tracefix.tools.core import ToolRejected
                    raise ToolRejected(receipt['rejected'])
                if 'result' not in receipt:
                    raise EffectBoundaryError('副作用 IPC 回执缺少结果')
                return receipt['result']
        finally:
            if started:
                _stop_worker(worker, job, group_ready)
            else:
                worker.close()
