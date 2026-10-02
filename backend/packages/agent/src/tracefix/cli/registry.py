from dataclasses import dataclass
import shlex
from tracefix.messages import error_message

@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    help: str

COMMANDS = [
    # 三类引导共用运行时账本；retarget 的确认步骤独立于提交目标文本。
    CommandSpec('hint','/hint TEXT','将提示注入当前 Run 的下一次模型请求'),
    CommandSpec('constrain','/constrain TEXT|JSON','收窄当前 Run 的路径、补丁大小或动作权限'),
    CommandSpec('retarget','/retarget TEXT | /retarget confirm ID','请求改目标；二次确认后结束父 Run 并派生子 Run'),
    CommandSpec('guidance','/guidance','查看当前 Run 引导的注入、采纳与拒绝状态'),
    CommandSpec('agents','/agents [show ID|cancel ID]','查看子 Agent 层级、结果与取消状态'),
    # compact 只请求运行时压缩历史，冻结目标、断言和权限继续完整保留。
    CommandSpec('compact','/compact','将旧步骤压缩为工作记忆并保留失败证据'),
    CommandSpec('remote','/remote [show|set|clear]','配置当前项目的远程仓库和拉取分支'),
    CommandSpec('continue','/continue RUN_ID INSTRUCTION','继续原任务并追加非成功结束标记和轨迹'),
    CommandSpec('run','/run','启动已输入目标'), CommandSpec('mode','/mode test|repair|chat','选择测试、修复或流式对话'),
    CommandSpec('projects','/projects [list|show|use]','项目配置、统计与切换'),
    CommandSpec('runs','/runs [list|show|logs|sources|export|remember]','运行历史、来源、产物与经验归档'),
    CommandSpec('knowledge','/knowledge [list|show|search|import|new|edit|enable|disable|export]','管理与检索共享知识文档'),
    CommandSpec('chat','/chat [--no-knowledge] MESSAGE','流式咨询模型；/chat clear 清空当前项目会话'),
    CommandSpec('status','/status','查看状态和用量'), CommandSpec('trace','/trace','查看已提交审计事件'),
    CommandSpec('diff','/diff','查看当前补丁'), CommandSpec('evidence','/evidence ID','查看证据路径'),
    CommandSpec('model-log','/model-log [N]','查看模型请求与响应'), CommandSpec('report','/report','查看静态报告'),
    CommandSpec('memory','/memory [search QUERY|sources [RUN_ID]]','查看记忆快照、检索经验与实际使用来源'), CommandSpec('context','/context','查看上下文和用量'),
    CommandSpec('skills','/skills','查看流程 Skill 索引'), CommandSpec('scope','/scope [use ID]','查看或切换项目'),
    CommandSpec('pause','/pause','在安全边界暂停'), CommandSpec('interrupt','/interrupt','安全打断并等待纠正提示'),
    CommandSpec('resume','/resume RUN_ID','校验检查点并继续'), CommandSpec('cancel','/cancel','取消当前 Run'),
    CommandSpec('approve','/approve ID','批准绑定补丁动作'), CommandSpec('reject','/reject ID','拒绝动作并保留产物'),
    CommandSpec('help','/help','显示帮助'), CommandSpec('quit','/quit','退出；活动 Run 先取消'),
]
REGISTRY = {command.name: command for command in COMMANDS}

def parse(text):
    if not text.startswith('/'):
        return None, [text]
    parts = split_arguments(text[1:])
    if not parts or parts[0] not in REGISTRY:
        raise ValueError('未知命令；请输入 /help 查看帮助')
    return parts[0], parts[1:]


def split_arguments(text):
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    lexer.escape = ''
    try:
        return list(lexer)
    except ValueError as error:
        raise ValueError(error_message(error)) from error
