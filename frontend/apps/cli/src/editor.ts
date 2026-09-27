/**
 * TTY 行编辑器：斜杠命令菜单、行内编辑、命令历史与就地重绘。
 *
 * 设计要点：
 * - 渲染出的每一行都截断到 columns - 1 列，保证一行永不折行，
 *   于是"块内行号"与"终端行号"一一对应，重绘时的光标算术是可靠的。
 * - 输入超过可见宽度时做水平滚动，而不是让终端折行。
 * - 所有纯计算（可视窗口、菜单窗口、行格式化、帧拼装）都是导出的纯函数，可单测。
 */

import nodeReadline from 'node:readline';
import fs from 'node:fs';
import path from 'node:path';
import {
  Capabilities, Palette, charWidth, cursor, displayWidth, padEnd, truncate,
} from './terminal.js';
import {Candidate, CommandSpec, REGISTRY, matchActions, matchCommands, SubCommand} from './registry.js';

/* ------------------------------------------------------------------ 纯函数 */

export interface Viewport {
  /** 可见的输入文本片段 */
  text: string;
  /** 光标相对该片段起点的列偏移 */
  column: number;
  /** 左右两侧是否还有被裁掉的内容 */
  clippedLeft: boolean;
  clippedRight: boolean;
}

/**
 * 计算输入的水平可视窗口。chars 是按码位切分的数组，cursor 是码位下标。
 * 窗口右端锚定光标：光标右侧永远可见，超长时左侧被裁掉。
 */
export function viewport(chars: string[], cursorIndex: number, available: number): Viewport {
  if (available <= 0) return {text: '', column: 0, clippedLeft: cursorIndex > 0, clippedRight: false};
  const widths = chars.map(charWidth);
  let start = 0, span = 0;
  for (let index = 0; index < cursorIndex; index++) span += widths[index];
  while (span > available && start < cursorIndex) span -= widths[start++];
  // end 至少推进到光标处，否则光标左侧的可见内容会被整段丢掉。
  let end = cursorIndex, used = span;
  while (end < chars.length && used + widths[end] <= available) used += widths[end++];
  return {
    text: chars.slice(start, end).join(''),
    column: span,
    clippedLeft: start > 0,
    clippedRight: end < chars.length,
  };
}

/** 保证 selected 落在可见窗口内的滚动窗口。 */
export function menuWindow(total: number, selected: number, maxVisible: number): {start: number; end: number} {
  if (total <= maxVisible) return {start: 0, end: total};
  let start = Math.min(Math.max(0, selected - Math.floor(maxVisible / 2)), total - maxVisible);
  if (selected < start) start = selected;
  if (selected >= start + maxVisible) start = selected - maxVisible + 1;
  return {start, end: start + maxVisible};
}

export interface MenuRow {
  /** 补全到输入行的文本 */
  completion: string;
  /** 命令名或子动作名 */
  label: string;
  hint: string;
  summary: string;
}

/** 一级命令候选转菜单行。 */
export function commandRows(candidates: Candidate[]): MenuRow[] {
  return candidates.map(({command}) => ({
    completion: '/' + command.name,
    label: '/' + command.name,
    hint: command.hint,
    summary: command.aliasOf ? command.summary : command.summary,
  }));
}

/** 子动作候选转菜单行。 */
export function actionRows(command: CommandSpec, actions: SubCommand[]): MenuRow[] {
  return actions.map(action => ({
    completion: '/' + command.name + ' ' + action.name,
    label: action.name,
    hint: '',
    summary: action.summary,
  }));
}

/**
 * 决定当前输入该显示哪种菜单。返回 null 表示不显示菜单。
 * 只在"输入以 / 开头、且还停留在命令名或第一个子动作上"时给候选。
 */
export function menuFor(text: string): {rows: MenuRow[]; kind: 'command' | 'action'} | null {
  if (!text.startsWith('/')) return null;
  const commandLevel = /^\/(\S*)$/.exec(text);
  if (commandLevel) {
    const rows = commandRows(matchCommands(commandLevel[1]));
    return rows.length ? {rows, kind: 'command'} : null;
  }
  const actionLevel = /^\/(\S+)[ \t]+(\S*)$/.exec(text);
  if (actionLevel) {
    const command = REGISTRY.get(actionLevel[1]);
    if (!command?.actions) return null;
    const rows = actionRows(command, matchActions(command, actionLevel[2]));
    return rows.length ? {rows, kind: 'action'} : null;
  }
  return null;
}

