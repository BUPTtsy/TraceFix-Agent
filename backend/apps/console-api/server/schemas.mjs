import strictFindingAcceptance from '../../../packages/agent/src/tracefix/service/finding_acceptance.schema.json' with {type: 'json'};

export const findingAcceptance = {...strictFindingAcceptance, $id: 'tracefix.finding-acceptance-template/1',
  required: strictFindingAcceptance.required.filter(key => key !== 'run_id')};

const text = {type: 'string', minLength: 1, maxLength: 200};
const object = {type: 'object', additionalProperties: true};
const nullableObject = {anyOf: [object, {type: 'null'}]};
const ids = {type: 'array', maxItems: 100, uniqueItems: true, items: text};
// 交付审批：approval 为待审批/已决定记录（含 id、status、decision 等），publication 为发布结果，delivery_diff 为待交付累计差异。
const deliveryMetadata = {can_approve: {type: 'boolean'}, approval: nullableObject};
const harnessMetadata = {current_run_id: {type: ['string', 'null']}, accepted_commit: {type: ['string', 'null']},
  quality: {type: ['string', 'null']}, owner_epoch: {type: ['integer', 'null'], minimum: 0},
  harness_revision: {type: ['integer', 'null'], minimum: 0}};
const locator = {type: 'object', additionalProperties: false, required: ['role', 'name'], properties: {
  role: {type: 'string', maxLength: 200}, name: {type: 'string', maxLength: 2000}}};
const assertion = {type: 'object', additionalProperties: false, required: ['locator'], properties: {
  locator, condition: {type: 'string', enum: ['visible', 'absent', 'checked', 'unchecked', 'disabled', 'enabled']}}};
const assertions = {type: 'array', maxItems: 100, items: assertion};
const action = {type: 'object', additionalProperties: false, required: ['kind'], properties: {
  kind: {type: 'string', enum: ['navigate', 'click', 'type', 'select', 'press', 'observe', 'finish']},
  observation_id: {type: 'null'}, element_ref: {type: 'null'}, page_generation: {type: 'null'},
  locator: {...locator, type: ['object', 'null']}, value: {type: ['string', 'null'], maxLength: 8000},
  preconditions: assertions, postconditions: assertions,
  wait: {type: ['object', 'null'], additionalProperties: false, properties: {assertions,
    timeout_seconds: {type: 'number', exclusiveMinimum: 0, maximum: 30}, interval_seconds: {type: 'number', exclusiveMinimum: 0, maximum: 2},
    max_observations: {type: 'integer', minimum: 1, maximum: 30}}}}};
export const testSpec = {type: 'object', additionalProperties: false, required: ['goal'], allOf: [{
  if: {required: ['mode'], properties: {mode: {enum: ['unit', 'static', 'build']}}},
  then: {properties: {assertions: {maxItems: 0}, regression_assertions: {maxItems: 0},
    regression_plan: {maxItems: 0}, executable_preconditions: {maxItems: 0},
    behavior_scenarios: {maxItems: 0}, authorized_actions: {maxItems: 0}}},
  else: {required: ['assertions', 'regression_assertions'], properties: {
    assertions: {minItems: 1}, regression_assertions: {minItems: 1}}}}], properties: {
  mode: {type: 'string', enum: ['gui', 'unit', 'static', 'build']},
  goal: {type: 'string', minLength: 5, maxLength: 4000}, preconditions: {type: 'array', maxItems: 100, items: {type: 'string', maxLength: 2000}},
  executable_preconditions: assertions,
  authorized_actions: {type: 'array', uniqueItems: true, maxItems: 7, items: action.properties.kind},
  assertions, regression_assertions: assertions,
  regression_plan: {type: 'array', maxItems: 100, items: action},
  behavior_scenarios: {type: 'array', maxItems: 50, items: {type: 'object', additionalProperties: false, required: ['id', 'steps'], properties: {
    id: text, description: {type: 'string', maxLength: 4000}, steps: {type: 'array', minItems: 1, maxItems: 100,
      items: {type: 'object', additionalProperties: false, required: ['action'], properties: {action, assertions}}}}}},
  guidance_ack: {type: 'array', maxItems: 100, items: {type: 'object', additionalProperties: false, required: ['id', 'how_applied'], properties: {
    id: text, how_applied: {type: 'string', minLength: 1, maxLength: 2000}}}}}};
