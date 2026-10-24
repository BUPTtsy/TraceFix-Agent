"""浏览器动作的来源、阶段和观测绑定策略。"""

from urllib.parse import urlsplit

from tracefix.runtime.contracts import BrowserAction, Phase, RunStatus
from tracefix.runtime.guidance import GuidanceRejected


def origin(url):
    """解析并规范化 HTTP(S) 来源，拒绝凭据、未知协议和无主机名的 URL。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise PermissionError("URL 无效")
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return parsed.scheme, parsed.hostname.lower(), port


class Policy:
    """集中执行导航白名单及浏览器动作授权检查。"""

    def __init__(self, allowed_origins):
        """根据配置建立不可变的来源集合。"""
        self.origins = {origin(u) for u in allowed_origins}

    def url(self, url):
        """确保 URL 的协议、主机和端口与已授权来源完全一致。"""
        if origin(url) not in self.origins:
            raise PermissionError("URL 来源未获授权")

    def initial_navigation(self, state, action):
        """限制准备阶段的首次导航只能打开运行状态中的目标 URL。"""
        if (state.run_status != RunStatus.RUNNING or state.phase != Phase.PREPARE
                or state.test_spec_ref or state.observation_ref
                or action.kind != 'navigate' or action.value != state.url):
            raise PermissionError('编译测试规范前仅允许首次只读打开用户指定的目标 URL')
        self.url(action.value)

    def browser(self, state, action: BrowserAction, spec, observation):
        """校验动作阶段、TestSpec 授权、观测新鲜度和元素引用绑定。"""
        if state.run_status != RunStatus.RUNNING or state.phase not in {Phase.PREPARE, Phase.EXPLORE, Phase.REPRODUCE, Phase.VERIFY}:
            raise PermissionError("当前状态下浏览器不可用")
        if action.kind not in spec.authorized_actions:
            raise PermissionError(f'动作 {action.kind!r} 超出 TestSpec 授权范围：{spec.authorized_actions}')
        # L2 引导在冻结 TestSpec 授权后继续收窄；任何一条约束拒绝都不能执行动作。
        for constraint in getattr(state, 'guidance_constraints', []):
            if constraint.allowed_actions is not None and action.kind not in constraint.allowed_actions:
                raise GuidanceRejected('浏览器动作违反用户约束：' + action.kind,
                                       details={'kind': action.kind})
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
