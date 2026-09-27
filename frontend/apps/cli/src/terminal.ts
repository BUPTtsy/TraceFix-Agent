/**
 * 终端能力探测、ANSI 配色与显示宽度计算。
 *
 * 这里的函数都是纯函数（除 capabilities 读取 process），便于单元测试：
 * 全角字符按 2 列计算，组合符号按 0 列计算，其余按 1 列。
 */

export interface Capabilities {
  /** stdin 与 stdout 同时是 TTY，且未被 NO_COLOR / TERM=dumb 降级 */
  rich: boolean;
  /** 允许输出 ANSI 颜色 */
  color: boolean;
  columns: number;
  rows: number;
}

export function capabilities(env: Record<string, string | undefined> = process.env,
                            stdin: {isTTY?: boolean} = process.stdin,
                            stdout: {isTTY?: boolean; columns?: number; rows?: number} = process.stdout): Capabilities {
  const tty = Boolean(stdin.isTTY && stdout.isTTY);
  const degraded = Boolean(env.NO_COLOR) || env.TERM === 'dumb' || env.TRACEFIX_CLI_PLAIN === '1';
  return {
    rich: tty && !degraded,
    color: tty && !degraded,
    columns: Math.max(20, stdout.columns || 80),
    rows: Math.max(4, stdout.rows || 24),
  };
}

/** 显示宽度为 2 的码位区间（East Asian Wide / Fullwidth 的安全子集）。 */
const WIDE: ReadonlyArray<readonly [number, number]> = [
  [0x1100, 0x115f], [0x2e80, 0x303e], [0x3041, 0x33ff], [0x3400, 0x4dbf],
  [0x4e00, 0x9fff], [0xa000, 0xa4cf], [0xa960, 0xa97f], [0xac00, 0xd7a3],
  [0xf900, 0xfaff], [0xfe10, 0xfe19], [0xfe30, 0xfe6f], [0xff00, 0xff60],
  [0xffe0, 0xffe6], [0x16fe0, 0x16fe4], [0x17000, 0x18aff], [0x1b000, 0x1b16f],
  [0x1f004, 0x1f004], [0x1f0cf, 0x1f0cf], [0x1f18e, 0x1f18e], [0x1f191, 0x1f19a],
  [0x1f200, 0x1f320], [0x1f32d, 0x1f335], [0x1f337, 0x1f37c], [0x1f37e, 0x1f393],
  [0x1f3a0, 0x1f3ca], [0x1f3cf, 0x1f3d3], [0x1f3e0, 0x1f3f0], [0x1f3f8, 0x1f43e],
  [0x1f440, 0x1f440], [0x1f442, 0x1f4fc], [0x1f4ff, 0x1f53d], [0x1f54b, 0x1f54e],
  [0x1f550, 0x1f567], [0x1f5fb, 0x1f64f], [0x1f680, 0x1f6c5], [0x1f6d0, 0x1f6d2],
  [0x1f7e0, 0x1f7eb], [0x1f90d, 0x1f9ff], [0x20000, 0x3fffd],
];

/** 宽度为 0 的码位区间（组合符号、零宽字符、变体选择符）。 */
const ZERO: ReadonlyArray<readonly [number, number]> = [
  [0x0300, 0x036f], [0x0483, 0x0489], [0x0591, 0x05bd], [0x0610, 0x061a],
  [0x064b, 0x065f], [0x0670, 0x0670], [0x06d6, 0x06dc], [0x0e31, 0x0e31],
  [0x0e34, 0x0e3a], [0x0e47, 0x0e4e], [0x200b, 0x200f], [0x2028, 0x202e],
  [0x20d0, 0x20ff], [0xfe00, 0xfe0f], [0xfe20, 0xfe2f], [0xfeff, 0xfeff],
];

function inside(ranges: ReadonlyArray<readonly [number, number]>, code: number): boolean {
  for (const [low, high] of ranges) if (code >= low && code <= high) return true;
  return false;
}

/** 单个码位的终端显示列数。 */
export function charWidth(character: string): number {
  const code = character.codePointAt(0);
  if (code === undefined) return 0;
  if (code < 32 || (code >= 0x7f && code < 0xa0)) return 0;
  if (inside(ZERO, code)) return 0;
  if (inside(WIDE, code)) return 2;
  return 1;
}

/** 字符串的终端显示列数（先剥离 ANSI 转义）。 */
export function displayWidth(text: string): number {
  let total = 0;
  for (const character of Array.from(stripAnsi(text))) total += charWidth(character);
  return total;
}

const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b[PX^_].*?\x1b\|\x1b\]8;;.*?(\x1b\|\x07)|\x1b[@-Z\-_]/g;

export function stripAnsi(text: string): string {
  return text.replace(ANSI, '');
}

/** 按显示宽度截断，超出时以 … 结尾。传入的文本不应包含 ANSI。 */
export function truncate(text: string, limit: number): string {
  if (limit <= 0) return '';
  if (displayWidth(text) <= limit) return text;
  let result = '', used = 0;
  for (const character of Array.from(text)) {
    const width = charWidth(character);
    if (used + width > limit - 1) break;
    result += character;
    used += width;
  }
  return result + '…';
}

/** 按显示宽度右侧补空格。 */
export function padEnd(text: string, limit: number): string {
  const missing = limit - displayWidth(text);
  return missing > 0 ? text + ' '.repeat(missing) : text;
}

type Paint = (text: string) => string;

export interface Palette {
  reset: Paint; bold: Paint; dim: Paint; inverse: Paint;
  cyan: Paint; green: Paint; yellow: Paint; red: Paint; magenta: Paint; blue: Paint; grey: Paint;
}

const plain: Paint = text => text;

function code(open: string): Paint {
  return text => '\x1b[' + open + 'm' + text + '\x1b[0m';
}

export function palette(color: boolean): Palette {
  if (!color) {
    return {reset: plain, bold: plain, dim: plain, inverse: plain, cyan: plain, green: plain,
      yellow: plain, red: plain, magenta: plain, blue: plain, grey: plain};
  }
  return {
    reset: plain, bold: code('1'), dim: code('2'), inverse: code('7'),
    cyan: code('36'), green: code('32'), yellow: code('33'), red: code('31'),
    magenta: code('35'), blue: code('34'), grey: code('90'),
  };
}

export interface Segment {
  text: string;
  style?: Paint;
}

/** 把若干段拼成一行，整体按显示宽度截断到 limit，逐段套用样式。 */
export function renderSegments(segments: Segment[], limit: number): string {
  let result = '', used = 0;
  for (const segment of segments) {
    if (used >= limit) break;
    const visible = truncate(segment.text, limit - used);
    if (!visible) continue;
    used += displayWidth(visible);
    result += segment.style ? segment.style(visible) : visible;
  }
  return result;
}

export const cursor = {
  up: (count: number) => count > 0 ? '\x1b[' + count + 'A' : '',
  down: (count: number) => count > 0 ? '\x1b[' + count + 'B' : '',
  right: (count: number) => count > 0 ? '\x1b[' + count + 'C' : '',
  column: (index: number) => '\x1b[' + (index + 1) + 'G',
  eraseDown: '\x1b[J',
  hide: '\x1b[?25l',
  show: '\x1b[?25h',
};