/** 格式化一行菜单，整体截断到 limit 列。 */
export function formatMenuRow(row: MenuRow, labelWidth: number, selected: boolean,
                              colors: Palette, limit: number, hintWidth = 0): string {
  const marker = selected ? '❯ ' : '  ';
  const markerWidth = displayWidth(marker);
  // 窄终端下 label 也要让位，否则补齐后的整行会超出列宽。
  const labelRoom = Math.max(0, limit - markerWidth);
  const label = truncate(padEnd(row.label, Math.min(labelWidth, labelRoom)), labelRoom);
  let used = markerWidth + displayWidth(label);
  const paintedLabel = selected ? colors.bold(colors.cyan(label)) : colors.cyan(label);
  let line = (selected ? colors.cyan(marker) : marker) + paintedLabel;

  // 参数形态单独占一列，让各行的中文说明起始列对齐。
  const hintRoom = Math.max(0, Math.min(hintWidth, limit - used - 1));
  if (hintRoom > 0) {
    const hint = padEnd(truncate(row.hint, hintRoom), hintRoom);
    line += ' ' + colors.yellow(hint);
    used += 1 + displayWidth(hint);
  }
  const summary = truncate(row.summary, Math.max(0, limit - used - 2));
  if (summary) line += '  ' + colors.grey(summary);
  return line;
}

export interface Frame {
  /** 需要写入 stdout 的完整转义序列 */
  output: string;
  /** 本帧占用的终端行数 */
  rows: number;
  /** 光标所在的块内行号 */
  cursorRow: number;
}

/**
 * 拼装一帧：先把光标移回上一帧起点并擦除，再写新内容，最后把光标放到目标位置。
 * previousRows / previousCursorRow 描述上一帧的几何；首帧传 0 / 0。
 */
export function composeFrame(lines: string[], cursorRow: number, cursorColumn: number,
                             previousRows: number, previousCursorRow: number): Frame {
  let output = cursor.hide;
  if (previousRows > 0) output += cursor.up(previousCursorRow) + cursor.column(0) + cursor.eraseDown;
  else output += cursor.column(0) + cursor.eraseDown;
  output += lines.join('\r\n');
  const last = Math.max(0, lines.length - 1);
  output += cursor.up(last - cursorRow) + cursor.column(0) + cursor.right(cursorColumn) + cursor.show;
  return {output, rows: lines.length, cursorRow};
}

/**
 * 历史是否可以落盘。远程配置、密钥样式的内容一律不持久化，
 * 避免把仓库地址、令牌之类写进 .tracefix/cli-history。
 */
export function persistable(line: string): boolean {
  if (!line.trim()) return false;
  if (/^\/(remote|chat)\b/.test(line.trim())) return false;
  return !/(--token|--secret|--password|api[_-]?key|authorization|bearer\s|gh[pousr]_[A-Za-z0-9]|sk-[A-Za-z0-9]{8})/i.test(line);
}

/* --------------------------------------------------------------- 编辑器本体 */

export interface EditorContext {
  projectId: () => string;
  mode: () => string;
  running: () => boolean;
  goal: () => string;
}

export interface EditorHooks {
  /** Ctrl+C 且有 Agent 在跑：中断当前 Run。返回 true 表示已处理。 */
  interrupt: () => boolean;
}

type Key = {name?: string; ctrl?: boolean; meta?: boolean; shift?: boolean; sequence?: string};

const MAX_HISTORY = 500;

export class LineEditor {
  private readonly input = process.stdin;
  private readonly output = process.stdout;
  private chars: string[] = [];
  private cursorIndex = 0;
  private history: string[] = [];
  private historyIndex = -1;
  private draft = '';
  private rows = 0;
  private cursorRow = 0;
  private selected = 0;
  private dismissed = false;
  private active = false;
  private pending: ((line: string | null) => void) | null = null;
  private lastReturn = 0;
  private killRing = '';
  private readonly historyFile: string | null;
  private readonly onKeypress = (character: string, key: Key) => this.handle(character, key || {});
  private readonly onResize = () => { if (this.active) this.render(); };

