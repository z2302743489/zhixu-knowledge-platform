import type { Role } from './types';

let currentRole: Role = 'admin';
export function setApiRole(role: Role) { currentRole = role; }
function errorText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(item => typeof item === 'object' && item && 'msg' in item ? String(item.msg) : String(item)).join('；');
  return '请求失败，请稍后重试';
}
export async function request<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set('X-Role', currentRole);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(`/api${path}`, { ...options, headers, credentials: 'same-origin' });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body ? errorText(body.detail ?? body.message ?? body) : `服务返回 ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const api = {
  get: <T = unknown>(path: string) => request<T>(path),
  post: <T = unknown>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T = unknown>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T = unknown>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: (path: string) => request(path, { method: 'DELETE' }),
  upload: <T = unknown>(path: string, body: FormData) => request<T>(path, { method: 'POST', body }),
};
export function itemsOf<T>(value: unknown, key = 'items'): T[] {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (Array.isArray(record[key])) return record[key] as T[];
    if (Array.isArray(record.items)) return record.items as T[];
  }
  return [];
}
export async function openOriginal(documentId: string, version?: string) {
  const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/file${version ? `?version=${encodeURIComponent(version)}` : ''}`, { headers: { 'X-Role': currentRole }, credentials: 'same-origin' });
  if (!response.ok) {
    const result = await response.json().catch(() => null);
    throw new Error(errorText(result?.detail ?? `无法打开原文件（${response.status}）`));
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a'); link.href = url; link.target = '_blank'; link.rel = 'noopener'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
export async function streamChat(body: unknown, signal: AbortSignal, onEvent: (event: string, data: Record<string, unknown>) => void) {
  const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Role': currentRole }, credentials: 'same-origin', body: JSON.stringify(body), signal });
  if (!response.ok) {
    const result = await response.json().catch(() => null);
    throw new Error(errorText(result?.detail ?? `对话请求失败（${response.status}）`));
  }
  if (!response.body) throw new Error('当前浏览器无法读取流式响应');
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = '';
  const dispatch = (block: string) => {
    let event = 'message'; const lines: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      if (line.startsWith('data:')) lines.push(line.slice(5).trimStart());
    }
    if (!lines.length || lines.join('\n') === '[DONE]') return;
    try { onEvent(event, JSON.parse(lines.join('\n'))); }
    catch (error) { if (error instanceof SyntaxError) throw new Error('服务返回了无法解析的对话事件'); throw error; }
  };
  try {
    while (true) {
      const next = await reader.read();
      pending += decoder.decode(next.value, { stream: !next.done }).replace(/\r\n/g, '\n');
      let separator: number;
      while ((separator = pending.indexOf('\n\n')) >= 0) { dispatch(pending.slice(0, separator)); pending = pending.slice(separator + 2); }
      if (next.done) { if (pending.trim()) dispatch(pending); break; }
    }
  } finally { reader.releaseLock(); }
}
