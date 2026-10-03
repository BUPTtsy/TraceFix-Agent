"""Fake adapters ONLY for CI smoke. Results are not real GUI or model evaluation."""
import base64
import dataclasses
import json
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from tracefix.config import Profile, Project
from tracefix.execution.policy import Policy
from tracefix.execution.workspace import Workspace, commit_workspace, git
from tracefix.execution.repository import safe_index_files
from tracefix.knowledge.retrieval import Retriever
from tracefix.knowledge.scope import ScopeResolver
from tracefix.model.gateway import ModelResult
from tracefix.runtime.contracts import (Assertion, BrowserAction, Decision, FileEdit, Locator,
    PatchProposal, ReproductionPlan, RunState, TestSpec, digest, new_id)
from tracefix.runtime.engine import Engine
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.store import MemoryStore

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class FakeRunner:
    def __init__(self, source, fail=None):
        self.source, self.closed, self.fail = source, False, fail
    async def inspect_images(self): return 'fake-ci-environment'
    async def start(self, source):
        if source != self.source:
            return {'passed': False, 'exit_code': 1, 'output': '源码快照摘要不匹配'}
        return await self.command('start')
    async def version(self): return self.source
    async def health(self): return await self.command('health')
    async def command(self, kind):
        exit_code = 1 if kind == self.fail else 0
        return {'passed': exit_code == 0, 'exit_code': exit_code, 'output': 'CI 模拟命令：'+kind}
    async def rebuild(self): return await self.command('build')
    async def close(self): self.closed=True


class FakeBrowser:
    def __init__(self, workspace, bugfree=False, flaky=False):
        self.workspace, self.bugfree, self.flaky = workspace, bugfree, flaky
        self.policy=Policy(['http://app:3000'])
        self.calls=[];self.open_count=0;self.closed=False
    async def open(self): self.open_count+=1;self.closed=False
    async def close(self): self.closed=True
    async def action(self, action):
        self.calls.append(action.model_dump())
        fixed = self.bugfree or 'true' in self.workspace.read('src/value.ts')
        if self.flaky and self.open_count%2==0: fixed=True
        snap='### Page URL: http://app:3000\n- heading "Task board" [level=1] [ref=e1]\n- checkbox "Complete task" [ref=e2]'+(' [checked]' if fixed else '')
        return {'id':new_id('obs'),'url':'http://app:3000','snapshot':snap,'console':'FAKE','network':'FAKE','png':PNG}


class FakeModel:
    def __init__(self, workspace):
        self.workspace, self.decision_calls = workspace, 0
    async def generate(self,schema,context, image=None, agent_instructions=None,on_attempt=None,on_response=None,on_error=None,on_usage=None, **options):
        exchange = on_attempt('FAKE-CI', {'url':'https://fake.invalid/chat/completions',
            'headers':{'Authorization':'[REDACTED]'},'json':{'messages':[]}}, 1) if on_attempt else None
        usage={'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}
        if on_usage: on_usage(usage)
        if schema is Decision:
            kind = 'observe' if self.decision_calls == 0 else 'finish'
            self.decision_calls += 1
            value=Decision(action=BrowserAction(kind=kind),summary='CI 模拟观察后停止')
        elif schema is PatchProposal:
            value=PatchProposal(summary='CI 模拟修复',evidence_refs=context['evidence_refs'][:1],edits=[FileEdit(path='src/value.ts',before_hash=digest(self.workspace.read('src/value.ts').encode()),content='export const persisted = true;\n')])
        elif schema is ReproductionPlan:
            value = ReproductionPlan(action_indices=list(range(len(context['exploration_actions']))),
                                     summary='CI 保留已执行的复现步骤')
        else: raise ValueError('不支持的模拟输出结构')
        if on_response: on_response(exchange, {'http_status':200,'headers':{},'body':{
            'model':'FAKE-CI','usage':usage,'choices':[{'finish_reason':'stop','message':{
                'content':value.model_dump_json(),'reasoning_content':'FAKE private reasoning'}}]}})
        return ModelResult(value,usage,'FAKE-CI','stop')


def make_engine(root, bugfree=False, runner_fail=None, flaky=False):
    root.mkdir(parents=True,exist_ok=True)
    work=root/'code';(work/'src').mkdir(parents=True)
    (work/'src/value.ts').write_bytes(b'export const persisted = false;\n')
    git(work,'init','-q')
    safe_index_files(work, ['src/value.ts'])
    commit_workspace(work, 'CI baseline')
    project=Project(id='b',repo_id='ci',root=work)
    scopes=ScopeResolver({'b':project});ctx=scopes.context('b')
    profile=Profile(project='b',source_commit='HEAD',commands={k:['node','ci'] for k in ['start','reset','static','unit','build']})
    workspace, source = Workspace.export(scopes, ctx, 'HEAD', root/'workspace')
    state=RunState(scope_id='b',goal='CI 模拟状态机验证',url=profile.url,mode='repair')
    store=MemoryStore();artifacts=Artifacts(root/'artifacts')
    spec=TestSpec(goal=state.goal,assertions=[Assertion(locator=Locator(role='checkbox',name='Complete task'),condition='checked')],regression_assertions=[Assertion(locator=Locator(role='heading',name='Task board'))])
    state.test_spec_ref=artifacts.put('b',state.run_id,spec.model_dump());state.test_spec_hash=digest(spec)
    state.source_manifest=digest(source);state.repo_snapshot_ref=artifacts.put('b',state.run_id,source)
    state.memory_snapshot_ref=artifacts.put('b',state.run_id,dataclasses.asdict(ctx))
    runner=FakeRunner(state.source_manifest,runner_fail);browser=FakeBrowser(workspace,bugfree,flaky)
    engine=Engine(store,artifacts,scopes,ctx,profile,workspace,runner,browser,FakeModel(workspace),
                  Retriever(store,scopes,ctx),source,InMemorySaver())
    return engine,state


async def main(root, *, plain=False):
    from tracefix.cli.render import Renderer
    engine,state=make_engine(root/new_id('smoke'))
    await engine.run(state)
    state=engine.store.load(state.run_id,'b')
    if state.approval_ref:
        engine.store.decide_approval(state.approval_ref,state,'reject')
        await engine.run(resume='reject')
    state=engine.store.load(state.run_id,'b')
    Renderer(plain=plain).panel('FAKE CI SMOKE · 非真实 GUI/API 证据',state.model_dump_json(indent=2))
    return state
