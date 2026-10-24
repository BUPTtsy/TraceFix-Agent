import Fastify from 'fastify';
import swagger from '@fastify/swagger';
import {timingSafeEqual, randomUUID} from 'node:crypto';
import path from 'node:path';
import * as schemas from './schemas.mjs';

export function controlAuthorized(request, token = process.env.TRACEFIX_CONTROL_TOKEN || '') {
  const supplied = request.headers.authorization?.match(/^Bearer\s+(.+)$/i)?.[1] || request.headers['x-tracefix-control-token'] || '';
  if (token) {
    const expected = Buffer.from(token), actual = Buffer.from(String(supplied));
    if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) return false;
  } else {
    if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(request.ip)) return false;
    try {if (!['localhost', '127.0.0.1', '[::1]'].includes(new URL('http://' + request.headers.host).hostname)) return false;}
    catch {return false;}
  }
  const origin = request.headers.origin;
  return !origin || origin === 'http://' + request.headers.host || origin === 'https://' + request.headers.host;
}

export function publicError(error, requestId) {
  const status = error.validation ? 400 : error.status ?? error.statusCode ?? (error.code === 'ENOENT' ? 404 : 500);
  return {status, body: {code: error.validation ? 'VALIDATION_ERROR' : error.code ||
    ({400: 'INVALID_REQUEST', 404: 'NOT_FOUND', 409: 'CONFLICT', 422: 'UNPROCESSABLE_REQUEST', 503: 'SERVICE_UNAVAILABLE'}[status] || 'INTERNAL_ERROR'),
    message: status >= 500 ? '服务暂不可用' : error.message || '请求失败',
    retryable: error.retryable === true, requestId,
    ...(error.taskId ? {taskId: error.taskId, taskUrl: '/api/v1/tasks/' + error.taskId} : {}),
    ...(error.runId ? {runId: error.runId} : {})}};
}

