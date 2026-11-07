import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, InputNumber, Modal, Progress, Space, Steps, Switch, Tag, Upload } from 'antd';
import { CheckCircleFilled, CloudUploadOutlined, DeleteOutlined, FileTextOutlined, LoadingOutlined } from '@ant-design/icons';
import type { Chunk, ImportJob, KnowledgeDocument } from '../types';
import { api, itemsOf } from '../api';
import { errorText, statusLabel, useWorkspace } from '../workspace';

const stages: Record<string, string> = { queued: '等待处理', parsing: '解析文档', splitting: '知识分块', preview: '等待确认', embedding: '生成向量', indexing: '写入索引', completed: '构建完成', failed: '处理失败' };
export default function ImportModal({ kbId, versionDocument, onClose }: { kbId: string; versionDocument?: KnowledgeDocument | null; onClose: () => void }) {
  const { data, refresh } = useWorkspace(); const { message } = App.useApp();
  const [step, setStep] = useState(0); const [files, setFiles] = useState<File[]>([]); const [chunkSize, setChunkSize] = useState(800); const [overlap, setOverlap] = useState(100); const [cloud, setCloud] = useState(true); const [running, setRunning] = useState(false); const [error, setError] = useState(''); const [jobs, setJobs] = useState<ImportJob[]>([]); const [previews, setPreviews] = useState<{ id: string; name: string; chunks: Chunk[] }[]>([]); const mounted = useRef(true); const selectedFiles = useRef<File[]>([]);
  useEffect(() => () => { mounted.current = false; }, []);
  async function poll(initial: ImportJob[]) {
    let values = initial;
    for (let count = 0; count < 1200 && mounted.current; count++) {
      values = await Promise.all(values.map(job => api.get<ImportJob>(`/jobs/${job.id}`))); if (!mounted.current) return values; setJobs(values);
      if (values.every(job => ['preview', 'completed', 'failed', 'cancelled'].includes(job.status))) return values;
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    if (!mounted.current) return values;
    throw new Error('处理时间较长，后台任务仍在继续，可返回文档列表查看状态。');
  }
  async function upload() {
    setRunning(true); setError(''); setStep(2);
    try {
      const body = new FormData(); files.forEach(file => body.append(versionDocument ? 'file' : 'files', file)); body.append('options', JSON.stringify({ chunkSize, overlap, cloudParser: cloud, autoBuild: false }));
      const raw = versionDocument
        ? await api.upload<{ document: KnowledgeDocument; jobId: string | null; duplicate?: boolean }>(`/documents/${versionDocument.id}/versions`, body)
        : await api.upload<{ documents: KnowledgeDocument[]; jobs: ImportJob[]; errors?: {name: string; error: string}[] }>(`/kbs/${kbId}/documents`, body);
      const result = 'documents' in raw ? raw : { documents: [raw.document], jobs: raw.jobId ? [{ id: raw.jobId } as ImportJob] : [], errors: [] };
      const uploadErrors = result.errors?.map(item => `${item.name}：${item.error}`).join('；') || '';
      if (!mounted.current) return; setJobs(result.jobs); const complete = result.jobs.length ? await poll(result.jobs) : [];
      if (!mounted.current) return;
      const previews = await Promise.all(result.documents.map(async doc => ({ id: doc.id, name: doc.name, chunks: itemsOf<Chunk>(await api.get(`/documents/${doc.id}/chunks?preview=true&page=1&pageSize=5`), 'chunks') })));
      if (!mounted.current) return; setPreviews(previews); const failed = complete.filter(job => job.status === 'failed'); if (failed.length || uploadErrors) setError([uploadErrors, ...failed.map(job => job.error || '部分文件解析失败')].filter(Boolean).join('；')); await refresh();
    } catch (reason) { if (mounted.current) setError(errorText(reason)); } finally { if (mounted.current) setRunning(false); }
  }
  async function build() {
    setRunning(true); setError(''); setStep(3);
    try {
      const ids = [...new Set(previews.filter(doc => doc.chunks.length).map(doc => doc.id))];
      const initial = await Promise.all(ids.map(async id => ({ id: (await api.post<{ jobId: string }>(`/documents/${id}/build`)).jobId } as ImportJob)));
      if (!mounted.current) return; const complete = await poll(initial); if (!mounted.current) return; const failed = complete.filter(job => job.status === 'failed'); if (failed.length) setError(failed.map(job => job.error || '部分文件构建失败').join('；')); else message.success('知识构建完成'); await refresh();
    } catch (reason) { if (mounted.current) setError(errorText(reason)); } finally { if (mounted.current) setRunning(false); }
  }
  return <Modal title={versionDocument ? `更新版本：${versionDocument.name}` : '导入知识'} open width={850} onCancel={onClose} mask={{ closable: !running }} footer={<Space><Button onClick={onClose}>{running ? '关闭，后台继续' : '关闭'}</Button>{step === 1 && <Button onClick={() => setStep(0)}>上一步</Button>}{step === 0 && <Button type="primary" disabled={!files.length} onClick={() => setStep(1)}>下一步</Button>}{step === 1 && <Button type="primary" disabled={overlap >= chunkSize} onClick={() => void upload()}>上传并解析</Button>}{step === 2 && <Button type="primary" disabled={running || !previews.some(doc => doc.chunks.length)} onClick={() => void build()}>确认并构建索引</Button>}{step === 3 && !running && <Button type="primary" onClick={onClose}>查看文档</Button>}</Space>}><Steps size="small" current={step} items={['选择文件', '解析与分块', '检查知识片段', '构建索引'].map(title => ({ title }))} /><div className="import-body">{error && <Alert type="error" showIcon title="部分处理未完成" description={error} style={{ marginBottom: 20 }} />}{step === 0 && <><Upload.Dragger multiple={!versionDocument} accept=".pdf,.doc,.docx,.md,.txt,.png,.jpg,.jpeg,.webp,.xlsx,.xls,.csv" showUploadList={false} beforeUpload={file => { const next = versionDocument ? [file] : selectedFiles.current.some(value => value.name === file.name && value.size === file.size) ? selectedFiles.current : [...selectedFiles.current, file]; if (next.length > 30) { message.error('单次最多导入 30 份文件'); return false; } if (next.reduce((sum, value) => sum + value.size, 0) > 50 * 1024 * 1024) { message.error('单批文件合计不能超过 50 MB'); return false; } selectedFiles.current = next; setFiles(next); return false; }}><div className="upload-icon"><CloudUploadOutlined /></div><h3>点击或拖拽文件到这里</h3><p>Word、PDF、Markdown、图片、Excel、CSV</p><p className="muted">单批合计最大 50 MB，最多 30 份文件。需云端解析的内容会发送至已配置的服务。</p></Upload.Dragger>{files.map((file, index) => <div className="upload-file" key={`${file.name}-${index}`}><FileTextOutlined /><span>{file.name}</span><small>{(file.size / 1024).toFixed(1)} KB</small><Button type="text" icon={<DeleteOutlined />} aria-label={`移除${file.name}`} onClick={() => { const next = files.filter((_, position) => position !== index); selectedFiles.current = next; setFiles(next); }} /></div>)}</>}{step === 1 && <div className="import-settings"><h3>解析方式</h3><div className="setting-box"><div><b>云端增强解析</b><p>识别扫描页、图片文字与版面结构</p></div><Switch checked={cloud} onChange={setCloud} /></div>{cloud && !data.ocr.configured && <Alert type="warning" title="云端解析服务尚未配置" description="Markdown 与表格使用原生解析。需云端处理的文件会返回配置错误；关闭增强解析可提取电子 PDF 文字。" />}<h3>知识分块</h3><div className="two-fields"><div><label>分块长度（字符）</label><InputNumber min={200} max={4000} step={100} value={chunkSize} onChange={value => setChunkSize(value || 800)} /></div><div><label>相邻分块重叠（字符）</label><InputNumber min={0} max={1000} value={overlap} onChange={value => setOverlap(value || 0)} /></div></div>{overlap >= chunkSize && <Alert type="error" title="重叠长度必须小于分块长度" />}<p className="muted">解析完成后先检查真实片段，再确认构建。表格按行组保留表头与来源。</p></div>}{step >= 2 && <><div className="job-list">{jobs.map(job => <div className="job-row" key={job.id}><div><b>{stages[job.stage] || job.stage || '等待处理'}</b><Tag color={job.status === 'failed' ? 'error' : ['completed', 'preview'].includes(job.status) ? 'success' : 'processing'}>{statusLabel(job.status)}</Tag></div><Progress percent={job.progress || 0} status={job.status === 'failed' ? 'exception' : job.status === 'completed' ? 'success' : 'normal'} />{job.error && <p className="job-error">{job.error}</p>}</div>)}</div>{running && !jobs.length && <div className="import-progress"><LoadingOutlined /><h2>正在上传文件</h2><p>文件正在发送，请稍候…</p></div>}{step === 2 && previews.map(doc => <section key={doc.id}><div className="preview-file"><FileTextOutlined />{doc.name}<Tag>真实解析预览</Tag></div>{doc.chunks.length ? doc.chunks.map((chunk, index) => <div className="chunk-card" key={chunk.id}><b>片段 {index + 1}</b><p>{chunk.text}</p><span className="muted">{chunk.location}</span></div>) : <p className="muted">没有可预览的片段，请检查任务错误。</p>}<p className="muted">最多预览前 5 个片段，完整内容可在文档详情中查看。</p></section>)}{step === 3 && !running && !error && <div className="import-progress"><CheckCircleFilled /><h2>知识已就绪</h2><p>向量索引已构建，可以检索或关联问答助手。</p></div>}</>}</div></Modal>;
}
