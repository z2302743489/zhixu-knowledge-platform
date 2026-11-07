import type { ReactNode } from 'react';
import { Alert, Button } from 'antd';
import { useWorkspace as useWorkspaceSafe } from './workspace';

export function PageTitle({ title, description, action }: { title: string; description: string; action?: ReactNode }) { return <div className="page-title"><div><h1>{title}</h1><p>{description}</p></div>{action}</div>; }
export function SetupNotice() {
  const { data, role, go } = useWorkspaceSafe();
  if (role === 'user' || (data.models.some(m => m.type === 'LLM' && m.enabled) && data.models.some(m => m.type === 'Embedding' && m.enabled) && data.kbs.length && data.assistants.some(a => a.published))) return null;
  return <div className="setup-notice"><Alert type="info" showIcon title="先连接模型，再导入第一份知识" description="配置 Embedding 与 LLM、新建知识库、导入并构建索引，最后发布问答助手。Rerank 与云端解析可单独配置。" action={role === 'admin' ? <Button onClick={() => go('models')}>配置模型</Button> : undefined} /></div>;
}
