/**
 * 斜杠命令注册表：仅用于交互提示、菜单与 /help 渲染。
 *
 * 注册表不参与命令派发，cli.ts 的 execute() 仍是唯一的语义来源，
 * 因此这里的改动不会影响任何命令的解析结果或错误文案。
 */

export interface SubCommand {
  name: string;
  summary: string;
}

export interface CommandSpec {
  /** 不含前导斜杠的命令名 */
  name: string;
  /** 参数形态提示，例如 test|repair|chat */
  hint: string;
  /** 一句中文解释 */
  summary: string;
  /** 分组，用于 /help 排版 */
  group: string;
  /** 子动作提示 */
  actions?: SubCommand[];
  /** 该命令是别名，指向 aliasOf */
  aliasOf?: string;
  /** 仅在有 Agent 正在运行时才会转发给 Agent */
  requiresRun?: boolean;
}

export const GROUPS = ['会话', '任务', '项目与知识', '产物与诊断', '运行控制'] as const;

export const COMMANDS: CommandSpec[] = [
  {
    name: 'help', hint: '', group: '会话',
    summary: '显示分组命令帮助',
  },
  {
    name: 'quit', hint: '', group: '会话',
    summary: '退出 CLI；若有 Agent 在跑先向它发送 /quit',
  },
  {
    name: 'mode', hint: 'test|repair|chat', group: '任务',
    summary: '切换新建 Run 的模式',
    actions: [
      {name: 'test', summary: '只复现并取证，不改代码'},
      {name: 'repair', summary: '复现后尝试生成修复补丁'},
      {name: 'chat', summary: '/run 直接转为模型对话，不启动 Agent'},
    ],
  },
  {
    name: 'run', hint: '', group: '任务',
    summary: '用当前目标与模式启动一次 Run',
  },
  {
    name: 'continue', hint: 'RUN_ID INSTRUCTION', group: '任务',
    summary: '带追加指令继续一次已结束的 Run',
  },
  {
    name: 'status', hint: '', group: '任务',
    summary: '查看当前项目最近一次 Run 的状态',
  },
  {
    name: 'chat', hint: '[--no-knowledge] MESSAGE | clear', group: '任务',
    summary: '直接向模型提问；chat clear 清空本项目会话',
    actions: [{name: 'clear', summary: '清空当前项目的 Chat 会话历史'}],
  },
  {
    name: 'projects', hint: 'list|show|use|create|configure', group: '项目与知识',
    summary: '查看、切换与配置项目',
    actions: [
      {name: 'list', summary: '列出全部已注册项目'},
      {name: 'show', summary: '查看某个项目（默认当前项目）的配置'},
      {name: 'use', summary: '把后续命令切换到指定项目'},
      {name: 'create', summary: '新建项目：/projects create PROJECT_ID ROOT'},
      {name: 'configure', summary: '写入项目的启动/重置/静态/单测/构建命令'},
    ],
  },
  {
    name: 'scope', hint: 'list|show|use|create|configure', group: '项目与知识',
    summary: '/projects 的别名，查看或切换项目', aliasOf: 'projects',
  },
  {
    name: 'knowledge', hint: 'list|show|search|new|import|edit|enable|disable|export', group: '项目与知识',
    summary: '管理知识库文档',
    actions: [
      {name: 'list', summary: '列出文档，可加 --query/--kind 过滤'},
      {name: 'show', summary: '按 ID 查看文档全文'},
      {name: 'search', summary: '按关键词检索文档'},
      {name: 'new', summary: '新建文档，默认打开编辑器填模板'},
      {name: 'import', summary: '从 .md/.markdown/.txt 文件导入文档'},
      {name: 'edit', summary: '编辑文档；从文件更新须带 --version'},
      {name: 'enable', summary: '启用文档，使其参与检索与注入'},
      {name: 'disable', summary: '停用文档，保留内容但不再注入'},
      {name: 'export', summary: '把文档正文导出到文件'},
    ],
  },
  {
    name: 'memory', hint: 'list|show|search|new|import|edit|enable|disable|export', group: '项目与知识',
    summary: '/knowledge 的别名，管理知识库文档', aliasOf: 'knowledge',
  },
  {
    name: 'skills', hint: '', group: '项目与知识',
    summary: '列出 skills 目录下可用的流程 Skill',
  },
  {
    name: 'runs', hint: 'list|show|logs|trace|sources|export|remember|continue', group: '产物与诊断',
    summary: '查看与操作运行记录',
    actions: [
      {name: 'list', summary: '列出运行记录，可加 --all/--status/--query/--limit'},
      {name: 'show', summary: '按 RUN_ID 查看单条运行记录'},
      {name: 'logs', summary: '查看该 Run 的日志'},
      {name: 'trace', summary: '查看该 Run 的审计事件轨迹'},
      {name: 'sources', summary: '查看该 Run 实际使用的知识来源'},
      {name: 'export', summary: '导出该 Run 的产物：/runs export ID REF FILE'},
      {name: 'remember', summary: '把该 Run 归档成一篇待完善的经验文档'},
      {name: 'continue', summary: '继续该 Run：/runs continue ID INSTRUCTION'},
    ],
  },
  {
    name: 'trace', hint: '', group: '产物与诊断',
    summary: '查看最近一次 Run 的审计事件轨迹',
  },
  {
    name: 'diff', hint: '', group: '产物与诊断',
    summary: '打印最近一次 Run 的补丁内容',
  },
  {
    name: 'report', hint: '', group: '产物与诊断',
    summary: '打印最近一次 Run 的静态报告文件路径',
  },
  {
    name: 'evidence', hint: 'REF', group: '产物与诊断',
    summary: '按产物 ref 打印证据文件路径',
  },
  {
    name: 'model-log', hint: '', group: '产物与诊断',
    summary: '列出最近一次 Run 的模型请求与响应产物',
  },
  {
    name: 'context', hint: '', group: '产物与诊断',
    summary: '查看最近一次 Run 的目标、阶段、知识与结论',
  },
  {
    name: 'remote', hint: 'show|set|clear', group: '项目与知识',
    summary: '配置远程仓库',
    actions: [
      {name: 'show', summary: '查看项目级与会话级远程配置及生效结果'},
      {name: 'set', summary: '设置远程：--repository owner/repo，可加 --base/--branch/--destination/--remote-write/--scope session'},
      {name: 'clear', summary: '清除远程配置；--scope session 只清会话级'},
    ],
  },
  {
    name: 'resume', hint: 'RUN_ID', group: '运行控制',
    summary: '校验检查点后继续指定 Run',
  },
  {
    name: 'approve', hint: 'ACTION_ID [RUN_ID]', group: '运行控制',
    summary: '批准等待中的动作（例如应用补丁）',
  },
  {
    name: 'reject', hint: 'ACTION_ID [RUN_ID]', group: '运行控制',
    summary: '拒绝等待中的动作并保留已产出的产物',
  },
  {
    name: 'pause', hint: '', group: '运行控制',
    summary: '让正在运行的 Agent 在安全边界暂停', requiresRun: true,
  },
  {
    name: 'interrupt', hint: '', group: '运行控制',
    summary: '安全打断正在运行的 Agent 并等待纠正提示', requiresRun: true,
  },
  {
    name: 'cancel', hint: '', group: '运行控制',
    summary: '取消正在运行的 Agent Run', requiresRun: true,
  },
];

