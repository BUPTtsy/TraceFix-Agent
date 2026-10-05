import React, {useCallback, useEffect, useRef, useState} from 'react';
import {Box, Text, useInput as useInkInput, useStdin, useStdout, type BoxProps} from 'ink';
import stringWidth from 'string-width';
import wrapAnsi from 'wrap-ansi';
import {useDoublePress} from './useDoublePress.js';
import {renderPlaceholder} from './renderPlaceholder.js';
import {normalizeTerminalInput, type TerminalKey} from './terminalInput.js';

export {Box, Text, stringWidth, wrapAnsi, useDoublePress, renderPlaceholder};
export type Key = TerminalKey;
export function useInput(handler: (input: string, key: Key) => void, options: {isActive?: boolean} = {}) {
  const {internal_eventEmitter} = useStdin();
  const rawInput = useRef('');
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => {
    if (options.isActive === false) return;
    const capture = (raw: string | Buffer) => {rawInput.current = String(raw);};
    internal_eventEmitter.prependListener('input', capture);
    return () => {internal_eventEmitter.removeListener('input', capture);};
  }, [internal_eventEmitter, options.isActive]);
  const handleInput = useCallback((input: string, key: Key) => {
    const raw = rawInput.current || input;
    rawInput.current = '';
    for (const event of normalizeTerminalInput(raw, input, key)) handlerRef.current(event.input, event.key);
  }, []);
  useInkInput(handleInput, options);
}
export function useTerminalMouse(enabled = true) {
  const {stdout} = useStdout();
  const {stdin} = useStdin();
  useEffect(() => {
    if (!enabled || !stdout.isTTY || !stdin.isTTY) return;
    let restored = false;
    const restore = () => {
      if (restored) return;
      restored = true;
      stdout.write('\x1b[?1006l\x1b[?1000l');
    };
    stdout.write('\x1b[?1000h\x1b[?1006h');
    process.once('exit', restore);
    return () => {process.off('exit', restore); restore();};
  }, [enabled, stdout, stdin]);
}
export type RGBColor = {r: number; g: number; b: number};
export type RGBColorString = `rgb(${number},${number},${number})`;
export type Theme = Record<string, string>;
export const useTheme = (): [string] => ['dark'];
export const getTheme = (_name?: string) => ({text: 'rgb(230,230,230)', error: 'rgb(171,43,63)', warning: 'rgb(215,158,78)', claude: 'rgb(215,119,87)'});
export type InlineGhostText = {text: string; fullCommand: string; insertPosition: number};
export type ImageDimensions = {width: number; height: number};
export type TextHighlight = {start: number; end: number; dimColor?: boolean};
export type TextInputState = {
  onInput: (input: string, key: Key) => void; renderedValue: string; offset: number;
  setOffset: (offset: number) => void; cursorLine: number; cursorColumn: number;
  viewportCharOffset: number; viewportCharEnd: number;
};
export type BaseInputState = TextInputState;
export type BaseTextInputProps = {
  value: string; onChange: (value: string) => void; focus?: boolean; showCursor?: boolean;
  placeholder?: string; placeholderElement?: React.ReactNode; argumentHint?: string; cursorOffset?: number;
  highlights?: TextHighlight[]; dimColor?: boolean; onIsPastingChange?: (value: boolean) => void;
  onPaste?: (value: string) => void;
  onImagePaste?: (base64Image: string, mediaType?: string, filename?: string, dimensions?: ImageDimensions, sourcePath?: string) => void;
};
export const env = {terminal: process.env.TERM_PROGRAM};
export const isInputModeCharacter = (_input?: string) => false;
export const isFullscreenEnvEnabled = () => true;
export const isModifierPressed = (_modifier?: string) => false;
export const prewarmModifiers = () => {};
export const markBackslashReturnUsed = () => {};
export const addToHistory = () => {};
export const useNotifications = () => ({addNotification: (_value: unknown) => {}, removeNotification: (_key: string) => {}});
export const useDeclaredCursor = (_value: unknown) => undefined;
export const Ansi = ({children}: {children: React.ReactNode}) => <Text>{children}</Text>;
export const NoSelect = ({fromLeftEdge: _fromLeftEdge, children, ...props}: BoxProps & {fromLeftEdge?: boolean; children?: React.ReactNode}) => <Box {...props}>{children}</Box>;
export const Ratchet = ({children}: {children: React.ReactNode; lock?: string}) => <>{children}</>;
export const HighlightedInput = ({text}: {text: string; highlights?: TextHighlight[]}) => <Text>{text}</Text>;
export function usePasteHandler({onInput}: {onInput: (input: string, key: Key) => void; onPaste?: unknown; onImagePaste?: unknown}) {
  return {wrappedOnInput: onInput, isPasting: false};
}
export function useWindowSize(enabled = true) {
  const {stdout} = useStdout();
  const [size, setSize] = useState(() => ({columns: Math.max(20, stdout.columns || 80), rows: Math.max(8, stdout.rows || 24)}));
  useEffect(() => {
    if (!enabled) return;
    const resize = () => setSize({columns: Math.max(20, stdout.columns || 80), rows: Math.max(8, stdout.rows || 24)});
    resize();
    stdout.on('resize', resize);
    return () => {stdout.off('resize', resize);};
  }, [enabled, stdout]);
  return size;
}
