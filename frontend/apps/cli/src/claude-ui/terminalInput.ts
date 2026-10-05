import type {Key as InkKey} from 'ink';

export type TerminalKey = InkKey & {fn?: boolean; home?: boolean; end?: boolean; wheelUp?: boolean; wheelDown?: boolean};
export type TerminalInput = {input: string; key: TerminalKey};

const mouseSequence = /\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[M[\s\S]{3}/g;

function emptyKey(): TerminalKey {
  return {upArrow: false, downArrow: false, leftArrow: false, rightArrow: false,
    pageDown: false, pageUp: false, return: false, escape: false, ctrl: false,
    shift: false, tab: false, backspace: false, delete: false, meta: false};
}

function mouseInput(sequence: string, key: TerminalKey): TerminalInput {
  const match = /^\x1b\[<(\d+);\d+;\d+([Mm])$/.exec(sequence);
  const button = match ? Number(match[1]) : sequence.charCodeAt(3) - 32;
  const pressed = match ? match[2] === 'M' : true;
  return {input: '', key: {...key, ctrl: false, meta: false, shift: false, escape: false, return: false,
    backspace: false, delete: false, home: false, end: false, upArrow: false, downArrow: false,
    leftArrow: false, rightArrow: false, pageUp: false, pageDown: false, tab: false,
    wheelUp: pressed && (button & 0x43) === 0x40, wheelDown: pressed && (button & 0x43) === 0x41}};
}

function keyboardInput(raw: string, input: string, key: TerminalKey): TerminalInput {
  if (raw === '\b' || raw === '\x1b\b' || raw === '\x7f' || raw === '\x1b\x7f') {
    return {input: '', key: {...key, backspace: true, delete: false, meta: raw.length === 2}};
  }
  if (/^\x1b\[3(?:;\d+)?~$/.test(raw)) return {input: '', key: {...key, delete: true, backspace: false}};
  const navigation = /^\x1b(?:\[(?:1(?:;\d+)?~|7~|H|1;\d+H)|OH)$/.test(raw) ? 'home'
    : /^\x1b(?:\[(?:4~|8~|F|1;\d+F)|OF)$/.test(raw) ? 'end' : undefined;
  if (navigation) return {input: '', key: {...key, [navigation]: true}};
  return {input, key};
}

export function normalizeTerminalInput(raw: string, input: string, key: TerminalKey): TerminalInput[] {
  const matches = [...raw.matchAll(mouseSequence)];
  if (!matches.length) return [keyboardInput(raw, input, key)];
  const events: TerminalInput[] = [];
  const textFragments: string[] = [];
  let offset = 0;
  for (const match of matches) {
    if (match.index > offset) textFragments.push(raw.slice(offset, match.index));
    events.push(mouseInput(match[0], key));
    offset = match.index + match[0].length;
  }
  if (offset < raw.length) textFragments.push(raw.slice(offset));
  const text = textFragments.join('');
  if (text) events.unshift(keyboardInput(text, text, emptyKey()));
  return events;
}
