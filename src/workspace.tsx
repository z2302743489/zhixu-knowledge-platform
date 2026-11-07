import { createContext, useContext } from 'react';
import type { Bootstrap, Page, Role } from './types';
export interface WorkspaceContext {
  data: Bootstrap; role: Role; refresh: () => Promise<void>; go: (page: Page) => void;
  run: <T>(task: () => Promise<T>, success?: string) => Promise<T | undefined>;
}
export const Workspace = createContext<WorkspaceContext | null>(null);
export function useWorkspace() { const value = useContext(Workspace); if (!value) throw new Error('Workspace unavailable'); return value; }
export const errorText = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试';
export const emptyData: Bootstrap = { kbs: [], models: [], assistants: [], sessions: [], logs: [], ocr: { configured: false }, mode: 'live' };
export const roleNames: Record<Role, string> = { admin: '管理员', manager: '知识库管理人员', user: '使用者' };
export const statusLabel = (value?: string) => ({ completed: '已完成', cancelled: '已停止', failed: '失败', running: '进行中', streaming: '正在回答', pending: '等待回答', success: '成功', queued: '排队中', processing: '处理中', ready: '已就绪', preview: '待确认构建', disabled: '已停用' }[value || ''] || value || '');

export function formatTime(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false }); }
