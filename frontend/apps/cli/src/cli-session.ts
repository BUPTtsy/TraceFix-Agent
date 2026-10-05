import {randomUUID} from 'node:crypto';
import {EventIntake, type PublicRecord, type TraceFixMessage} from './tracefix-events.js';

export class CliSession {
  readonly id = randomUUID();
  private binding: {scopeId: string; recordId: string} | null = null;

  bindRun(scopeId: string, recordId: string): void {
    if (!scopeId || !recordId) throw new Error('会话绑定需要项目与 Run');
    this.binding = {scopeId, recordId};
  }

  currentRunId(scopeId: string): string | null {
    return this.binding?.scopeId === scopeId ? this.binding.recordId : null;
  }

  clearRun(): void {
    this.binding = null;
  }
}

export function chatMessage(value: PublicRecord, scopeId: string, sessionId: string, sequence: number): TraceFixMessage | null {
  if (value.scope_id !== scopeId || value.session_id !== sessionId) throw new Error('对话事件会话或项目不匹配');
  if (!value.type || value.type === 'chat.ready' || value.type === 'chat.cleared') return null;
  const type = String(value.type);
  const messageId = String(value.message_id || `message-${sequence}`);
  const payload: PublicRecord = {...(value.payload && typeof value.payload === 'object' && !Array.isArray(value.payload) ? value.payload as PublicRecord : value)};
  for (const key of ['type', 'scope_id', 'session_id', 'message_id', 'payload']) delete payload[key];
  payload.message_id = messageId;
  if (value.references) payload.references = value.references;
  const event = {type, scope_id: scopeId, run_id: `chat:${sessionId}:${messageId}`, payload};
  const [message] = new EventIntake(scopeId, event.run_id).accept([event]);
  if (!message) return null;
  if (Array.isArray(payload.references)) message.metadata.references = payload.references;
  return {...message, id: type.startsWith('chat.') ? `chat:${sessionId}:${messageId}` : `chat-event:${sessionId}:${sequence}`};
}