const githubRead = {type: 'object', additionalProperties: false, required: ['source_kind'], properties: {
  source_kind: {enum: ['commit', 'file', 'issue', 'pull_request'], type: 'string'},
  path: {type: 'string', minLength: 1, maxLength: 1024}, number: {type: 'integer', minimum: 1}},
  oneOf: [{properties: {source_kind: {const: 'file'}}, required: ['path'], not: {required: ['number']}},
    {properties: {source_kind: {enum: ['issue', 'pull_request']}}, required: ['number'], not: {required: ['path']}},
    {properties: {source_kind: {enum: ['commit']}}, not: {anyOf: [{required: ['path']}, {required: ['number']}]}}]};

export const taskInput = {type: 'object', additionalProperties: false, required: ['mode', 'goal', 'execution'],
  anyOf: [{required: ['projectId']}, {required: ['repo_id']}, {required: ['repositoryId']}],
  allOf: [
    {if: {required: ['mode'], properties: {mode: {const: 'test'}}}, then: {
      not: {anyOf: [{required: ['source_run_id']}, {required: ['finding_acceptance']}]},
      properties: {source_finding_ids: {type: 'array', maxItems: 0}}}},
    {if: {required: ['source_run_id']}, then: {required: ['source_finding_ids'],
      properties: {mode: {const: 'repair'}, source_finding_ids: {type: 'array', minItems: 1}}}},
    {if: {required: ['source_finding_ids'], properties: {source_finding_ids: {type: 'array', minItems: 1}}},
      then: {required: ['source_run_id'], properties: {mode: {const: 'repair'}}}},
    {if: {required: ['finding_acceptance']}, then: {required: ['source_run_id', 'source_finding_ids'],
      properties: {mode: {const: 'repair'}, source_finding_ids: {type: 'array', minItems: 1}}}}], properties: {
    projectId: {...text, maxLength: 128}, repo_id: {...text, maxLength: 128}, repositoryId: {...text, maxLength: 128},
    mode: {type: 'string', enum: ['test', 'repair']}, goal: {type: 'string', minLength: 5, maxLength: 4000},
    execution: {type: 'object', additionalProperties: false, required: ['executionMode'], properties: {
      executionMode: {type: 'string', enum: ['batch']}, backend: {type: 'string', enum: ['claude_sdk']},
      coding_backend: {type: 'string', enum: ['claude_sdk']},
      url: {type: 'string', format: 'uri', maxLength: 2048},
      spec: testSpec}},
    additionalRuleIds: {...ids, maxItems: 50}, rule_ids: {...ids, maxItems: 50}, source_run_id: text,
    source_finding_ids: ids, github_reads: {type: 'array', maxItems: 50, items: githubRead},
    finding_acceptance: {$ref: findingAcceptance.$id},
    create_and_start: {type: 'boolean'}}};
export const task = {type: 'object', additionalProperties: false,
  required: ['id', 'definition', 'status', 'version', 'currentRunId', 'runIds', 'createdAt', 'updatedAt'], properties: {
    id: text, definition: object, status: {type: 'string'}, version: {type: 'integer', minimum: 1},
    currentRunId: {type: ['string', 'null']}, runIds: {type: 'array', items: text},
    createdAt: {type: 'string', format: 'date-time'}, updatedAt: {type: 'string', format: 'date-time'},
    ...harnessMetadata, ...deliveryMetadata, coding_backend: {type: 'string'}, control_status: {type: 'string'}, can_stop: {type: 'boolean'}, can_resume: {type: 'boolean'},
    resume_blockers: {type: 'array', items: {type: 'string'}}, harness_job_id: {type: ['string', 'null']}, harness_session_id: {type: ['string', 'null']}}};
export const run = {type: 'object', additionalProperties: false, required: ['id', 'projectId', 'status'], properties: {
  id: text, taskId: {type: ['string', 'null']}, projectId: text, agentRunId: {type: ['string', 'null']},
  mode: {type: 'string'}, goal: {type: 'string'}, status: {type: 'string'}, phase: {type: ['string', 'null']},
  outcome: {type: ['string', 'null']}, startedAt: {type: 'string'}, updatedAt: {type: 'string'},
  finishedAt: {type: ['string', 'null']}, processEndedAt: {type: ['string', 'null']},
  error: {anyOf: [object, {type: 'string'}, {type: 'null'}]}, artifacts: {type: 'array', items: object},
  ...harnessMetadata, ...deliveryMetadata, publication: nullableObject, delivery_diff: {type: ['string', 'null']},
  coding_backend: {type: 'string'}, control_status: {type: 'string'}, can_stop: {type: 'boolean'}, can_resume: {type: 'boolean'},
  resume_blockers: {type: 'array', items: {type: 'string'}}, harness_job_id: {type: ['string', 'null']}, harness_session_id: {type: ['string', 'null']}}};
