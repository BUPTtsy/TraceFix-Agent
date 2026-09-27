/**
 * 启动横幅与 /help 渲染。
 *
 * 两者都返回字符串数组而不是直接写 stdout，方便单元测试与降级：
 * 非 TTY / NO_COLOR 时 palette(false) 会让样式函数退化为恒等函数。
 */

import {Palette, displayWidth, padEnd, truncate} from './terminal.js';
import {COMMANDS, CommandSpec, GROUPS} from './registry.js';

export interface BannerInfo {
  projectId: string;
  mode: string;
  node: string;
  version: string;
}

/** 启动横幅。窄终端下自动省略装饰线。 */
export function banner(info: BannerInfo, colors: Palette, columns: number): string[] {
  const limit = Math.max(20, columns - 1);
  const title = colors.bold(colors.cyan('TraceFix')) + colors.grey(' CLI v' + info.version);
  const facts = colors.grey('project ') + colors.cyan(info.projectId) +
    colors.grey('  mode ') + colors.cyan(info.mode) +
    colors.grey('  node ') + colors.grey(info.node);
  const tip = colors.grey('输入 ') + colors.bold('/') + colors.grey(' 查看命令，直接输入文字记录目标，') +
    colors.bold('/run') + colors.grey(' 启动。');
  const rule = colors.grey('─'.repeat(Math.min(limit, 56)));
  return [rule, title, facts, tip, rule, ''].map(line =>
    displayWidth(line) > limit ? truncate(line, limit) : line);
}

/** 分组帮助。每个命令一行：名称、参数形态、中文说明。 */
export function help(colors: Palette, columns: number, commands: CommandSpec[] = COMMANDS): string[] {
  const limit = Math.max(24, columns - 1);
  const nameWidth = Math.min(16, Math.max(...commands.map(command => displayWidth('/' + command.name))));
  // 参数形态列与说明列共享剩余宽度：窄终端下先压缩参数列，必要时再截断说明。
  const longestHint = Math.max(...commands.map(command => displayWidth(command.hint)));
  // 说明列至少留一半宽度，避免参数形态过长时把中文说明挤没。
  const hintWidth = Math.max(0, Math.min(34, longestHint, limit - nameWidth - Math.floor(limit / 2)));
  const lines: string[] = [];
  for (const group of GROUPS) {
    const members = commands.filter(command => command.group === group);
    if (!members.length) continue;
    lines.push(colors.bold(colors.magenta(group)));
    for (const command of members) {
      const name = padEnd('/' + command.name, nameWidth);
      const hint = hintWidth ? padEnd(truncate(command.hint, hintWidth), hintWidth) : '';
      const tail = command.summary + (command.requiresRun ? '（需有 Agent 在运行）' : '');
      const used = 2 + nameWidth + 1 + displayWidth(hint) + 1;
      lines.push('  ' + colors.cyan(name) + ' ' + colors.yellow(hint) + ' ' +
        colors.grey(truncate(tail, Math.max(0, limit - used))));
    }
    lines.push('');
  }
  lines.push(colors.grey('别名：/scope = /projects，/memory = /knowledge'));
  lines.push(colors.grey('按键：↑↓ 历史或菜单 · Tab 补全 · Esc 关菜单 · Ctrl+A/E 行首行尾 · Ctrl+W 删词 · Ctrl+U 删到行首 · Ctrl+C 打断或清行 · Ctrl+D 退出'));
  return lines;
}

/** 非 TTY 下的纯文本帮助，保持与改造前一致的三行形态。 */
export const PLAIN_HELP = [
  '/projects list|show|use  /knowledge list|show|search|new|import|edit|enable|disable|export',
  '/runs list|show|logs|trace|sources|export|remember|continue  /remote show|set|clear',
  '/mode test|repair|chat  /run  /continue  /status  /chat  /resume  /approve  /reject  /quit',
];
