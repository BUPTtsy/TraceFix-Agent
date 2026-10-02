export type GuidanceConstraints = {
  include_paths?: string[];
  exclude_paths?: string[];
  max_files_changed?: number;
  max_lines_changed?: number;
  allowed_actions?: string[];
};

const CONSTRAINT_KEYS = new Set([
  'include_paths', 'exclude_paths', 'max_files_changed', 'max_lines_changed', 'allowed_actions',
]);
const ACTIONS = new Set(['navigate', 'click', 'type', 'select', 'press', 'observe', 'finish']);

function chineseCount(value: string): number {
  if (/^\d+$/.test(value)) return Number(value);
  const numerals: Record<string, number> = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100,
  };
  let total = 0;
  let current = 0;
  for (const character of value) {
    const number = numerals[character];
    if (number === undefined) throw new Error('无法识别约束中的数量');
    if (number === 10 || number === 100) {
      current = Math.max(current, 1) * number;
      total += current;
      current = 0;
    } else current = current * 10 + number;
  }
  return total + current;
}

function validatePathPattern(value: unknown): string {
  if (typeof value !== 'string' || !value.trim() || value.startsWith('/') || value.startsWith('\\') || value.includes(':') ||
      value.replaceAll('\\', '/').split('/').includes('..'))
    throw new Error('约束路径必须是项目内的相对 glob');
  return value;
}

export function validateGuidanceConstraints(value: unknown): GuidanceConstraints {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('约束必须是 JSON 对象');
  const raw = value as Record<string, unknown>;
  if (Object.keys(raw).some(key => !CONSTRAINT_KEYS.has(key))) throw new Error('约束 JSON 字段无效');
  const result: GuidanceConstraints = {};
  for (const key of ['include_paths', 'exclude_paths'] as const) {
    if (raw[key] === undefined) continue;
    if (!Array.isArray(raw[key]) || raw[key].length > 2000) throw new Error(`${key} 必须是路径数组`);
    result[key] = raw[key].map(validatePathPattern);
  }
  for (const key of ['max_files_changed', 'max_lines_changed'] as const) {
    if (raw[key] === undefined) continue;
    if (typeof raw[key] !== 'number' || !Number.isInteger(raw[key]) || raw[key] < 1) throw new Error(`${key} 必须是正整数`);
    result[key] = raw[key];
  }
  if (raw.allowed_actions !== undefined) {
    if (!Array.isArray(raw.allowed_actions) || raw.allowed_actions.some(action => typeof action !== 'string' || !ACTIONS.has(action)))
      throw new Error('allowed_actions 包含不支持的动作');
    const actions = [...new Set(raw.allowed_actions)];
    if (!actions.includes('navigate') || !actions.includes('finish')) throw new Error('动作约束必须保留 navigate 和 finish');
    result.allowed_actions = actions;
  }
  if (!Object.keys(result).length) throw new Error('必须提供可强制执行的收窄约束');
  return result;
}

export function parseGuidanceConstraints(text: string): GuidanceConstraints {
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return validateGuidanceConstraints(parsed);
    throw new Error('约束必须是 JSON 对象或可识别的路径、文件数、行数限制');
  } catch (error) {
    if (error instanceof SyntaxError) {
      const raw: Record<string, unknown> = {};
      const excluded = text.match(/(?:不要改|禁止修改|不修改|exclude)\s*[:：]?\s*([\w./*?\\-]+)/i);
      const lines = text.match(/(?:不超过|最多|<=)\s*(?:修改|变更|更改)?\s*([0-9零〇一二两三四五六七八九十百]+)\s*行/);
      const files = text.match(/(?:不超过|最多|<=)\s*(?:修改|变更|更改)?\s*([0-9零〇一二两三四五六七八九十百]+)\s*(?:个)?\s*文件/);
      if (excluded) {
        const pattern = excluded[1].replaceAll('\\', '/').replace(/\/+$/, '');
        raw.exclude_paths = [/[?*]/.test(pattern) || /\.[^/]+$/.test(pattern) ? pattern : `${pattern}/**`];
      }
      if (lines) raw.max_lines_changed = chineseCount(lines[1]);
      if (files) raw.max_files_changed = chineseCount(files[1]);
      return validateGuidanceConstraints(raw);
    }
    throw error;
  }
}

export function guidanceUnsafe(text: string): boolean {
  const normalized = text.replace(/(?:不要|不得|不能|禁止)(?:跳过|绕过|关闭|禁用|删除|取消|扩大|提升|忽略)/g, '');
  return /(?:跳过|绕过|关闭|禁用|删除|取消).{0,12}(?:验证|门禁|测试|权限|审批)|(?:扩大|提升|忽略).{0,12}(?:权限|授权)|(?:skip|bypass|disable).{0,20}(?:validation|verification|tests?|approval|policy)/i.test(normalized);
}

export function classifyGuidance(text: string, requestedLevel?: unknown, suppliedConstraints?: unknown) {
  const level = requestedLevel === undefined
    ? (/(?:改目标|更改目标|其实问题在|retarget)/i.test(text) ? 'retarget' : undefined)
    : requestedLevel;
  if (level !== undefined && !['hint', 'constraint', 'retarget'].includes(String(level))) throw new Error('引导级别无效');
  if (level === 'constraint' || (!level && suppliedConstraints !== undefined)) {
    return {level: 'constraint', constraints: suppliedConstraints === undefined
      ? parseGuidanceConstraints(text) : validateGuidanceConstraints(suppliedConstraints)};
  }
  if (level) return {level: String(level), constraints: null};
  try {
    return {level: 'constraint', constraints: parseGuidanceConstraints(text)};
  } catch {
    return {level: 'hint', constraints: null};
  }
}
