import asyncio
import sys

from prompt_toolkit.patch_stdout import patch_stdout

from tracefix.cli.input import make_prompt
from tracefix.cli.registry import COMMANDS, parse
from tracefix.cli.render import Renderer
from tracefix.runtime.contracts import RunState, RunStatus


DEMO_DIFF = ('--- a/src/status.ts\n+++ b/src/status.ts\n@@ -1 +1 @@\n'
             '-const status = cachedStatus;\n+const status = await loadStatus();\n')


def demo_events():
    samples = [
        ('PREPARE', 'run.started', {'manifest_ref': '演示_源码快照.json'}),
        ('EXPLORE', 'state.changed', {'phase': 'EXPLORE', 'status': 'RUNNING'}),
        ('EXPLORE', 'model.started', {'model': '演示模型', 'logical_call': 1, 'attempt': 1, 'schema': 'Decision'}),
        ('EXPLORE', 'model.called', {'model_revision': '演示模型', 'finish_reason': 'stop'}),
        ('REPRODUCE', 'tool.started', {'operation_id': 'demo:1:browser.assert', 'intent': {'assertion': '刷新后保留任务状态'}}),
        ('REPRODUCE', 'tool.completed', {'operation_id': 'demo:1:browser.assert', 'receipt': {'passed': False, 'evidence_ref': '演示_刷新截图.png'}}),
        ('DIAGNOSE', 'model.started', {'model': '演示模型', 'logical_call': 2, 'attempt': 1, 'schema': 'PatchProposal'}),
        ('DIAGNOSE', 'model.called', {'model_revision': '演示模型', 'finish_reason': 'stop'}),
        ('PATCH', 'patch.applied', {'diff_ref': '演示_候选补丁.diff', 'patch_hash': 'a'*64}),
        ('VERIFY', 'gate.decided', {'passed': True, 'evidence_ref': '演示_验证记录.json'}),
        ('REVIEW', 'state.changed', {'phase': 'REVIEW', 'status': 'WAITING_APPROVAL'}),
    ]
    return [{'seq': index, 'phase': phase, 'type': kind, 'payload': payload}
            for index, (phase, kind, payload) in enumerate(samples, start=1)]


async def preview(args):
    renderer = Renderer(args.plain)
    renderer.welcome(args.project, args.mode, '演示模型', preview=True)
    state = RunState(scope_id=args.project, goal='演示：刷新页面后任务状态应保留',
                     url='http://preview.invalid', mode=args.mode, run_id='demo_preview')
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    events = demo_events()
    task = None

    async def play():
        renderer.reset()
        state.phase = 'PREPARE'
        state.run_status = RunStatus.RUNNING
        state.outcome = None
        renderer.status(state)
        for event in events:
            renderer.event(event)
            if interactive:
                await asyncio.sleep(0.35)
        state.phase = 'REVIEW'
        state.run_status = RunStatus.WAITING_APPROVAL
        state.approval_ref = 'demo_approval'
        state.patch_hash = 'a'*64
        state.budget.model_calls = 2
        state.budget.browser_actions = 1
        state.budget.patches = 1
        renderer.status(state)

    async def stop():
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        renderer.run_status = 'CANCELLED'
        renderer.activity = ''

    if not interactive:
        await play()
        renderer.diff(DEMO_DIFF)
        renderer.panel('演示结果', '验证通过（演示数据）；审批尚未提交。演示文件名仅用于展示，不对应实际文件。')
        return
    prompt = make_prompt(renderer, lambda: args.project, lambda: args.mode)
    renderer.text('演示专用：/run 重播 · /diff 补丁 · /trace 时间线 · /approve demo_approval · /quit 退出')
    with patch_stdout(raw=True):
        task = asyncio.create_task(play())
        try:
            while True:
                try:
                    text = (await prompt.prompt_async()).strip()
                    if not text:
                        continue
                    command, arguments = parse(text)
                    if command == 'quit':
                        break
                    if command == 'run':
                        await stop()
                        task = asyncio.create_task(play())
                    elif command == 'help':
                        renderer.help(COMMANDS)
                        renderer.text('当前为演示模式：仅 /run /status /trace /diff /approve /reject /cancel /quit 执行演示动作。')
                    elif command == 'trace':
                        for event in events:
                            renderer.event(event, replay=True)
                    elif command == 'diff':
                        renderer.diff(DEMO_DIFF)
                    elif command == 'status':
                        renderer.status(state)
                    elif command in {'approve', 'reject'}:
                        if state.run_status != RunStatus.WAITING_APPROVAL or arguments != ['demo_approval']:
                            raise ValueError('等待演示审批后，输入 /approve demo_approval 或 /reject demo_approval')
                        state.run_status = RunStatus.COMPLETED
                        renderer.status(state)
                        renderer.panel('演示结果', '演示审批已'+('批准' if command == 'approve' else '拒绝')+'。未执行真实动作。')
                    elif command == 'cancel':
                        await stop()
                        state.run_status = RunStatus.CANCELLED
                        renderer.status(state)
                    else:
                        renderer.text('演示模式已收到输入；输入 /run 重播，或 /quit 退出。')
                except KeyboardInterrupt:
                    await stop()
                    state.run_status = RunStatus.CANCELLED
                    renderer.status(state)
                except EOFError:
                    break
                except ValueError as error:
                    from tracefix.messages import error_message
                    renderer.panel('演示输入', error_message(error))
        finally:
            await stop()
