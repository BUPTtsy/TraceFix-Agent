"""Built-in demonstration rules shipped with the local console."""
from __future__ import annotations

from .models import Rule


def builtin_rules() -> list[Rule]:
    return [
        Rule(
            id="builtin_console_no_error", name="页面加载和操作后不得出现 Console Error",
            status="enabled", category="console", severity="major", priority=95, pinned=True,
            scope={"level": "org"}, phases=["EXPLORE", "VERIFY"],
            detection={"type": "oracle", "oracle": {"kind": "console_no_error", "ignore": []}},
            fix_guidance="检查报错堆栈对应的组件和请求失败分支；不要通过静默 catch 或降低日志级别隐藏错误。",
            examples={"violation": "console 出现 Error 或未捕获异常", "compliant": "观测期间没有 Console Error"},
            tags=["baseline", "console"], owner="TraceFix 内置规则",
        ),
        Rule(
            id="builtin_api_no_5xx", name="业务 API 不得返回 5xx",
            status="enabled", category="network", severity="critical", priority=90, pinned=True,
            scope={"level": "org"}, phases=["EXPLORE", "VERIFY"],
            detection={"type": "oracle", "oracle": {"kind": "network_status", "url_patterns": ["/api/**"], "forbidden_statuses": [500, 501, 502, 503, 504, 505, 506, 507, 508, 509, 510, 511]}},
            fix_guidance="检查服务端错误分支和请求参数；前端应展示可理解的失败反馈，不要把 5xx 当作成功处理。",
            examples={"violation": "/api/orders 返回 500", "compliant": "业务 API 返回 2xx 或明确的可处理 4xx"},
            tags=["baseline", "network"], owner="TraceFix 内置规则",
        ),
        Rule(
            id="builtin_submit_feedback", name="提交后必须出现成功或失败反馈",
            status="enabled", category="functional", severity="major", priority=85, pinned=True,
            scope={"level": "org"}, phases=["EXPLORE", "VERIFY"],
            detection={"type": "oracle", "oracle": {"kind": "dom_after_action", "trigger": {"action": "click", "locator": {"role": "button", "name_regex": "提交|保存|Submit|Save"}}, "expect_any": [{"locator": {"role": "alert"}, "condition": "visible"}, {"locator": {"role": "status"}, "condition": "visible"}], "within_ms": 3000}},
            fix_guidance="在成功和失败分支分别设置可访问的 role=alert 或 role=status 反馈，并确保反馈不会只写入 Console。",
            examples={"violation": "点击提交后没有任何可见反馈", "compliant": "提交后出现保存成功或错误提示"},
            tags=["baseline", "form", "feedback"], owner="TraceFix 内置规则",
        ),
        Rule(
            id="builtin_input_label", name="表单输入必须关联可访问名称",
            status="enabled", category="a11y", severity="major", priority=75,
            scope={"level": "org", "path_globs": ["**/*.tsx", "**/*.jsx", "**/*.html"]}, phases=["DIAGNOSE", "VERIFY"],
            detection={"type": "static", "static": {"regex": r"<input\b(?![^>]*(?:aria-label|aria-labelledby|id\s*=))[^>]*>", "path_globs": ["**/*.tsx", "**/*.jsx", "**/*.html"], "message": "input 缺少 aria-label、aria-labelledby 或可关联 id"}},
            fix_guidance="优先使用 label htmlFor/id 建立关联；自定义控件必须提供稳定的可访问名称。",
            examples={"violation": "<input type=\"text\" />", "compliant": "<label htmlFor=\"email\">邮箱</label><input id=\"email\" />"},
            tags=["baseline", "a11y", "form"], owner="TraceFix 内置规则",
        ),
        Rule(
            id="builtin_empty_state", name="空列表必须提供空状态说明",
            status="enabled", category="functional", severity="minor", priority=55,
            scope={"level": "org"}, phases=["EXPLORE", "DIAGNOSE"],
            detection={"type": "guided", "guided": {"prompt": "当列表、表格或搜索结果为空时，确认页面提供明确的空状态说明、下一步操作或重新加载入口。"}},
            fix_guidance="为空集合提供说明性文案和下一步操作，不要只渲染空白区域或永久加载状态。",
            examples={"violation": "无数据时表格区域完全空白", "compliant": "显示暂无数据及创建/刷新操作"},
            tags=["guidance", "empty-state"], owner="TraceFix 内置规则",
        ),
    ]
