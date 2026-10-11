const text = {type: 'string', minLength: 1, maxLength: 200};
const object = {type: 'object', additionalProperties: true};
const nullableObject = {anyOf: [object, {type: 'null'}]};
const ids = {type: 'array', maxItems: 100, uniqueItems: true, items: text};
const githubRead = {type: 'object', additionalProperties: false, required: ['source_kind'], properties: {
  source_kind: {enum: ['metadata', 'commit', 'file', 'issue', 'pull_request'], type: 'string'},
  path: {type: 'string', minLength: 1, maxLength: 1024}, number: {type: 'integer', minimum: 1}},
  oneOf: [{properties: {source_kind: {const: 'file'}}, required: ['path'], not: {required: ['number']}},
    {properties: {source_kind: {enum: ['issue', 'pull_request']}}, required: ['number'], not: {required: ['path']}},
    {properties: {source_kind: {enum: ['metadata', 'commit']}}, not: {anyOf: [{required: ['path']}, {required: ['number']}]}}]};

export const taskInput = {type: 'object', additionalProperties: false, required: ['mode', 'goal', 'execution'],
  anyOf: [{required: ['projectId']}, {required: ['repo_id']}, {required: ['repositoryId']}], properties: {
    projectId: {...text, maxLength: 128}, repo_id: {...text, maxLength: 128}, repositoryId: {...text, maxLength: 128},
    mode: {type: 'string', enum: ['test', 'repair']}, goal: {type: 'string', minLength: 1, maxLength: 4000},
    execution: {type: 'object', additionalProperties: false, required: ['executionMode'], properties: {
      executionMode: {type: 'string', enum: ['batch']}, url: {type: 'string', format: 'uri', maxLength: 2048},
      spec: {type: 'string', minLength: 1, maxLength: 32000}}},
    additionalRuleIds: {...ids, maxItems: 50}, rule_ids: {...ids, maxItems: 50}, source_run_id: text,
    source_finding_ids: ids, github_reads: {type: 'array', maxItems: 50, items: githubRead},
    finding_acceptance: {...object, description: 'Reserved versioned binding; rejected with 422 until C integrates its strict schema.'},
    create_and_start: {type: 'boolean'}}};
export const task = {type: 'object', additionalProperties: false,
  required: ['id', 'definition', 'status', 'version', 'currentRunId', 'runIds', 'createdAt', 'updatedAt'], properties: {
    id: text, definition: object, status: {type: 'string'}, version: {type: 'integer', minimum: 1},
    currentRunId: {type: ['string', 'null']}, runIds: {type: 'array', items: text},
    createdAt: {type: 'string', format: 'date-time'}, updatedAt: {type: 'string', format: 'date-time'}}};
export const run = {type: 'object', additionalProperties: false, required: ['id', 'projectId', 'status'], properties: {
  id: text, taskId: {type: ['string', 'null']}, projectId: text, agentRunId: {type: ['string', 'null']},
  mode: {type: 'string'}, goal: {type: 'string'}, status: {type: 'string'}, phase: {type: ['string', 'null']},
  outcome: {type: ['string', 'null']}, startedAt: {type: 'string'}, updatedAt: {type: 'string'},
  finishedAt: {type: ['string', 'null']}, processEndedAt: {type: ['string', 'null']},
  error: {anyOf: [object, {type: 'string'}, {type: 'null'}]}, artifacts: {type: 'array', items: object}}};
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
    error: nullableObject, provenance: object}};
export const empty = {type: 'object', additionalProperties: false, properties: {}};
export const params = {type: 'object', additionalProperties: false, required: ['id'], properties: {id: text}};
export const idempotency = {type: 'object', properties: {'idempotency-key': {type: 'string', minLength: 1, maxLength: 200, pattern: '^[!-~]+$'}}};
export const genericObject = object;
