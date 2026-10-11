import http from 'node:http';
import {digest, version} from '../contract.mjs';

export async function completionFixture(mode = 'fixed') {
  let checked = false;
  const server = http.createServer(async (request, response) => {
    if (request.url === '/__reset') {checked = false; response.end('{}'); return;}
    if (request.url === '/api/task' && request.method === 'GET') {response.setHeader('content-type', 'application/json'); response.end(JSON.stringify({checked})); return;}
    if (request.url === '/api/task' && request.method === 'PATCH') {
      if (mode === 'fixed') checked = !checked;
      if (mode === 'regression') checked = true;
      response.setHeader('content-type', 'application/json'); response.end(JSON.stringify({checked})); return;
    }
    response.setHeader('content-type', 'text/html');
    response.end(`<h1>Task board</h1><input type="checkbox" aria-label="Complete Write project brief"><script>
      const checkbox = document.querySelector('input');
      async function read() {checkbox.checked = (await (await fetch('/api/task')).json()).checked;}
      checkbox.onchange = async () => {checkbox.disabled=true; await fetch('/api/task',{method:'PATCH'}); await read(); checkbox.disabled=false;};
      read();
    </script>`);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/`;
  const locator = {role: 'checkbox', name: 'Complete Write project brief'};
  const state = condition => ({locator, condition});
  const click = condition => ({action: {kind: 'click', locator}, assertions: [state('enabled'), state(condition)]});
  const reload = condition => ({action: {kind: 'navigate', value: url}, assertions: [state(condition)]});
  const scenario = (id, steps) => ({id, description: id, steps});
  const spec = {goal: '完成、取消及刷新持久化验收', authorized_actions: ['navigate', 'click', 'observe', 'finish'],
    executable_preconditions: [state('unchecked')], assertions: [state('checked')],
    regression_assertions: [state('unchecked')], regression_plan: [], behavior_scenarios: [
      scenario('original-complete', [click('checked')]),
      scenario('normal', [{action: {kind: 'observe'}, assertions: [state('unchecked')]}]),
      scenario('reverse', [click('checked'), click('unchecked')]),
      scenario('refresh', [click('checked'), reload('checked'), click('unchecked'), reload('unchecked')])],
  };
  return {server, url, input: {schema_version: version, url, spec,
    reset_path: '/__reset', isolation: {disposable: true, service_url: url}, include_regression: false,
    binding: {run_id: 'fixture-run', repo_id: 'fixture-repo', commit: 'fixture-revision', spec_hash: digest(spec)}},
    close: () => new Promise(resolve => server.close(resolve))};
}
