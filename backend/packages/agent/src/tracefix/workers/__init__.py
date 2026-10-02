"""Dynamic supervisor/worker package."""

from tracefix.workers.contracts import (
    WorkerEvent,
    WorkerEventType,
    WorkerResult,
    WorkerSnapshot,
    WorkerStatus,
    WorkerTask,
    WorkerTool,
)
from tracefix.workers.locks import LockedWorkspace, WorkspaceMutex
from tracefix.workers.prompt_policy import (
    SUPERVISOR_DELEGATION_POLICY,
    WORKER_ACTION_POLICY,
    build_supervisor_prompt,
    build_worker_prompt,
    build_worker_system_prompt,
)
from tracefix.workers.scheduler import (
    WorkerCancelled,
    WorkerContext,
    WorkerDelegationError,
    WorkerExecutionError,
    WorkerHandle,
    WorkerScheduler,
)
from tracefix.workers.tools import supervisor_tools
from tracefix.workers.runtime import (
    ROLE_PHASES, ROLE_TOOLS, BrowserSandbox, BrowserSandboxFactory,
    HierarchyTrace, IsolatedGuiScout, SubAgentRejected, SubAgentRuntime, SubAgentTrace,
)

__all__ = [
    'WorkerEvent', 'WorkerEventType', 'WorkerResult', 'WorkerSnapshot', 'WorkerStatus', 'WorkerTask', 'WorkerTool',
    'WorkerCancelled', 'WorkerContext', 'WorkerDelegationError', 'WorkerExecutionError',
    'WorkerHandle', 'WorkerScheduler', 'WorkspaceMutex', 'LockedWorkspace',
    'SUPERVISOR_DELEGATION_POLICY', 'WORKER_ACTION_POLICY', 'build_supervisor_prompt',
    'build_worker_prompt', 'build_worker_system_prompt',
    'supervisor_tools',
    'ROLE_PHASES', 'ROLE_TOOLS', 'BrowserSandbox', 'BrowserSandboxFactory',
    'HierarchyTrace', 'IsolatedGuiScout', 'SubAgentRejected', 'SubAgentRuntime', 'SubAgentTrace',
]