export const error = {type: 'object', additionalProperties: false, required: ['code', 'message', 'retryable', 'requestId'], properties: {
  code: text, message: {type: 'string'}, retryable: {type: 'boolean'}, requestId: text,
  taskId: text, taskUrl: {type: 'string'}, runId: text}};
export const errors = Object.fromEntries([400, 401, 403, 404, 409, 413, 415, 422, 500, 503].map(status => [status, error]));
export const mutation = {type: 'object', additionalProperties: false, required: ['task', 'run', 'replayed'], properties: {
  task, run: {...run, type: ['object', 'null']}, replayed: {type: 'boolean'}}};
export const result = {type: 'object', additionalProperties: false,
  required: ['runId', 'taskId', 'outcome', 'settled', 'ready', 'ruleCoverage', 'findings', 'patch', 'verification', 'error', 'provenance'], properties: {
    runId: text, taskId: {type: ['string', 'null']}, outcome: {type: ['string', 'null']}, settled: {type: 'boolean'}, ready: {type: 'boolean'},
    ruleCoverage: object, findings: {type: 'array', items: object}, patch: nullableObject, verification: nullableObject,
    summary: {type: ['string', 'null']}, branch: {type: ['string', 'null']}, commit: {type: ['string', 'null']}, publication: nullableObject,
    coding_backend: {type: 'string'}, control_status: {type: 'string'},
    can_stop: {type: 'boolean'}, can_resume: {type: 'boolean'}, resume_blockers: {type: 'array', items: {type: 'string'}},
    ...harnessMetadata, ...deliveryMetadata, harness_job_id: {type: ['string', 'null']}, harness_session_id: {type: ['string', 'null']},
    error: nullableObject, provenance: object}};
const commandArgv = {type: 'array', minItems: 1, maxItems: 200, items: {type: 'string', minLength: 1, maxLength: 8000}};
const repositoryProfile = {type: 'object', additionalProperties: false, required: ['commands'], properties: {
  project: {type: 'string', maxLength: 64}, source_commit: {type: 'string', maxLength: 64},
  url: {type: 'string', format: 'uri', maxLength: 2048},
  allowed_origins: {type: 'array', minItems: 1, maxItems: 100, items: {type: 'string', maxLength: 2048}},
  commands: {type: 'object', required: ['start', 'reset', 'static', 'unit', 'build'], additionalProperties: commandArgv},
  image: text, browser_image: text, health_path: text, version_path: text,
  port: {type: 'integer', minimum: 1, maximum: 65535}, timeout_seconds: {type: 'integer', minimum: 1, maximum: 600},
  screenshot_redaction: text}};
const repositoryFields = {
  repo_id: {type: 'string', pattern: '^[A-Za-z0-9_-]{1,64}$'},
  projectId: {type: 'string', pattern: '^[A-Za-z0-9_-]{1,64}$'},
  id: {type: 'string', pattern: '^[A-Za-z0-9_-]{1,64}$'},
  root: {type: 'string', minLength: 1, maxLength: 4096},
  owner: {type: 'string', pattern: '^[A-Za-z0-9_.-]{1,100}$'},
  repo: {type: 'string', pattern: '^[A-Za-z0-9_.-]{1,100}$'},
  default_ref: {type: 'string', minLength: 1, maxLength: 255},
  checkout_mode: {type: 'string', enum: ['existing', 'managed']}, publish_branch: {type: 'boolean'},
  require_approval: {type: 'boolean'},
  protected_paths: {type: 'array', maxItems: 200, uniqueItems: true, items: {type: 'string', minLength: 1, maxLength: 1024}},
  profile: repositoryProfile, profile_ref: {type: ['string', 'null'], minLength: 1, maxLength: 4096},
  allowed_files: {type: 'array', minItems: 1, maxItems: 200, uniqueItems: true, items: {type: 'string', minLength: 1, maxLength: 1024}},
  allowed_urls: {type: 'array', minItems: 1, maxItems: 100, uniqueItems: true, items: {type: 'string', minLength: 1, maxLength: 2048}},
  credential_ref: {type: ['string', 'null'], pattern: '^[A-Za-z0-9_.:/-]{1,200}$'}, enabled: {type: 'boolean'}};
