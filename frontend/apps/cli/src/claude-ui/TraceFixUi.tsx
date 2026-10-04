import React, {useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useApp, useInput} from 'ink';
import chalk from 'chalk';
import {matchCommands} from '../registry.js';
import type {TraceFixMessage} from '../tracefix-events.js';
import {BaseTextInput} from './BaseTextInput.js';
import {useTextInput} from './useTextInput.js';
import {MessageResponse} from './MessageResponse.js';
import {InterruptedByUser} from './InterruptedByUser.js';
import {SpinnerGlyph} from './SpinnerGlyph.js';
import {useWindowSize} from './adapter.js';
import {TerminalSizeContext} from './TerminalSizeContext.js';

export interface TraceFixUiState {projectId: string; mode: string; goal: string; running: boolean}
export interface TraceFixUiProps {
  state: () => TraceFixUiState;
  initialMessages?: TraceFixMessage[];
  onSubmit: (value: string) => boolean | Promise<boolean>;
  onCancel: () => boolean;
  registerOutput: (push: (message: TraceFixMessage) => void) => void;
}

function wrapLines(value: string, width: number): string[] {
  return value.split('\n').flatMap(line => {
    if (!line) return [''];
    const chunks: string[] = [];
    for (let offset = 0; offset < line.length; offset += width) chunks.push(line.slice(offset, offset + width));
    return chunks;
  });
}

function messageLines(message: TraceFixMessage, width: number, expanded: boolean): string[] {
  const lines = message.kind === 'cancel' ? ['Interrupted · What should TraceFix do instead?', message.text] : wrapLines(message.text, width);
  if (message.ref) lines.push(...wrapLines(`  ref: ${message.ref}`, width));
  if (Object.keys(message.metadata).length) lines.push(...wrapLines(JSON.stringify(message.metadata), width));
  if (expanded && message.event) lines.push(...JSON.stringify(message.event.payload, null, 2).split('\n'));
  return lines;
}

function MessageLine({message, expanded, width, start = 0, count}: {message: TraceFixMessage; expanded: boolean; width: number; start?: number; count?: number}) {
  const color = message.kind === 'error' ? 'red' : message.kind === 'tool' ? 'yellow' : message.kind === 'cancel' ? 'gray' : undefined;
  const lines = messageLines(message, width, expanded).slice(start, count === undefined ? undefined : start + count);
  const interrupted = message.kind === 'cancel' && start === 0 && lines[0]?.startsWith('Interrupted');
  return <Box flexDirection="column"><MessageResponse>{interrupted ? <>{<InterruptedByUser />}{lines.length > 1 && <Text color={color}>{'\n' + lines.slice(1).join('\n')}</Text>}</> : <Text color={color}>{lines.join('\n')}</Text>}</MessageResponse></Box>;
}

