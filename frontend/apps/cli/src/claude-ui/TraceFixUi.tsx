import React, {useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useApp} from 'ink';
import chalk from 'chalk';
import {matchCommands} from '../registry.js';
import type {TraceFixMessage} from '../tracefix-events.js';
import {BaseTextInput} from './BaseTextInput.js';
import {useTextInput} from './useTextInput.js';
import {MessageResponse} from './MessageResponse.js';
import {InterruptedByUser} from './InterruptedByUser.js';
import {SpinnerGlyph} from './SpinnerGlyph.js';
import {useInput, useTerminalMouse, useWindowSize} from './adapter.js';
import {TerminalSizeContext} from './TerminalSizeContext.js';
import {appendUiMessage, messageLines, selectUiMessages, type UiMessage, visibleMessageRows} from './presentation.js';

export interface TraceFixUiState {projectId: string; mode: string; goal: string; running: boolean; chatting?: boolean}
export interface TraceFixUiProps {
  state: () => TraceFixUiState;
  initialMessages?: TraceFixMessage[];
  onSubmit: (value: string) => boolean | Promise<boolean>;
  onCancel: () => boolean;
  registerOutput: (push: (message: TraceFixMessage) => void) => void;
}

function MessageLine({message, expanded, width, start = 0, count}: {message: UiMessage; expanded: boolean; width: number; start?: number; count?: number}) {
  const payload = message.event?.payload || {};
  const color = message.kind === 'error' || payload.passed === false ? 'red' : message.kind === 'tool' ? 'yellow' : message.kind === 'cancel' ? 'gray' : undefined;
  const lines = messageLines(message, width, expanded).slice(start, count === undefined ? undefined : start + count);
  const interrupted = message.kind === 'cancel' && start === 0 && lines[0]?.startsWith('Interrupted');
  const plainOutput = message.event?.type === 'cli.output' || message.event?.type === 'cli.error' || message.event?.type?.startsWith('chat.');
  const body = interrupted ? <>{<InterruptedByUser />}{lines.length > 1 && <Text color={color}>{'\n' + lines.slice(1).join('\n')}</Text>}</> : <Text color={color}>{lines.join('\n')}</Text>;
  return <Box flexDirection="column">{plainOutput ? body : <MessageResponse>{body}</MessageResponse>}</Box>;
}