export async function createApi({bridge, controller, token, legacyHandler, logger = false}) {
  const app = Fastify({logger, genReqId: () => randomUUID(), bodyLimit: 1600000,
    ajv: {customOptions: {removeAdditional: false, coerceTypes: false, useDefaults: false}}});
  app.addSchema(schemas.findingAcceptance);
  await app.register(swagger, {openapi: {openapi: '3.1.0', info: {title: 'TraceFix beta control plane', version: '1.0.0'},
    components: {securitySchemes: {controlToken: {type: 'http', scheme: 'bearer'}}}, security: [{controlToken: []}]}});
  app.addHook('onRequest', async (request, reply) => {
    reply.header('cache-control', 'no-store').header('x-request-id', request.id);
    if ((request.url.startsWith('/api/') || request.url.startsWith('/__reset')) && !controlAuthorized(request, token))
      return reply.code(401).send({code: 'UNAUTHORIZED', message: '控制面需要有效凭据', retryable: false, requestId: request.id});
  });
  app.setErrorHandler((error, request, reply) => {
    const failure = publicError(error, request.id);
    if (failure.status >= 500) request.log.error({err: error}, '控制面请求失败');
    reply.code(failure.status).send(failure.body);
  });
  app.setNotFoundHandler((request, reply) => reply.code(404).send({code: 'NOT_FOUND', message: '接口不存在', retryable: false, requestId: request.id}));
  const route = (method, url, schema, handler, preValidation) => app.route({method, url, schema, handler, preValidation});
  const response = (status, success) => ({[status]: success, ...schemas.errors});
  const listQuery = {type: 'object', additionalProperties: false, properties: {projectId: {type: 'string', maxLength: 128}}};
  route('GET', '/api/v1/openapi.json', {hide: true}, async () => app.swagger());
  const repositoryResponse = status => response(status, schemas.genericObject);
  route('GET', '/api/v1/repositories', {response: response(200, {type: 'array', items: schemas.genericObject})},
    () => bridge('repositories'));
  route('POST', '/api/v1/repositories', {body: schemas.repositoryInput, response: repositoryResponse(201)},
    async (request, reply) => reply.code(201).send(await bridge('repository.create', request.body)));
  route('GET', '/api/v1/repositories/:id', {params: schemas.params, response: repositoryResponse(200)},
    request => bridge('repository', request.params));
  route('PATCH', '/api/v1/repositories/:id', {params: schemas.params, body: schemas.repositoryPatch, response: repositoryResponse(200)},
    request => bridge('repository.update', {id: request.params.id, patch: request.body}));
  route('DELETE', '/api/v1/repositories/:id', {params: schemas.params, response: repositoryResponse(200)},
    request => bridge('repository.delete', request.params));
  route('GET', '/api/v1/repositories/:id/rules', {params: schemas.params, response: repositoryResponse(200)},
    request => bridge('repository.rules', request.params));
  route('PUT', '/api/v1/repositories/:id/rules', {params: schemas.params, body: {type: 'object', additionalProperties: false,
    required: ['ruleIds', 'expectedVersion'], properties: {ruleIds: {type: 'array', uniqueItems: true, maxItems: 100, items: {type: 'string', minLength: 1, maxLength: 120}}, expectedVersion: {type: 'integer', minimum: 0}}}, response: repositoryResponse(200)},
    request => bridge('repository.rules.save', {...request.params, ...request.body}));
  route('POST', '/api/v1/tasks', {body: schemas.taskInput, headers: schemas.idempotency, response: response(201, schemas.mutation)}, async (request, reply) => {
    const created = await bridge('task.create', {body: request.body, key: request.headers['idempotency-key']});
    reply.header('location', '/api/v1/tasks/' + created.task.id);
    if (!request.body.create_and_start) return reply.code(201).send({...created, run: null});
    try {
      const started = await controller.start(created.task.id, {}, request.headers['idempotency-key'], 'create-start', created.task.version);
      return reply.code(201).send(started);
    } catch (error) {throw Object.assign(error, {taskId: created.task.id});}
  }, async request => {if (request.headers['idempotency-key']) await bridge('task.create.check', {
    body: request.body || {}, key: request.headers['idempotency-key']});});
  route('GET', '/api/v1/tasks', {querystring: listQuery, response: response(200, {type: 'array', items: schemas.task})},
    request => bridge('tasks', request.query));
  route('GET', '/api/v1/tasks/:id', {params: schemas.params, response: response(200, schemas.task)}, request => bridge('task', request.params));
  route('PUT', '/api/v1/tasks/:id', {params: schemas.params, body: schemas.taskInput, response: response(200, schemas.task)},
    request => bridge('task.update', {id: request.params.id, body: request.body}));
  route('POST', '/api/v1/tasks/:id/start', {params: schemas.params, body: schemas.empty, headers: schemas.idempotency, response: response(202, schemas.mutation)},
    async (request, reply) => reply.code(202).send(await controller.start(request.params.id, request.body || {}, request.headers['idempotency-key'])),
    async request => {if (request.headers['idempotency-key']) await bridge('task.start.check', {
      id: request.params.id, body: request.body || {}, key: request.headers['idempotency-key']});});
  route('POST', '/api/v1/tasks/:id/resume', {params: schemas.params, body: schemas.empty, headers: schemas.idempotency, response: response(202, schemas.mutation)},
    async (request, reply) => reply.code(202).send(await controller.resume(request.params.id, request.body || {}, request.headers['idempotency-key'])),
    async request => {if (request.headers['idempotency-key']) await bridge('task.resume.check', {
      id: request.params.id, body: request.body || {}, key: request.headers['idempotency-key']});});
  // 修复交付前人工审批：正文恰为 {approval_id, decision}；决定写入后由 --deliver-stdin 完成交付，幂等与 resume 一致。
  route('POST', '/api/v1/tasks/:id/approval', {params: schemas.params, body: schemas.approvalDecision, headers: schemas.idempotency, response: response(202, schemas.mutation)},
    async (request, reply) => reply.code(202).send(await controller.approve(request.params.id, request.body, request.headers['idempotency-key'])),
    async request => {if (request.headers['idempotency-key']) await bridge('task.approval.check', {
      id: request.params.id, body: request.body || {}, key: request.headers['idempotency-key']});});
  route('POST', '/api/v1/tasks/:id/stop', {params: schemas.params, body: schemas.empty,
    response: {...response(200, schemas.mutation), 202: schemas.mutation}},
    async (request, reply) => {const stopped = await controller.stop(request.params.id); return reply.code(stopped.status).send(stopped.value);});
  route('GET', '/api/v1/runs', {querystring: listQuery, response: response(200, {type: 'array', items: schemas.run})}, request => bridge('runs', request.query));
  for (const prefix of ['/api/v1/runs', '/api/runs']) {
    route('GET', prefix + '/:id', {params: schemas.params, response: response(200, prefix.includes('/v1/') ? schemas.run : schemas.genericObject)}, request => bridge('run', request.params));
    route('GET', prefix + '/:id/findings', {params: schemas.params, response: response(200, {type: 'array', items: schemas.genericObject})}, request => bridge('run.findings', request.params));
    route('GET', prefix + '/:id/result', {params: schemas.params, response: {...response(200, schemas.result), 202: schemas.result}}, async (request, reply) => {
      const result = await bridge('run.result', request.params);
      if (!result.ready) reply.code(202).header('retry-after', '1');
      return result;
    });
    route('GET', prefix + '/:id/events', {params: schemas.params,
      querystring: {type: 'object', additionalProperties: false, properties: {after: {type: 'string', pattern: '^\\d{1,10}$'}}},
      response: response(200, {type: 'array', items: schemas.genericObject})}, request => bridge('run.trace', {...request.params, ...request.query}));
    route('GET', prefix + '/:id/artifacts/:ref', {params: {type: 'object', additionalProperties: false, required: ['id', 'ref'], properties: {
      id: {type: 'string', maxLength: 200}, ref: {type: 'string', minLength: 1, maxLength: 200}}},
      response: response(200, {type: 'string', format: 'binary'})}, async (request, reply) => {
      const {id, ref} = request.params, artifact = await bridge('artifact', {id, ref});
      if (artifact.encoding !== 'base64') throw new Error('证据传输编码无效');
      const extension = path.extname(ref), inline = ['.html', '.png', '.json'].includes(extension);
      reply.type({'.html': 'text/html; charset=utf-8', '.png': 'image/png', '.json': 'application/json; charset=utf-8'}[extension] || 'text/plain; charset=utf-8')
        .header('content-disposition', `${inline ? 'inline' : 'attachment'}; filename*=UTF-8''${encodeURIComponent(ref)}`)
        .header('x-content-type-options', 'nosniff');
      if (extension === '.html') reply.header('content-security-policy', "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; sandbox");
      return reply.send(Buffer.from(artifact.content, 'base64'));
    });
  }
  registerRuleRoutes(route, bridge);
  if (legacyHandler) {
    const fallback = async (request, reply) => {
      request.raw.tracefixBody = request.body;
      request.raw.tracefixRequestId = request.id;
      reply.hijack();
      await legacyHandler(request.raw, reply.raw);
    };
    app.all('/*', {schema: {hide: true}}, fallback);
    app.all('/', {schema: {hide: true}}, fallback);
  }
  await app.ready();
  return app;
}