export function TraceFixUi({state, initialMessages = [], onSubmit, onCancel, registerOutput}: TraceFixUiProps) {
  const {exit} = useApp();
  const size = useWindowSize();
  const [input, setInput] = useState('');
  const [offset, setOffset] = useState(0);
  const [messages, setMessages] = useState(initialMessages);
  const [scroll, setScroll] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [historyIndex, setHistoryIndex] = useState(-1);
  const history = useRef<string[]>([]);
  const outputCounter = useRef(0);
  const current = state();
  useEffect(() => {const clock = setInterval(() => setNow(Date.now()), 120); return () => clearInterval(clock);}, []);
  useEffect(() => registerOutput(message => setMessages(prev => [...prev, message])), [registerOutput]);
  const suggestions = useMemo(() => input.startsWith('/') && !input.includes(' ') ? matchCommands(input.slice(1)).slice(0, 6) : [], [input]);
  const viewport = Math.max(3, size.rows - 9);
  const rowWidth = Math.max(10, size.columns - 4);
  const messageRows = (message: TraceFixMessage) => messageLines(message, rowWidth, expanded).length;
  const visible: Array<{message: TraceFixMessage; start: number; count: number}> = [];
  let consumed = 0;
  for (let index = messages.length - 1; index >= 0 && consumed < scroll + viewport; index--) {
    const rows = messageRows(messages[index]);
    const start = Math.max(0, scroll - consumed);
    const end = Math.min(rows, scroll + viewport - consumed);
    if (start < end) visible.unshift({message: messages[index], start, count: end - start});
    consumed += rows;
  }
  const appendCancel = () => {const requested = onCancel(); outputCounter.current += 1; setMessages(prev => [...prev, {id: `cancel-${outputCounter.current}`, kind: 'cancel', text: requested ? '已向 Agent 发送打断请求；/cancel 可直接取消。' : '当前没有正在执行的 Run。', metadata: {}}]);};
  const accept = async (value: string) => {if (!value.trim()) return; history.current.push(value); setHistoryIndex(-1); setInput(''); setOffset(0); setScroll(0); if (!await onSubmit(value)) exit();};
  const selectHistory = (direction: number) => {const index = Math.max(-1, Math.min(history.current.length - 1, historyIndex + direction)); setHistoryIndex(index); const value = index < 0 ? '' : history.current[history.current.length - 1 - index]; setInput(value); setOffset(value.length);};
  const inputState = useTextInput({value: input, onChange: setInput, onSubmit: value => {void accept(value);}, onExit: () => {void accept('/quit');}, cursorChar: ' ', invert: chalk.inverse, themeText: value => value, columns: Math.max(10, size.columns - 4), externalOffset: offset, onOffsetChange: setOffset, multiline: true, disableEscapeDoublePress: true, onHistoryUp: () => selectHistory(1), onHistoryDown: () => selectHistory(-1), inputFilter: (value, key) => ((key.ctrl && ['c', 'l', 'o'].includes(value)) || key.pageUp || key.pageDown || (key.shift && (key.upArrow || key.downArrow)) || key.tab) ? '' : value});
  useInput((value, key) => {if (key.ctrl && value === 'c') {if (current.running) appendCancel(); else if (input) {setInput(''); setOffset(0);} else void accept('/quit'); return;} if (key.ctrl && value === 'l') {setMessages([]); setScroll(0); return;} if (key.ctrl && value === 'o') {setExpanded(previous => !previous); return;} if (key.escape && current.running) {appendCancel(); return;} if (key.pageUp || (key.shift && key.upArrow)) {setScroll(previous => previous + viewport); return;} if (key.pageDown || (key.shift && key.downArrow)) {setScroll(previous => Math.max(0, previous - viewport)); return;} if (key.tab && suggestions.length) {const value = '/' + suggestions[0].command.name + ' '; setInput(value); setOffset(value.length);}});
  return <TerminalSizeContext.Provider value={size}><Box flexDirection="column" width={size.columns}><Box borderStyle="round" borderColor="gray" flexDirection="column" paddingX={1}><Text bold color="cyan">TraceFix · {current.projectId} · {current.mode}</Text><Text dimColor>{current.goal || '输入目标，然后 /run'}</Text></Box><Box flexDirection="column" height={viewport} overflow="hidden">{visible.map(({message, start, count}) => <MessageLine key={`${message.id}-${start}`} message={message} expanded={expanded} width={rowWidth} start={start} count={count} />)}</Box>{current.running && <Box><SpinnerGlyph frame={Math.floor(now / 120)} messageColor="text" /><Text>TraceFix is working…</Text></Box>}{suggestions.length > 0 && <Text dimColor>{suggestions.map(item => `/${item.command.name}`).join('  ')}</Text>}<Box><Text color="cyan">❯ </Text><BaseTextInput value={input} onChange={setInput} inputState={inputState} terminalFocus={true} focus={true} showCursor={true} cursorOffset={offset} placeholder="输入目标或 /help" /></Box><Text dimColor>Enter 提交 · Ctrl+C/Esc 打断 · PgUp/PgDn 滚动 · ↑↓ 历史 · Tab 补全 · Ctrl+L 清屏</Text></Box></TerminalSizeContext.Provider>;
}

export {InterruptedByUser};