  constructor(private readonly capabilities: Capabilities,
              private readonly colors: Palette,
              private readonly context: EditorContext,
              private readonly hooks: EditorHooks,
              dataRoot: string) {
    this.historyFile = dataRoot ? path.join(dataRoot, 'cli-history') : null;
    this.loadHistory();
  }

  /* ---------------------------------------------------------- 历史持久化 */

  private loadHistory(): void {
    if (!this.historyFile || !fs.existsSync(this.historyFile)) return;
    try {
      this.history = fs.readFileSync(this.historyFile, 'utf8').split(/\r?\n/)
        .filter(line => line.trim()).slice(-MAX_HISTORY);
    } catch { this.history = []; }
  }

  private saveHistory(line: string): void {
    if (!this.historyFile || !persistable(line)) return;
    try {
      fs.mkdirSync(path.dirname(this.historyFile), {recursive: true});
      fs.appendFileSync(this.historyFile, line + '\n', {encoding: 'utf8', mode: 0o600});
    } catch { /* 历史写不进去不影响交互 */ }
  }

  /* -------------------------------------------------------------- 生命周期 */

  start(): void {
    if (this.active) return;
    this.active = true;
    nodeReadline.emitKeypressEvents(this.input);
    if (this.input.isTTY) this.input.setRawMode(true);
    this.input.setEncoding('utf8');
    this.input.resume();
    this.input.on('keypress', this.onKeypress);
    this.output.on('resize', this.onResize);
  }

  stop(): void {
    if (!this.active) return;
    this.active = false;
    this.input.off('keypress', this.onKeypress);
    this.output.off('resize', this.onResize);
    if (this.input.isTTY) this.input.setRawMode(false);
    this.input.pause();
    this.output.write(cursor.show);
  }

  /** 读一行。返回 null 表示用户要求退出（Ctrl+D / 空行 Ctrl+C）。 */
  read(): Promise<string | null> {
    this.chars = [];
    this.cursorIndex = 0;
    this.historyIndex = -1;
    this.draft = '';
    this.selected = 0;
    this.dismissed = false;
    this.rows = 0;
    this.cursorRow = 0;
    this.render();
    return new Promise(resolve => { this.pending = resolve; });
  }

  /**
   * 让外部输出（Agent 流式输出、错误）插到输入行之前：
   * 先擦掉当前块，写外部内容，再把输入行重绘回来。
   */
  external(write: () => void): void {
    const wasRendered = this.rows > 0;
    if (wasRendered) this.erase();
    write();
    if (wasRendered && this.pending) this.render();
  }

  private erase(): void {
    if (!this.rows) return;
    this.output.write(cursor.up(this.cursorRow) + cursor.column(0) + cursor.eraseDown);
    this.rows = 0;
    this.cursorRow = 0;
  }

  /* ---------------------------------------------------------------- 渲染 */

  private statusLine(limit: number): string {
    const parts = [
      this.colors.grey('project '), this.colors.cyan(this.context.projectId()),
      this.colors.grey('  mode '), this.colors.cyan(this.context.mode()),
    ].join('');
    const state = this.context.running()
      ? this.colors.yellow('● Agent 运行中')
      : this.colors.grey('○ 空闲');
    const goal = this.context.goal().trim();
    const goalPart = goal ? this.colors.grey('  goal ') + this.colors.grey(truncate(goal, 24)) : '';
    const line = parts + '  ' + state + goalPart;
    return displayWidth(line) > limit ? truncate(line, limit) : line;
  }

  private promptText(): string {
    return this.context.running() ? '● ' : '❯ ';
  }

