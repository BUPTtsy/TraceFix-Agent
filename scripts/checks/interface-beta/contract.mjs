import {createHash} from 'node:crypto';

export const version = 'tracefix.playwright-acceptance/1';
const actions = new Set(['navigate', 'click', 'type', 'select', 'press', 'observe']);
const conditions = new Set(['visible', 'absent', 'checked', 'unchecked', 'disabled', 'enabled']);
const checkable = new Set(['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio']);

export function digest(value) {
  const canonical = item => Array.isArray(item) ? item.map(canonical) : item && typeof item === 'object'
    ? Object.fromEntries(Object.keys(item).sort().map(key => [key, canonical(item[key])])) : item;
  return createHash('sha256').update(JSON.stringify(canonical(value))).digest('hex');
}

function boundDigest(value, canonical) {
  if (canonical === undefined) return digest(value);
  if (typeof canonical !== 'string' || digest(JSON.parse(canonical)) !== digest(value))
    throw new Error('冻结 canonical JSON 与执行规范不一致');
  return createHash('sha256').update(canonical).digest('hex');
}

export function serviceUrl(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
    throw new Error('服务 URL 必须是无凭据 HTTP URL');
  return url;
}

export function requireIsolation(input) {
  const url = serviceUrl(input.url);
  if (input.isolation?.disposable !== true || input.isolation?.service_url !== url.href)
    throw new Error('必须绑定 disposable 隔离测试服务');
  return url;
}

function locator(value) {
  if (!value || typeof value.role !== 'string' || !value.role || typeof value.name !== 'string'
      || Object.keys(value).some(key => !['role', 'name'].includes(key)))
    throw new Error('定位器只能包含精确 role/name');
}

function assertion(value) {
  locator(value?.locator);
  if (!conditions.has(value.condition || 'visible') || Object.keys(value).some(key => !['locator', 'condition'].includes(key)))
    throw new Error('不支持的断言');
  if (['checked', 'unchecked'].includes(value.condition) && !checkable.has(value.locator.role))
    throw new Error('checked/unchecked 断言要求可勾选角色');
}

function assertions(values) {
  if (!Array.isArray(values) || values.length > 100) throw new Error('断言数量无效');
  values.forEach(assertion);
}

export function validateAction(value, input) {
  const allowed = ['kind', 'locator', 'value', 'observation_id', 'element_ref', 'page_generation',
    'preconditions', 'postconditions', 'wait'];
  if (!value || Object.keys(value).some(key => !allowed.includes(key)) || !actions.has(value.kind)
      || !input.spec.authorized_actions.includes(value.kind)) throw new Error('未授权或不支持的动作');
  if (value.observation_id != null || value.element_ref != null || value.page_generation != null)
    throw new Error('冻结动作不能引用旧页面元素');
  if (['click', 'type', 'select'].includes(value.kind)) locator(value.locator);
  if (value.locator) locator(value.locator);
  if (['navigate', 'type', 'select', 'press'].includes(value.kind) && typeof value.value !== 'string')
    throw new Error('动作需要字符串 value');
  if (value.kind === 'navigate' && serviceUrl(new URL(value.value, input.url).href).origin !== serviceUrl(input.url).origin)
    throw new Error('导航超出冻结服务 origin');
  assertions(value.preconditions || []);
  assertions(value.postconditions || []);
  if (value.wait) {
    if (Object.keys(value.wait).some(key => !['assertions', 'timeout_seconds', 'interval_seconds', 'max_observations'].includes(key)))
      throw new Error('等待包含不支持的字段');
    assertions(value.wait.assertions || []);
    if (value.wait.timeout_seconds != null && (!(value.wait.timeout_seconds > 0) || value.wait.timeout_seconds > 30))
      throw new Error('等待超时超过限制');
  }
}

export function compileCases(input) {
  requireIsolation(input);
  if (input.schema_version !== version || !input.spec || !Array.isArray(input.spec.authorized_actions))
    throw new Error('验收请求版本或 TestSpec 无效');
  if (boundDigest(input.spec, input.spec_canonical) !== input.binding?.spec_hash) throw new Error('冻结 TestSpec hash 不匹配');
  if (!input.binding.run_id || !input.binding.repo_id || !input.binding.commit)
    throw new Error('缺少 Run/repo/revision 绑定');
  if (!Array.isArray(input.spec.behavior_scenarios) || input.spec.behavior_scenarios.length > 50)
    throw new Error('业务场景数量无效');
  assertions(input.spec.executable_preconditions || []);
  assertions(input.spec.assertions);
  assertions(input.spec.regression_assertions);
  const cases = [];
  if (input.original_plan) cases.push({id: 'original', kind: 'original',
    steps: input.original_plan.map(action => ({action, assertions: []})), final: input.spec.assertions});
  if (input.include_regression !== false) cases.push({id: 'regression', kind: 'regression',
    steps: (input.spec.regression_plan || []).map(action => ({action, assertions: []})), final: input.spec.regression_assertions});
  for (const scenario of input.spec.behavior_scenarios) {
    if (typeof scenario.id !== 'string' || !scenario.id || !Array.isArray(scenario.steps) || !scenario.steps.length
        || !scenario.steps.at(-1).assertions?.length
        || scenario.steps.length > 100 || scenario.steps.length > 1 && !scenario.steps.slice(0, -1).some(step => step.assertions?.length)
        || Object.keys(scenario).some(key => !['id', 'description', 'steps'].includes(key)))
      throw new Error('业务场景缺少完整冻结检查点');
    cases.push({...scenario, kind: 'behavior', scenario_hash: boundDigest(scenario, input.scenario_canonical?.[scenario.id])});
  }
  if (!cases.length || cases.length > 52 || new Set(cases.map(item => item.id)).size !== cases.length)
    throw new Error('场景为空、过多或 id 重复');
  for (const item of cases) {
    item.scenario_hash ||= digest({id: item.id, steps: item.steps, final: item.final});
    for (const step of item.steps) {
      if (Object.keys(step).some(key => !['action', 'assertions'].includes(key))) throw new Error('步骤字段无效');
      validateAction(step.action, input);
      assertions(step.assertions || []);
    }
  }
  if (input.reset_path && new URL(input.reset_path, input.url).origin !== serviceUrl(input.url).origin)
    throw new Error('reset 超出隔离服务 origin');
  if (input.case_ids !== undefined) {
    if (!Array.isArray(input.case_ids) || !input.case_ids.length
        || input.case_ids.some(id => !cases.some(item => item.id === id))) throw new Error('受控场景选择无效');
    return cases.filter(item => input.case_ids.includes(item.id));
  }
  return cases;
}

export function aggregate(cases) {
  if (cases.some(item => item.status === 'fail')) return 'fail';
  return cases.length && cases.every(item => item.status === 'pass') ? 'pass' : 'inconclusive';
}

export function oracleResult(result, caseId) {
  if (!['pass', 'fail'].includes(result.status)) throw new Error('不确定结果不能生成 Oracle 二值评分');
  return {case_id: caseId, passed: result.status === 'pass', error: result.error || null,
    steps: result.cases.map(item => ({name: item.id, passed: item.status === 'pass', error: item.error || null})),
    oracle: version, final_scoring_only: true};
}
