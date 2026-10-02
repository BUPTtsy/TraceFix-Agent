const phases: Record<string, string> = {
  STARTING: '启动', PREPARE: '准备', EXPLORE: '探索', REPRODUCE: '复现', DIAGNOSE: '诊断',
  PATCH: '修补', VERIFY: '验证', REVIEW: '审批', FINALIZE: '收尾',
};
const outcomes: Record<string, string> = {
  FIX_VERIFIED: '修复已通过本地验证', NO_BUG_FOUND: '当前测试范围内未发现问题',
  BUG_CONFIRMED: '已确认存在缺陷', LOOP_DETECTED: '检测到死循环',
  INCONCLUSIVE: '证据不足，无法确定', REPAIR_EXHAUSTED: '修复次数已用尽',
  POLICY_BLOCKED: '被策略阻止', INFRA_FAILURE: '运行环境故障',
};
const events: Record<string, string> = {
  'run.started': '运行开始', 'run.finished': '运行结束', 'run.error': '运行错误',
  'run.continued': '非成功结束后继续执行', 'run.continuation_requested': '用户请求继续原任务',
  'model.started': '模型开始', 'model.called': '模型调用完成', 'model.usage': '模型用量',
  'model.decision': '模型决策摘要', 'model.request.persisted': '模型输入已保存',
  'model.response.persisted': '模型输出已保存', 'model.error.persisted': '模型错误已保存',
  'state.changed': '状态切换', 'tool.started': '工具开始', 'tool.completed': '工具完成',
  'tool.error': '工具执行失败', 'knowledge.selected': '知识来源已选择', 'evidence.damaged': '证据损坏',
  'retrieval.degraded': '代码检索与修复记忆未启用',
  'gate.decided': '验证门禁结论', 'patch.applied': '补丁已应用', 'validation.failed': '验证失败',
  'loop.suspected': '疑似死循环', 'loop.detected': '检测到死循环',
  'input.applied': '补充输入已记录', 'subtask.completed': '子任务已完成',
  'worker.created': 'Worker 已创建', 'worker.queued': 'Worker 排队中',
  'worker.started': 'Worker 开始执行', 'worker.progress': 'Worker 执行进展',
  'worker.retrying': 'Worker 正在重试', 'worker.completed': 'Worker 已完成',
  'worker.partial': 'Worker 部分成功', 'worker.failed': 'Worker 执行失败',
  'worker.cancelled': 'Worker 已取消', 'worker.canceled': 'Worker 已取消',
  'worker.expired': 'Worker 已过期', 'worker.joined': 'Worker 结果已汇总',
  'worker.result.persisted': 'Worker 结果已保存',
  'workers.joined': 'Worker 结果已汇总', 'subtasks.joined': '子任务结果已汇总',
};
const services: Record<string, string> = {test: '测试', repair: '修复', chat: '对话', unknown: '模式未记录'};
const workerStatuses: Record<string, string> = {
  CREATED: '已创建', QUEUED: '排队中', RUNNING: '执行中', RETRYING: '重试中',
  SUCCEEDED: '已完成', PARTIAL: '部分成功', FAILED: '失败', CANCELLED: '已取消', EXPIRED: '已过期',
};
const workerRoles: Record<string, string> = {
  browser_operator: '浏览器操作者', browser: '浏览器操作者',
  code_explorer: '代码探索者', code_investigator: '代码探索者',
  evidence_reviewer: '证据审查员', test_planner: '测试规划者', verifier: '验证者',
};

export const phaseLabel = (phase: string) => phases[phase.toUpperCase()] || phase;
export const outcomeLabel = (outcome: string) => outcomes[outcome] || outcome;
export const eventLabel = (event: string) => events[event] || event;
export const serviceLabel = (service: string) => services[service] || service;
export const workerStatusLabel = (status: string) => workerStatuses[status.toUpperCase()] || status;
export const workerRoleLabel = (role: string) => workerRoles[role] || role;
