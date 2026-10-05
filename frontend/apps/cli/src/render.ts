import {COMMANDS, GROUPS} from './registry.js';

export function help(): string[] {
  const lines: string[] = [];
  for (const group of GROUPS) {
    const commands = COMMANDS.filter(command => command.group === group);
    if (!commands.length) continue;
    lines.push(group);
    for (const command of commands) {
      lines.push('  /' + command.name + ' ' + command.hint + ' · ' + command.summary +
        (command.requiresRun ? '（需有 Agent 在运行）' : ''));
    }
    lines.push('');
  }
  lines.push('别名：/scope = /projects，/memory = /knowledge');
  lines.push('按键：↑↓ 历史 · Tab 补全 · Ctrl+C/Esc 打断 · Ctrl+S 选择复制 · 滚轮/PgUp/PgDn 浏览 · Ctrl+O 公开详情');
  return lines;
}

export const PLAIN_HELP = [
  '/projects list|show|use  /knowledge list|show|search|new|import|edit|enable|disable|export',
  '/runs list|show|logs|trace|sources|export|remember|continue  /remote show|set|clear',
  '/mode test|repair|chat  /run（兼容入口）  /continue  /status  /chat  /resume  /approve  /reject  /quit',
];
