import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Alert, App, Avatar, Button, Select, Spin, Tag, Tooltip } from 'antd';
import { AppstoreOutlined, BookOutlined, ClockCircleOutlined, CloudUploadOutlined, DatabaseOutlined, FileExcelOutlined, FileImageOutlined, FileTextOutlined, InfoCircleOutlined, MessageOutlined, SlidersOutlined, ThunderboltFilled } from '@ant-design/icons';
import { api, setApiRole } from './api';
import type { Bootstrap, Page, Role } from './types';
import { emptyData, errorText, roleNames, Workspace } from './workspace';
import KnowledgePage from './pages/KnowledgePage';
import ModelsPage from './pages/ModelsPage';
import AssistantsPage from './pages/AssistantsPage';
import LogsPage from './pages/LogsPage';
import ChatPage from './pages/ChatPage';
import { PageTitle, SetupNotice } from './components';
import { useWorkspace as useWorkspaceSafe } from './workspace';
import './styles.css';

const nav: { key: Page; label: string; icon: ReactNode; group?: string }[] = [
  { key: 'overview', label: '工作台', icon: <AppstoreOutlined /> }, { key: 'knowledge', label: '知识库', icon: <DatabaseOutlined /> },
  { key: 'assistants', label: '问答助手', icon: <ThunderboltFilled /> }, { key: 'chat', label: '在线对话', icon: <MessageOutlined /> },
  { key: 'models', label: '模型与解析服务', icon: <SlidersOutlined />, group: '系统管理' }, { key: 'logs', label: '日志监测', icon: <ClockCircleOutlined /> },
];
const readRole = (): Role => { try { const value = localStorage.getItem('zhixu-role'); return value === 'manager' || value === 'user' ? value : 'admin'; } catch { return 'admin'; } };

export default function Platform() {
  const { message } = App.useApp();
  const [role, setRole] = useState<Role>(readRole);
  const [page, setPage] = useState<Page>(() => readRole() === 'user' ? 'chat' : 'knowledge');
  const [data, setData] = useState<Bootstrap>(emptyData);
  const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const sequence = useRef(0);
  async function refresh() {
    const requestId = ++sequence.current;
    try { const value = await api.get<Bootstrap>('/bootstrap'); if (requestId === sequence.current) { setData({ ...emptyData, ...value }); setError(''); } }
    catch (reason) { if (requestId === sequence.current) setError(errorText(reason)); throw reason; }
    finally { if (requestId === sequence.current) setLoading(false); }
  }
  async function run<T,>(task: () => Promise<T>, success?: string): Promise<T | undefined> {
    try { const result = await task(); if (success) message.success(success); return result; }
    catch (reason) { message.error(errorText(reason), 7); return undefined; }
  }
  useEffect(() => { setApiRole(role); setLoading(true); setData(emptyData); void refresh().catch(() => undefined); try { localStorage.setItem('zhixu-role', role); } catch { /* Optional role preference. */ } return () => { sequence.current++; }; }, [role]);
  function go(next: Page) { setPage(next); }
  function changeRole(next: Role) { setRole(next); setPage(next === 'user' ? 'chat' : 'knowledge'); }
  return <Workspace.Provider value={{ data, role, refresh, run, go }}><div className="app-shell"><aside className="sidebar"><button className="brand" onClick={() => go(role === 'user' ? 'chat' : 'overview')}><span className="brand-symbol">知</span><span>知序<small>KNOWLEDGE OS</small></span></button><div className="workspace-switch"><span className="workspace-avatar">企</span><div>企业工作空间<small>通用知识库中台</small></div><Tag variant="filled">1.0</Tag></div><div className="nav-label">工作空间</div><nav>{nav.filter(item => role === 'admin' || (role === 'manager' ? !['models', 'logs'].includes(item.key) : item.key === 'chat')).map(item => <div key={item.key}>{item.group && <div className="nav-label secondary-label">{item.group}</div>}<button aria-label={item.label} className={`nav-item ${page === item.key ? 'active' : ''}`} onClick={() => go(item.key)}>{item.icon}<span>{item.label}</span>{item.key === 'knowledge' && <small>{data.kbs.length}</small>}</button></div>)}</nav><div className="sidebar-bottom"><div className="prototype-note"><span className="dot" />企业内部试用<small>服务端持久化 · 云端模型</small></div><div className="role-label">当前角色<Tooltip title="可以自由切换，仅区分操作模式，不构成真实身份与权限隔离。"><InfoCircleOutlined /></Tooltip></div><Select aria-label="当前角色" value={role} onChange={changeRole} options={Object.entries(roleNames).map(([value, label]) => ({ value, label }))} style={{ width: '100%' }} /></div></aside><section className="workspace"><header className="topbar"><div>工作空间 <span>/</span>{nav.find(item => item.key === page)?.label}</div><div className="topbar-right"><span className="prototype-badge"><span />ENTERPRISE KNOWLEDGE</span><Tooltip title="内部试用，无需登录，可自由选择操作角色。"><InfoCircleOutlined /></Tooltip><Avatar size={30} style={{ background: '#f0edff', color: '#6f60cc' }}>{role === 'user' ? '用' : '管'}</Avatar></div></header>{error ? <main className="content"><Alert type="error" showIcon title="暂时无法连接服务" description={error} action={<Button loading={loading} onClick={() => { setLoading(true); void refresh().catch(() => undefined); }}>重新连接</Button>} /><p className="muted" style={{ marginTop: 18 }}>请确认后端服务已启动，或查看项目部署说明。</p></main> : loading ? <div className="app-loading"><Spin size="large" /></div> : page === 'chat' ? <ChatPage key={role} /> : <main className="content">{page === 'knowledge' ? <KnowledgePage /> : page === 'models' ? <ModelsPage /> : page === 'assistants' ? <AssistantsPage /> : page === 'logs' ? <LogsPage /> : <Overview />}<footer>知序 · 企业知识中台<span>KNOWLEDGE OS / V1.0</span></footer></main>}</section></div></Workspace.Provider>;
}

function Overview() {
  const { go } = useWorkspaceSafe();
  return <><PageTitle title="让企业知识，成为团队的工作助力" description="从资料沉淀到可信回答，在一个工作空间完成。" /><SetupNotice /><section className="overview-hero"><div><div className="eyebrow">YOUR TEAM’S SECOND BRAIN</div><h2>让知识有序，<br />让每个答案有据可循。</h2><p>上传企业资料，检查知识片段，构建团队共享的知识资产。</p><Button type="primary" onClick={() => go('knowledge')}>管理知识库</Button></div><div className="hero-art"><span className="orbit" /><span className="orbit orbit-two" /><span className="art-core"><ThunderboltFilled /></span><span className="art-file art-a"><FileTextOutlined /></span><span className="art-file art-b"><FileExcelOutlined /></span><span className="art-file art-c"><FileImageOutlined /></span></div></section><div className="journey-grid">{[{ icon: <CloudUploadOutlined />, title: '01 · 导入企业知识', text: '检查解析结果与来源，确认后构建索引。', page: 'knowledge' as Page }, { icon: <SlidersOutlined />, title: '02 · 配置问答助手', text: '选择知识范围、回答规则与模型。', page: 'assistants' as Page }, { icon: <BookOutlined />, title: '03 · 验证知识价值', text: '在线提问，查看引用与运行日志。', page: 'chat' as Page }].map(item => <div className="surface journey-card" key={item.title}>{item.icon}<h3>{item.title}</h3><p>{item.text}</p><button className="text-link" onClick={() => go(item.page)}>立即开始 →</button></div>)}</div></>;
}