  private render(): void {
    if (!this.capabilities.rich) return;
    const limit = Math.max(8, (this.output.columns || 80) - 1);
    const prompt = this.promptText();
    const promptWidth = displayWidth(prompt);
    const view = viewport(this.chars, this.cursorIndex, Math.max(1, limit - promptWidth - 1));
    const painted = this.colors.cyan(prompt) +
      (view.clippedLeft ? this.colors.grey('«') : '') + view.text +
      (view.clippedRight ? this.colors.grey('»') : '');
    const lines = [this.statusLine(limit), painted];
    const cursorColumn = promptWidth + (view.clippedLeft ? 1 : 0) + view.column;

    const menu = this.dismissed ? null : menuFor(this.currentText());
    if (menu) {
      const maxVisible = Math.max(3, Math.min(8, (this.output.rows || 24) - 4));
      if (this.selected >= menu.rows.length) this.selected = menu.rows.length - 1;
      if (this.selected < 0) this.selected = 0;
      const {start, end} = menuWindow(menu.rows.length, this.selected, maxVisible);
      const labelWidth = Math.min(22, Math.max(...menu.rows.map(row => displayWidth(row.label))));
      // 参数形态列的宽度按当前可见的候选计算，宽度不够时整列省略。
      const widest = Math.max(0, ...menu.rows.slice(start, end).map(row => displayWidth(row.hint)));
      const hintWidth = Math.min(widest, Math.max(0, limit - labelWidth - 26));
      for (let index = start; index < end; index++) {
        lines.push(formatMenuRow(menu.rows[index], labelWidth, index === this.selected,
          this.colors, limit, hintWidth));
      }
      const hidden = menu.rows.length - (end - start);
      lines.push(this.colors.grey(truncate(
        hidden > 0
          ? `  ↑↓ 选择 · Tab/Enter 补全 · Esc 关闭 · 另有 ${hidden} 项`
          : '  ↑↓ 选择 · Tab/Enter 补全 · Esc 关闭', limit)));
    }

    const frame = composeFrame(lines, 1, cursorColumn, this.rows, this.cursorRow);
    this.output.write(frame.output);
    this.rows = frame.rows;
    this.cursorRow = frame.cursorRow;
  }

  private currentText(): string {
    return this.chars.join('');
  }

  /* ---------------------------------------------------------------- 按键 */

  private handle(character: string, key: Key): void {
    if (!this.pending) return;
    const name = key.name || '';

    if (key.ctrl && name === 'c') return this.interrupt();
    if (key.ctrl && name === 'd') {
      if (!this.chars.length) return this.finish(null);
      return this.deleteForward();
    }

    if (name === 'return' || name === 'enter') {
      const now = Date.now();
      if (name === 'enter' && now - this.lastReturn < 20) return;   // Windows 下 CRLF 去重
      this.lastReturn = now;
      return this.submit();
    }
    if (name === 'tab') return this.complete(true);
    if (name === 'escape') { this.dismissed = true; return this.render(); }

    if (name === 'up' || name === 'down') {
      if (!this.dismissed && menuFor(this.currentText())) {
        const rows = menuFor(this.currentText())!.rows.length;
        this.selected = name === 'up'
          ? (this.selected - 1 + rows) % rows
          : (this.selected + 1) % rows;
        return this.render();
      }
      return this.browseHistory(name === 'up' ? -1 : 1);
    }

    if (name === 'left') { if (this.cursorIndex > 0) this.cursorIndex--; return this.render(); }
    if (name === 'right') { if (this.cursorIndex < this.chars.length) this.cursorIndex++; return this.render(); }
    if (name === 'home' || (key.ctrl && name === 'a')) { this.cursorIndex = 0; return this.render(); }
    if (name === 'end' || (key.ctrl && name === 'e')) { this.cursorIndex = this.chars.length; return this.render(); }
    if (name === 'backspace' || (key.ctrl && name === 'h')) return this.deleteBackward();
    if (name === 'delete') return this.deleteForward();
    if (key.ctrl && name === 'u') return this.killToStart();
    if (key.ctrl && name === 'k') return this.killToEnd();
    if (key.ctrl && name === 'w') return this.killWord();
    if (key.ctrl && name === 'y') return this.insert(this.killRing);
    if (key.ctrl && name === 'l') { this.rows = 0; this.cursorRow = 0; this.output.write('\x1b[2J\x1b[H'); return this.render(); }
    if (key.ctrl || key.meta) return;

    if (character && !/[\x00-\x1f\x7f]/.test(character)) return this.insert(character);
  }

