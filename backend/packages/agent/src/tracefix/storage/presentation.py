"""Chinese labels for human-facing reports; machine contracts stay stable."""
import html
import json
import base64
from urllib.parse import quote


LABELS = {
    'PREPARE': '准备', 'EXPLORE': '探索', 'REPRODUCE': '复现', 'DIAGNOSE': '诊断',
    'PATCH': '修补', 'VERIFY': '验证', 'REVIEW': '审批', 'FINALIZE': '收尾',
    'FIX_VERIFIED': '修复已通过本地验证', 'NO_BUG_FOUND': '当前测试范围内未发现问题',
    'BUG_CONFIRMED': '已确认存在缺陷', 'LOOP_DETECTED': '检测到死循环',
    'INCONCLUSIVE': '证据不足，无法确定', 'REPAIR_EXHAUSTED': '修复次数已用尽',
    'POLICY_BLOCKED': '被策略阻止', 'INFRA_FAILURE': '运行环境故障',
    'RUNNING': '运行中', 'WAITING_INPUT': '等待输入', 'WAITING_APPROVAL': '等待审批',
    'PAUSED': '已暂停', 'COMPLETED': '已结束', 'CANCELLED': '已取消', 'ABNORMAL': '异常结束', 'FAILED': '运行失败', 'SUPERSEDED': '已替换',
    'request': '模型输入', 'response': '模型输出', 'error': '错误信息',
    'record': '记录', 'test': '测试', 'repair': '修复', 'chat': '对话', 'knowledge.selected': '知识来源已选择',
    'evidence.damaged': '证据损坏', 'retrieval.degraded': '代码检索与修复记忆未启用',
    # 标签用于解释实际降级能力，事件类型与 fallback 原因仍保留原始契约值。
    'memory.degraded': '记忆检索已降级', 'sqlite_fallback': '已回退到 SQLite 本地检索',
    'disabled_capabilities': '未启用的能力', 'postgres_unavailable': '未连接 PostgreSQL',
    'code_index': '代码索引', 'code_retrieval': '代码检索',
    'repair_memory_read': '读取修复记忆', 'repair_memory_write': '写入修复记忆',
    'vector_retrieval': '向量检索',
    'interrupt.prompt': '运行中输入已记录', 'recorded_only': '仅记录，未注入模型',
    'applied_to_model': '是否已注入模型', 'resumes_original_plan': '是否按原计划继续',
    'run.continued': '异常结束后继续执行', 'run.continuation_requested': '用户请求继续原任务',
    'model.decision': '模型决策摘要',
    'continuation_count': '继续执行次数', 'abnormal_termination': '曾发生非成功结束',
    'continuation_markers': '继续执行标记', 'continuation_instruction': '用户继续指令',
    'schema_version': '数据格式版本', 'run_id': '运行编号', 'scope_id': '项目',
    'goal': '修复目标', 'outcome': '验证结论', 'reproduced': '问题是否稳定复现',
    'patch_hash': '补丁校验值', 'branch': '本地候选分支', 'merged': '是否已合并',
    'execution_mode': '运行策略', 'batch': '批处理', 'interactive': '交互式',
    'patch_diff_ref': '最终补丁文件', 'patch_available': '是否有有效补丁',
    'patch_verification': '补丁验证状态', 'patch_export_error': '补丁导出错误',
    'verified': '已验证', 'unverified': '尚未完成验证', 'none': '无有效补丁',
    'result_summary': '最终结果摘要', 'diagnosis_retry_count': '连续诊断重试次数',
    'diagnosis_feedback_refs': '诊断重试反馈记录',
    'budget': '资源使用情况', 'usage': '资源用量', 'evidence_refs': '复现与断言证据',
    'execution_backend': '执行环境', 'source_manifest': '源码校验值',
    'environment_digest': '环境版本信息', 'baseline_validation_refs': '修复前基线测试',
    'validation_refs': '修复后验证记录', 'cleanup_errors': '环境清理错误',
    'model_exchange_refs': '模型输入输出记录', 'agent_instructions_ref': '项目约束快照',
    'agent_instructions_hash': '项目约束校验值', 'agent_instructions_path': '项目约束来源',
    'limits': '结论适用范围', 'model_calls': '模型调用次数', 'browser_actions': '浏览器动作数',
    'patches': '补丁次数', 'subtasks': '子任务数', 'tokens': 'Token 总数',
    'cost_usd': '费用（未估算）', 'max_model_calls': '历史模型调用上限',
    'max_browser_actions': '浏览器动作上限', 'max_patches': '补丁上限',
    'max_subtasks': '子任务上限', 'max_tokens': 'Token 上限', 'max_cost_usd': '费用上限（美元）',
    'deadline': '截止时间（Unix 秒）', 'last_progress': '最近进展时间（Unix 秒）',
    'stall_seconds': '停滞超时（秒）', 'phase': '阶段', 'passed': '是否通过',
    'evidence_ref': '证据文件', 'diff_ref': '补丁文件', 'manifest_ref': '源码快照',
    'request_ref': '模型输入文件', 'response_ref': '模型输出文件', 'error_ref': '模型错误文件',
    'report_ref': '报告数据文件', 'html_ref': '报告页面', 'logical_call': '模型调用编号',
    'attempt': '尝试次数', 'schema': '输出结构', 'model': '模型',
    'run.started': '运行开始', 'run.finished': '运行结束', 'run.error': '运行错误',
    'run.warning': '运行告警',
    'model.usage': '模型用量', 'model.called': '模型调用完成',
    'model.request.persisted': '模型输入已保存', 'model.response.persisted': '模型输出已保存',
    'model.error.persisted': '模型错误已保存', 'gate.decided': '验证门禁结论',
    'patch.applied': '补丁已应用', 'tool.started': '工具开始', 'tool.completed': '工具完成',
    'state.changed': '状态更新', 'model.started': '模型开始', 'validation.failed': '验证失败',
    'usage': '用量', 'prompt_tokens': '输入 Token', 'completion_tokens': '输出 Token',
    'total_tokens': 'Token 合计', 'model_revision': '模型版本', 'finish_reason': '结束原因',
    'cost_is_configured_estimate': '费用未估算', 'http_status': 'HTTP 状态码',
    'reasoning_content_present': '响应含 reasoning_content 字段',
    'seq': '事件序号', 'type': '事件类型', 'at': '发生时间（Unix 秒）',
    'revision': '状态版本', 'payload': '事件内容',
    'run_status': '运行状态',
    'WAITING_NETWORK': '等待网络恢复', 'UNKNOWN_OPERATION': '操作结果未知',
    'RETRYING': '正在重试', 'IN_FLIGHT': '执行中', 'DONE': '已完成', 'UNKNOWN': '未知',
    'tool.error': '工具执行错误', 'input.applied': '补充指令已记录',
    'subtask.completed': '子任务完成', 'reason': '原因', 'effect': '处理结果',
    'loop.suspected': '疑似死循环', 'loop.detected': '检测到死循环',
    'error_details': '错误详情', 'details': '详细信息', 'message': '说明',
    'original_message': '原始诊断', 'status': '状态', 'category': '错误分类',
    'operation_status': '操作状态', 'operation_id': '操作编号', 'intent': '操作意图',
    'receipt': '操作回执', 'action': '动作', 'kind': '种类', 'summary': '摘要',
    'retryable': '是否允许重试', 'will_retry': '是否即将重试',
    'requires_manual_review': '需要人工核对', 'requires_new_run': '需要新建运行',
    'retry_after_seconds': '服务端建议等待秒数', 'retry_delay_seconds': '重试等待秒数',
    'billing_status': '计费用量状态', 'request_status': '请求状态',
    'logical_exchange_id': '逻辑调用编号', 'exchange_id': '调用记录编号',
    'action_id': '动作编号', 'observation_id': '观测编号', 'element_ref': '元素引用',
    'started_at': '开始时间', 'finished_at': '结束时间', 'dispatched': '是否已派发',
    'source': '来源', 'browser': '浏览器', 'sandbox': '沙箱',
    'timeout': '超时', 'network': '网络', 'configuration': '配置',
    'rate_limited': '请求限流', 'service_unavailable': '服务不可用',
    'http_error': 'HTTP 错误', 'response_validation': '响应校验',
    'incomplete_output': '输出不完整', 'output_validation': '输出校验',
    'missing_receipt': '缺少操作回执', 'browser_transport_error': '浏览器连接错误',
    'browser_action_unknown': '浏览器动作结果未知', 'retry_budget': '重试预算',
    'not_sent': '尚未发送', 'response_received': '已收到响应',
    'unknown': '未知', 'known': '已知', 'not_confirmed': '尚未确认',
    'stop': '正常结束', 'length': '达到输出长度上限', 'content_filter': '内容过滤',
    'sandbox.start': '启动沙箱', 'baseline.unit': '运行基线单元测试',
    'browser.initial_navigation': '首次打开目标页面', 'scenario.reset': '重置场景',
    'workspace.patch': '应用工作区补丁', 'local.commit': '创建本地候选提交',
    'verify.static': '静态检查', 'verify.unit': '单元测试', 'verify.build': '构建',
    'verify.health': '健康检查', 'verify.original': '原问题重测', 'verify.regression': '回归测试',
    'check_plan': '冻结检查计划', 'check_results': '逐项检查结果', 'check_summary': '检查汇总',
    'overall_status': '整体测试结果', 'images': '图片证据', 'image_errors': '图片证据错误',
    'PASSED': '测试通过', 'PASSED_WITH_FINDINGS': '通过但存在非阻断问题',
    'criteria': '检测内容与指标', 'severity': '严重级别', 'detector': '检测方式',
    'blocker': '阻断', 'critical': '阻断', 'normal': '普通', 'warning': '告警',
    'user_goal': '用户目标', 'rule': '检测规则', 'model': '模型', 'dom': 'DOM',
    'oracle': '页面规则', 'static': '源码静态检查', 'ast': 'AST', 'multimodal': '多模态',
    'actual': '实际检测结果', 'coverage_complete': '计划覆盖完整', 'blocker_failed': '阻断失败数',
    'executed': '已执行数', 'missing': '遗漏数', 'missing_ids': '遗漏检查编号',
}


