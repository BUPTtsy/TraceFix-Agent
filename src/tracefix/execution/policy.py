from urllib.parse import urlsplit

from tracefix.runtime.contracts import BrowserAction, Phase, RunStatus


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise PermissionError("URL 无效")
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return parsed.scheme, parsed.hostname.lower(), port


class Policy:
    def __init__(self, allowed_origins):
        self.origins = {origin(u) for u in allowed_origins}

    def url(self, url):
        if origin(url) not in self.origins:
            raise PermissionError("URL 来源未获授权")

    def initial_navigation(self, state, action):
        if (state.run_status != RunStatus.RUNNING or state.phase != Phase.PREPARE
                or state.test_spec_ref or state.observation_ref
                or action.kind != 'navigate' or action.value != state.url):
            raise PermissionError('编译测试规范前仅允许首次只读打开用户指定的目标 URL')
        self.url(action.value)

    def browser(self, state, action: BrowserAction, spec, observation):
        if state.run_status != RunStatus.RUNNING or state.phase not in {Phase.PREPARE, Phase.EXPLORE, Phase.REPRODUCE, Phase.VERIFY}:
            raise PermissionError("当前状态下浏览器不可用")
        if action.kind not in spec.authorized_actions:
            raise PermissionError(f'动作 {action.kind!r} 超出 TestSpec 授权范围：{spec.authorized_actions}')
        if action.kind == 'navigate':
            self.url(action.value)
        if action.kind in {'click', 'type', 'select', 'press'}:
            if not observation or action.observation_id != observation['id']:
                raise PermissionError("浏览器观测已过期")
        if action.kind in {'click', 'type', 'select'}:
            from tracefix.execution.browser import resolve_locator
            try:
                ref = resolve_locator(observation['snapshot'], action.locator)
            except ValueError as e:
                # 到这里的定位器来自模型（重放路径已在绑定时解析过），零匹配或多匹配属于模型输出问题：
                # 既不是越权，也不是基础设施故障。
                from tracefix.model.gateway import ModelOutputError
                raise ModelOutputError('模型定位器无法唯一匹配元素：' + str(e)) from e
            if action.element_ref != ref:
                raise PermissionError("元素引用未绑定到当前观测")
        if action.kind == 'press' and action.value not in {'Enter', 'Escape', 'Tab', 'Shift+Tab', 'ArrowDown', 'ArrowUp', 'Space'}:
            raise PermissionError("该按键不在允许列表中")