  private insert(text: string): void {
    if (!text) return;
    const added = Array.from(text);
    this.chars.splice(this.cursorIndex, 0, ...added);
    this.cursorIndex += added.length;
    this.dismissed = false;
    this.selected = 0;
    this.render();
  }

  private deleteBackward(): void {
    if (this.cursorIndex > 0) this.chars.splice(--this.cursorIndex, 1);
    this.dismissed = false;
    this.render();
  }

  private deleteForward(): void {
    if (this.cursorIndex < this.chars.length) this.chars.splice(this.cursorIndex, 1);
    this.render();
  }

  private killToStart(): void {
    this.killRing = this.chars.slice(0, this.cursorIndex).join('');
    this.chars.splice(0, this.cursorIndex);
    this.cursorIndex = 0;
    this.render();
  }

  private killToEnd(): void {
    this.killRing = this.chars.slice(this.cursorIndex).join('');
    this.chars.length = this.cursorIndex;
    this.render();
  }

  private killWord(): void {
    let index = this.cursorIndex;
    while (index > 0 && /\s/.test(this.chars[index - 1])) index--;
    while (index > 0 && !/\s/.test(this.chars[index - 1])) index--;
    this.killRing = this.chars.slice(index, this.cursorIndex).join('');
    this.chars.splice(index, this.cursorIndex - index);
    this.cursorIndex = index;
    this.render();
  }

  private browseHistory(step: number): void {
    if (!this.history.length) return;
    if (this.historyIndex === -1) {
      if (step > 0) return;
      this.draft = this.currentText();
      this.historyIndex = this.history.length;
    }
    const next = this.historyIndex + step;
    if (next < 0) return;
    if (next >= this.history.length) {
      this.historyIndex = -1;
      this.chars = Array.from(this.draft);
    } else {
      this.historyIndex = next;
      this.chars = Array.from(this.history[next]);
    }
    this.cursorIndex = this.chars.length;
    this.dismissed = true;
    this.render();
  }

  /**
   * 补全高亮项。keepMenu 为 true（Tab）时补全后继续展开下一层子动作；
   * 为 false（Enter）时补全后收起菜单，这样下一次 Enter 一定是提交，
   * 不会在命令/子动作之间无限级联。
   */
  private complete(keepMenu: boolean): void {
    const menu = this.dismissed ? null : menuFor(this.currentText());
    if (!menu) return;
    const row = menu.rows[Math.min(this.selected, menu.rows.length - 1)];
    this.chars = Array.from(row.completion + ' ');
    this.cursorIndex = this.chars.length;
    this.dismissed = !keepMenu;
    this.selected = 0;
    this.render();
  }

  private submit(): void {
    // 菜单开着时 Enter 先补全；但如果已经输入的正是高亮项本身，
    // 就直接提交，避免 /status 这类完整命令需要多按一次回车。
    const menu = this.dismissed ? null : menuFor(this.currentText());
    if (menu) {
      const row = menu.rows[Math.min(this.selected, menu.rows.length - 1)];
      if (row.completion.trim() !== this.currentText().trim()) return this.complete(false);
    }
    const line = this.currentText();
    this.erase();
    if (this.capabilities.rich) {
      this.output.write(this.colors.cyan(this.promptText()) + line + '\n');
    }
    if (line.trim() && this.history[this.history.length - 1] !== line.trim()) {
      this.history.push(line.trim());
      if (this.history.length > MAX_HISTORY) this.history.shift();
      this.saveHistory(line.trim());
    }
    this.finish(line);
  }

  private interrupt(): void {
    if (this.context.running() && this.hooks.interrupt()) {
      this.external(() => this.output.write(this.colors.yellow('已向 Agent 发送打断请求；/cancel 可直接取消。') + '\n'));
      return;
    }
    if (this.chars.length) {
      this.chars = [];
      this.cursorIndex = 0;
      this.dismissed = false;
      this.selected = 0;
      return this.render();
    }
    this.erase();
    this.output.write(this.colors.grey('已退出。') + '\n');
    this.finish(null);
  }

  private finish(line: string | null): void {
    const resolve = this.pending;
    this.pending = null;
    this.rows = 0;
    this.cursorRow = 0;
    resolve?.(line);
  }
}
