export type Role = 'admin' | 'manager' | 'user';
export type Page = 'overview' | 'knowledge' | 'assistants' | 'chat' | 'models' | 'logs';
export interface Chunk { id: string; text: string; location: string; enabled: boolean; version?: string }
export interface KnowledgeDocument { id: string; name: string; kind: string; status: string; chunks: Chunk[]; size: string; date: string; error?: string; chunkCount?: number; totalChunks?: number; activeVersion?: string }
export interface KnowledgeBase { id: string; name: string; description: string; category: string; color: string; model: string; enabled: boolean; docs: KnowledgeDocument[]; documentCount?: number; totalDocs?: number; chunkCount?: number }
export interface ModelConfig { id: string; name: string; provider: string; type: 'LLM' | 'Embedding' | 'Rerank'; modelId: string; base: string; dim?: number; enabled: boolean; hasKey?: boolean; tested?: boolean; options?: Record<string, unknown> }
export interface Assistant { id: string; name: string; description: string; kbIds: string[]; model: string; prompt: string; greeting: string; rerank: boolean; published: boolean; options?: Record<string, unknown> }
export interface Reference { name: string; documentId?: string; version?: string; chunk: Chunk }
export interface ChatMessage { id: string; role: 'user' | 'assistant'; text: string; refs?: Reference[]; feedback?: 'up' | 'down'; status?: string }
export interface ChatSession { id: string; title: string; assistantId: string; messages: ChatMessage[] }
export interface LogEntry { id: string; time: string; kind: string; question: string; model: string; tokens: number | null; duration: number; status: string; refs?: string[]; details?: Record<string, unknown> }
export interface Bootstrap { kbs: KnowledgeBase[]; models: ModelConfig[]; assistants: Assistant[]; logs: LogEntry[]; sessions: ChatSession[]; ocr: { configured: boolean; region?: string }; mode: string }
export interface ImportJob { id: string; documentId: string; status: string; stage: string; progress: number; error?: string }
export interface SearchResult { doc: { id: string; name: string }; chunk: Chunk; score: number; version?: string; kbId?: string }