export const REGISTRY = new Map(COMMANDS.map(command => [command.name, command]));

export interface Candidate {
  command: CommandSpec;
  /** 0 = 前缀匹配，1 = 子串匹配，2 = 顺序模糊匹配 */
  tier: number;
}

/**
 * 按已输入的命令名片段筛选候选：前缀匹配优先，其次子串，最后按序的模糊匹配兜底。
 * 同一档内按命令名长度再按字典序排序，保证结果稳定。
 */
export function matchCommands(fragment: string, commands: CommandSpec[] = COMMANDS): Candidate[] {
  const needle = fragment.toLowerCase();
  // 空片段（刚敲下 /）保持注册表的分组声明顺序，比按长度排序更好读。
  if (!needle) return commands.map(command => ({command, tier: 0}));
  const found: Candidate[] = [];
  for (const command of commands) {
    const name = command.name.toLowerCase();
    if (name.startsWith(needle)) found.push({command, tier: 0});
    else if (name.includes(needle)) found.push({command, tier: 1});
    else if (fuzzy(name, needle)) found.push({command, tier: 2});
  }
  return found.sort((left, right) => left.tier - right.tier ||
    left.command.name.length - right.command.name.length ||
    left.command.name.localeCompare(right.command.name));
}

/** needle 的字符按顺序出现在 haystack 中（不要求连续）。 */
export function fuzzy(haystack: string, needle: string): boolean {
  let index = 0;
  for (const character of needle) {
    index = haystack.indexOf(character, index);
    if (index < 0) return false;
    index += 1;
  }
  return true;
}

/** 子动作筛选，前缀匹配优先。 */
export function matchActions(command: CommandSpec, fragment: string): SubCommand[] {
  if (!command.actions) return [];
  const needle = fragment.toLowerCase();
  // 空片段保持声明顺序（list/show/... 更符合使用频率）；
  // 有片段时前缀匹配优先，同档内仍按声明顺序，避免菜单顺序跳动。
  const order = new Map(command.actions.map((action, index) => [action.name, index]));
  return command.actions
    .filter(action => !needle || action.name.toLowerCase().includes(needle))
    .sort((left, right) => {
      const leftPrefix = left.name.toLowerCase().startsWith(needle) ? 0 : 1;
      const rightPrefix = right.name.toLowerCase().startsWith(needle) ? 0 : 1;
      return leftPrefix - rightPrefix || order.get(left.name)! - order.get(right.name)!;
    });
}

/** Levenshtein 距离，用于未知命令的“最接近命令”建议。 */
export function distance(left: string, right: string): number {
  if (left === right) return 0;
  if (!left.length) return right.length;
  if (!right.length) return left.length;
  let previous = Array.from({length: right.length + 1}, (_, index) => index);
  for (let row = 1; row <= left.length; row++) {
    const current = [row];
    for (let column = 1; column <= right.length; column++) {
      const cost = left[row - 1] === right[column - 1] ? 0 : 1;
      current[column] = Math.min(previous[column] + 1, current[column - 1] + 1, previous[column - 1] + cost);
    }
    previous = current;
  }
  return previous[right.length];
}

/** 返回与输入最接近的命令名（不含斜杠），差得太远时返回空数组。 */
export function suggest(name: string, limit = 3): string[] {
  return COMMANDS
    .map(command => ({name: command.name, score: distance(name.toLowerCase(), command.name.toLowerCase())}))
    .filter(item => item.score > 0 && item.score <= Math.max(2, Math.ceil(name.length / 2)))
    .sort((left, right) => left.score - right.score || left.name.localeCompare(right.name))
    .slice(0, limit)
    .map(item => item.name);
}