def label(value):
    return LABELS.get(str(value), str(value))


def readable(value):
    if isinstance(value, dict):
        return {label(key): readable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [readable(item) for item in value]
    if isinstance(value, str):
        return label(value)
    if value is None:
        return '无'
    if isinstance(value, bool):
        return '是' if value else '否'
    return value


def report_page(report, *, artifacts=None, scope_id=None, run_id=None):
    from tracefix.storage.reporting import CHECK_STATUSES, ISSUE_STATUSES, _check_text, collect_report_images

    def render(value, key=''):
        if isinstance(value, list) and key.endswith('_refs'):
            return '<ul>'+''.join('<li>'+render(ref, 'artifact_ref')+'</li>' for ref in value)+'</ul>'
        if isinstance(value, str) and key.endswith('_ref'):
            return f'<a href="{quote(value, safe="")}">{html.escape(value)}</a>'
        return '<pre>'+html.escape(json.dumps(readable(value), ensure_ascii=False, indent=2))+'</pre>'

    technical_keys = {'schema_version', 'run_id', 'patch_hash', 'source_manifest',
                      'environment_digest', 'agent_instructions_hash'}
    summary, technical = [], []
    for key, value in report.items():
        if key in {'issues', 'check_plan', 'check_results', 'images', 'image_errors'}:
            continue
        row = f'<tr><th>{html.escape(label(key))}</th><td>{render(value, key)}</td></tr>'
        (technical if key in technical_keys else summary).append(row)
    issue_sections = []
    for index, issue in enumerate(report.get('issues', []), 1):
        status = ISSUE_STATUSES.get(issue['status'], issue['status'])
        details = ''.join(f'<dt>{heading}</dt><dd>{html.escape(str(issue.get(key, "")))}</dd>'
            for key, heading in [('location', '位置'), ('expected', '预期'), ('actual', '实际'), ('verification', '验证')])
        steps = ''.join('<li>' + html.escape(step) + '</li>' for step in issue.get('steps', []))
        evidence = ''.join('<li>' + render(ref, 'artifact_ref') + '</li>' for ref in issue.get('evidence_refs', []))
        issue_sections.append(f'<article><h3>{index}. [{html.escape(status)}] {html.escape(issue["title"])}</h3>'
            f'<dl>{details}</dl><h4>复现步骤</h4><ol>{steps}</ol><h4>证据</h4><ul>{evidence}</ul></article>')
    title = '测试报告' if report.get('mode') == 'test' else '修复报告'
    issues = '<section><h2>问题清单</h2>' + (''.join(issue_sections) or '<p>未形成有证据的问题记录。</p>') + '</section>'
    check_rows = []
    for item in report.get('check_results') or []:
        status = str(item.get('status', 'inconclusive')).lower()
        status = {'pass': 'passed', 'fail': 'failed'}.get(status, status)
        cells = [html.escape(str(item.get('id') or item.get('check_id') or '')),
                 html.escape(str(item.get('name') or item.get('title') or '未命名检查')),
                 html.escape(_check_text(item.get('criteria', ''))),
                 html.escape(label(item.get('severity', 'normal'))),
                 html.escape(label(item.get('source', 'rule'))),
                 html.escape(label(item.get('detector', item.get('detector_type', 'model')))),
                 html.escape(CHECK_STATUSES.get(status, status)),
                 html.escape(_check_text(item.get('actual', ''))),
                 html.escape(_check_text(item.get('error', ''))),
                 render(item.get('evidence_refs') or [], 'evidence_refs')]
        check_rows.append('<tr>' + ''.join('<td>' + value + '</td>' for value in cells) + '</tr>')
    checks = ''
    if 'check_plan' in report or 'check_results' in report:
        headings = ['ID', '名称', '检测内容与指标', '严重级别', '来源', '检测方式', '状态', '实际结果', '错误', '证据']
        checks = ('<section><h2>逐项检查</h2><table><thead><tr>'
                  + ''.join('<th>' + heading + '</th>' for heading in headings)
                  + '</tr></thead><tbody>' + ''.join(check_rows)
                  + '</tbody></table><details><summary>完整检查计划</summary>'
                  + render(report.get('check_plan') or []) + '</details></section>')
    image_sections = []
    image_errors = list(report.get('image_errors') or [])
    if artifacts is not None:
        scope_id, run_id = scope_id or report.get('scope_id'), run_id or report.get('run_id')
        if not scope_id or not run_id:
            image_errors.append({'ref': '', 'error': '缺少当前 Run 的 scope_id/run_id，无法读取图片证据'})
        else:
            images, errors = collect_report_images(report, artifacts, scope_id, run_id)
            image_errors.extend(errors)
            for item in images:
                try:
                    raw = artifacts.read(scope_id, run_id, item['ref'])
                    source = 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')
                    image_sections.append('<figure><img src="' + source + '" alt="'
                        + html.escape(str(item['alt']), quote=True) + '"><figcaption>'
                        + html.escape(str(item['alt'])) + ' — ' + render(item['ref'], 'artifact_ref')
                        + '</figcaption></figure>')
                except (OSError, ValueError, PermissionError) as error:
                    image_errors.append({'ref': item['ref'], 'error': str(error)})
    elif report.get('images'):
        image_errors.append({'ref': '', 'error': '未提供当前 Run 的 Artifacts 读取器，无法校验并嵌入图片证据'})
    for item in image_errors:
        message = _check_text(item.get('error')) if isinstance(item, dict) else _check_text(item)
        reference = item.get('ref', '') if isinstance(item, dict) else ''
        image_sections.append('<p class="evidence-error">图片证据无法读取：'
                              + html.escape(str(reference)) + '；' + html.escape(message) + '</p>')
    image_section = '<section><h2>图片证据</h2>' + ''.join(image_sections) + '</section>' if image_sections else ''
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'style-src \'unsafe-inline\'; img-src \'self\' data:">'
            f'<title>TraceFix {title}</title><style>body{{font:16px system-ui;max-width:1050px;'
            'margin:40px auto;padding:20px}table{border-collapse:collapse;width:100%}'
            'td,th{border:1px solid #ddd;padding:12px;text-align:left;vertical-align:top}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer;margin:20px 0}'
            'a{overflow-wrap:anywhere}img{max-width:100%;height:auto}figure{margin:24px 0}'
            'figcaption{color:#555}.evidence-error{color:#9d2020}</style>'
            + f'<h1>TraceFix {title}</h1>' + checks + issues + image_section + '<table>'
            +''.join(summary)+'</table><details><summary>技术详情与完整校验值</summary><table>'
            +''.join(technical)+'</table></details>')