export function TraceFixUi({state, initialMessages = [], onSubmit, onCancel, registerOutput}: TraceFixUiProps) {
  const {exit} = useApp();
  const [selectionMode, setSelectionMode] = useState(false);
  const selectionModeRef = useRef(false);
  const pendingSelectionMessages = useRef<TraceFixMessage[]>([]);
  const frozenView = useRef<{messages: UiMessage[]; current: TraceFixUiState; size: {columns: number; rows: number}; scroll: number; expanded: boolean; now: number} | null>(null);
  const size = useWindowSize(!selectionMode);
  const [input, setInput] = useState('');
  const [offset, setOffset] = useState(0);
  const [messages, setMessages] = useState<UiMessage[]>(initialMessages);
  const [scroll, setScroll] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [historyIndex, setHistoryIndex] = useState(-1);
  const history = useRef<string[]>([]);
  const outputCounter = useRef(0);
  const current = state();
  useTerminalMouse(!selectionMode);
  useEffect(() => {
    if (selectionMode) return;
    const clock = setInterval(() => {if (!selectionModeRef.current) setNow(Date.now());}, 120);
    return () => clearInterval(clock);
  }, [selectionMode]);
  useEffect(() => registerOutput(message => {
    if (selectionModeRef.current) {
      pendingSelectionMessages.current.push(message);
      return;
    }
    setMessages(prev => appendUiMessage(prev, message));
  }), [registerOutput]);
  const suggestions = useMemo(() => input.startsWith('/') && !input.includes(' ') ? matchCommands(input.slice(1)).slice(0, 6) : [], [input]);
  const enterSelectionMode = () => {
    if (selectionModeRef.current) return;
    frozenView.current = {messages, current, size, scroll, expanded, now};
    selectionModeRef.current = true;
    setSelectionMode(true);
  };
  const leaveSelectionMode = () => {
    if (!selectionModeRef.current) return;
    const queued = pendingSelectionMessages.current.splice(0);
    selectionModeRef.current = false;
    frozenView.current = null;
    setSelectionMode(false);
    if (queued.length) setMessages(prev => queued.reduce((next, message) => appendUiMessage(next, message), prev));
  };
  const displayedView = selectionMode && frozenView.current ? frozenView.current : {messages, current, size, scroll, expanded, now};
  const displayedMessages = useMemo(() => selectUiMessages(displayedView.messages, displayedView.expanded), [displayedView.messages, displayedView.expanded]);
  const displayedCurrent = displayedView.current;
  const displayedSize = displayedView.size;
  const displayedScroll = displayedView.scroll;
  const displayedExpanded = displayedView.expanded;
  const displayedNow = displayedView.now;
  const viewport = Math.max(3, displayedSize.rows - 9);
  const rowWidth = Math.max(10, displayedSize.columns - 8);
  const totalRows = useMemo(() => displayedMessages.reduce((total, message) => total + messageLines(message, rowWidth, displayedExpanded).length, 0), [displayedMessages, rowWidth, displayedExpanded]);
  const maxScroll = Math.max(0, totalRows - viewport);
  const visible = useMemo(() => visibleMessageRows(displayedMessages, rowWidth, displayedExpanded, displayedScroll, viewport), [displayedMessages, rowWidth, displayedExpanded, displayedScroll, viewport]);
  const previousRows = useRef(totalRows);
  useEffect(() => {
    const added = totalRows - previousRows.current;
    previousRows.current = totalRows;
    setScroll(previous => Math.max(0, Math.min(maxScroll, previous > 0 && added > 0 ? previous + added : previous)));
  }, [totalRows, maxScroll]);
  const appendCancel = () => {const requested = onCancel(); outputCounter.current += 1; setMessages(prev => appendUiMessage(prev, {id: `cancel-${outputCounter.current}`, kind: 'cancel', text: requested ? current.chatting ? '已请求取消当前对话。' : '已向 Agent 发送打断请求；/cancel 可直接取消。' : '当前没有正在执行的 Run。', metadata: {}}));};
  const accept = async (value: string) => {if (!value.trim()) return; history.current.push(value); setHistoryIndex(-1); setInput(''); setOffset(0); setScroll(0); if (!await onSubmit(value)) exit();};
  const selectHistory = (direction: number) => {const index = Math.max(-1, Math.min(history.current.length - 1, historyIndex + direction)); setHistoryIndex(index); const value = index < 0 ? '' : history.current[history.current.length - 1 - index]; setInput(value); setOffset(value.length);};
  const inputState = useTextInput({value: input, onChange: setInput, onSubmit: value => {void accept(value);}, onExit: () => {void accept('/quit');}, cursorChar: ' ', invert: chalk.inverse, themeText: value => value, columns: Math.max(10, displayedSize.columns - 4), externalOffset: offset, onOffsetChange: setOffset, multiline: true, disableEscapeDoublePress: true, onHistoryUp: () => selectHistory(1), onHistoryDown: () => selectHistory(-1), inputFilter: (value, key) => ((key.ctrl && ['c', 'l', 'o', 's'].includes(value)) || key.pageUp || key.pageDown || (key.shift && (key.upArrow || key.downArrow)) || key.tab) ? '' : value});
  useInput((value, key) => {
    if (selectionMode) {
      if ((key.ctrl && value === 's') || key.escape) leaveSelectionMode();
      return;
    }
    if (key.ctrl && value === 's') {enterSelectionMode(); return;}
    if (key.ctrl && value === 'c') {if (displayedCurrent.running) appendCancel(); else if (input) {setInput(''); setOffset(0);} else void accept('/quit'); return;} if (key.ctrl && value === 'l') {setMessages([]); setScroll(0); return;} if (key.ctrl && value === 'o') {setExpanded(previous => !previous); setScroll(0); return;} if (key.escape && displayedCurrent.running) {appendCancel(); return;} if (key.wheelUp) {setScroll(previous => Math.min(maxScroll, previous + 3)); return;} if (key.wheelDown) {setScroll(previous => Math.max(0, previous - 3)); return;} if (key.pageUp || (key.shift && key.upArrow)) {setScroll(previous => Math.min(maxScroll, previous + viewport)); return;} if (key.pageDown || (key.shift && key.downArrow)) {setScroll(previous => Math.max(0, previous - viewport)); return;} if (key.tab && suggestions.length) {const value = '/' + suggestions[0].command.name + ' '; setInput(value); setOffset(value.length);}
  });
  return <TerminalSizeContext.Provider value={displayedSize}><Box flexDirection="column" width={displayedSize.columns}><Box borderStyle="round" borderColor="gray" flexDirection="column" paddingX={1}><Text bold color="cyan">TraceFix · {displayedCurrent.projectId} · {displayedCurrent.mode}</Text><Text dimColor>{displayedCurrent.mode === 'chat' ? '直接输入消息开始对话 · /resume RUN_ID 恢复历史 Run' : displayedCurrent.goal || '直接输入目标启动 Run'}</Text></Box><Box flexDirection="column" height={viewport} overflow="hidden">{visible.map(({message, start, count}) => <MessageLine key={`${message.id}-${start}`} message={message} expanded={displayedExpanded} width={rowWidth} start={start} count={count} />)}</Box>{displayedCurrent.running && <Box><SpinnerGlyph frame={Math.floor(displayedNow / 120)} messageColor="text" /><Text>{displayedCurrent.chatting ? '正在回答…' : '正在执行…'}</Text></Box>}{suggestions.length > 0 && <Text dimColor>{suggestions.map(item => `/${item.command.name}`).join('  ')}</Text>}<Box><Text color="cyan">❯ </Text><BaseTextInput value={input} onChange={setInput} inputState={inputState} terminalFocus={true} focus={!selectionMode} showCursor={!selectionMode} cursorOffset={offset} placeholder={displayedCurrent.mode === 'chat' ? '输入消息或 /help' : '输入目标或 /help'} /></Box><Text dimColor>{selectionMode ? '选择模式：拖拽选中 · Ctrl+Shift+C/右键复制 · Ctrl+S/Esc 返回' : 'Enter 提交 · Ctrl+C/Esc 打断 · Ctrl+S 选择复制 · 滚轮/PgUp/PgDn 浏览 · Ctrl+O 公开详情 · ↑↓ 历史 · Tab 补全'}</Text></Box></TerminalSizeContext.Provider>;
}

export {InterruptedByUser};