function registerRuleRoutes(route, bridge) {
  const anyResponse = status => ({[status]: schemas.genericObject, ...schemas.errors});
  const listResponse = {200: {type: 'array', items: schemas.genericObject}, ...schemas.errors};
  for (const prefix of ['/api/v1/rules', '/api/rules']) {
    route('GET', prefix, {querystring: schemas.ruleQuery, response: listResponse}, request => bridge('rules', {
      ...request.query, includeArchived: request.query.includeArchived === 'true'}));
    route('POST', prefix, {body: schemas.ruleInput, response: anyResponse(201)}, async (request, reply) => reply.code(201).send(await bridge('rule.create', request.body)));
    route('GET', prefix + '/insights', {response: listResponse}, request => bridge('rule.insights', request.query));
    route('GET', prefix + '/:id', {params: schemas.params, response: anyResponse(200)}, request => bridge('rule', request.params));
    route('PUT', prefix + '/:id', {params: schemas.params, body: schemas.ruleInput, response: anyResponse(200)}, request => bridge('rule.update', {...request.body, id: request.params.id}));
    route('DELETE', prefix + '/:id', {params: schemas.params, response: anyResponse(200)}, request => bridge('rule.delete', request.params));
    for (const suffix of ['versions', 'preview', 'findings']) {
      route('GET', prefix + '/:id/' + suffix, {params: schemas.params, response: suffix === 'preview' ? anyResponse(200) : listResponse},
        request => bridge('rule.' + suffix, {...request.query, id: request.params.id, ruleId: request.params.id}));
    }
    for (const [action, status] of [['publish', 'enabled'], ['disable', 'disabled'], ['archive', 'archived'], ['rollback', null]]) {
      route('POST', prefix + '/:id/' + action, {params: schemas.params, body: action === 'rollback' ? {...schemas.ruleAction, required: ['version']} : schemas.ruleAction, response: anyResponse(200)},
        request => bridge(status ? 'rule.status' : 'rule.rollback', {id: request.params.id, status,
          expectedVersion: request.body.expectedVersion, version: request.body.version}));
    }
  }
  for (const prefix of ['/api/v1/rule-sets', '/api/rule-sets']) {
    route('GET', prefix, {response: listResponse}, () => bridge('rule-sets'));
    route('POST', prefix, {body: schemas.genericObject, response: anyResponse(201)}, async (request, reply) => reply.code(201).send(await bridge('rule-set.save', request.body)));
  }
}