export const repositoryInput = {type: 'object', additionalProperties: false,
  required: ['owner', 'repo', 'default_ref', 'allowed_files', 'allowed_urls'], properties: repositoryFields,
  allOf: [
    {anyOf: [{required: ['repo_id']}, {required: ['projectId']}, {required: ['id']}]},
    {anyOf: [{required: ['profile']}, {required: ['profile_ref']}]},
    {if: {required: ['checkout_mode'], properties: {checkout_mode: {const: 'managed'}}}, then: {}, else: {required: ['root']}}]};
export const repositoryPatch = {type: 'object', additionalProperties: false, minProperties: 1, properties: repositoryFields};
export const empty = {type: 'object', additionalProperties: false, properties: {}};
export const approvalDecision = {type: 'object', additionalProperties: false, required: ['approval_id', 'decision'], properties: {
  approval_id: text, decision: {type: 'string', enum: ['approve', 'reject']}}};
export const params = {type: 'object', additionalProperties: false, required: ['id'], properties: {id: text}};
export const idempotency = {type: 'object', properties: {'idempotency-key': {type: 'string', minLength: 1, maxLength: 200, pattern: '^[!-~]+$'}}};
export const genericObject = object;
export const ruleInput = {type: 'object', additionalProperties: false, required: ['name', 'detection'], properties: {
  id: {type: 'string', pattern: '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$'},
  name: {type: 'string', minLength: 1, maxLength: 160}, version: {type: 'integer', minimum: 1},
  expectedVersion: {type: 'integer', minimum: 1}, expected_version: {type: 'integer', minimum: 1},
  status: {type: 'string', enum: ['draft', 'enabled', 'disabled', 'archived']},
  category: {type: 'string', enum: ['functional', 'a11y', 'console', 'network', 'visual', 'performance', 'security', 'i18n', 'code-pattern']},
  severity: {type: 'string', enum: ['blocker', 'critical', 'major', 'minor']}, priority: {type: 'integer', minimum: 1, maximum: 100}, pinned: {type: 'boolean'},
  scope: {type: 'object', additionalProperties: false, properties: {level: {type: 'string', enum: ['org', 'project', 'job', 'run']},
    ...Object.fromEntries(['project_ids', 'job_ids', 'run_ids', 'url_patterns', 'path_globs', 'frameworks'].map(key =>
      [key, {type: 'array', maxItems: key === 'frameworks' ? 30 : 100, items: {type: 'string'}}]))}},
  phases: {type: 'array', maxItems: 10, items: {type: 'string', minLength: 1}},
  detection: {type: 'object', additionalProperties: false, required: ['type'], properties: {
    type: {type: 'string', enum: ['oracle', 'static', 'guided']}, oracle: nullableObject, static: nullableObject, guided: nullableObject},
    allOf: [
      {if: {properties: {type: {const: 'oracle'}}}, then: {required: ['oracle'], properties: {oracle: {
        type: 'object', minProperties: 1, required: ['kind'], properties: {kind: {type: 'string', enum: [
          'console_no_error', 'network_status', 'dom_assertion', 'dom_after_action', 'a11y_axe', 'visual_threshold']}}, additionalProperties: true}}}},
      ...['static', 'guided'].map(kind => ({if: {properties: {type: {const: kind}}}, then: {
        required: [kind], properties: {[kind]: {type: 'object', minProperties: 1, additionalProperties: true}}}}))]},
  fix_guidance: {type: 'string', maxLength: 4096}, examples: {type: 'object', additionalProperties: {type: 'string'}},
  tags: {type: 'array', maxItems: 30, items: {type: 'string', minLength: 1}}, owner: {type: 'string', maxLength: 160},
  author: {type: 'string'}, org_id: {type: 'string'}, changeNote: {type: 'string'}, change_note: {type: 'string'},
  created_at: {type: ['string', 'null']}, updated_at: {type: ['string', 'null']}}};
export const ruleQuery = {type: 'object', additionalProperties: false, properties: {
  projectId: {type: 'string', maxLength: 128}, status: {type: 'string', enum: ['draft', 'enabled', 'disabled', 'archived']},
  q: {type: 'string', maxLength: 500}, includeArchived: {type: 'string', enum: ['true', 'false']}}};
export const ruleAction = {type: 'object', additionalProperties: false, properties: {
  expectedVersion: {type: 'integer', minimum: 1}, version: {type: 'integer', minimum: 1}}};
