import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const API = '/api/solution'

type FrozenObject = {
  type: string
  source_id: number
  name: string
  version: string
  fingerprint: string
  content: Record<string, unknown>
}

type PackageRow = {
  id: number
  name: string
  description: string
  status: string
  selection: { type: string; ref_id: number }[]
  frozen: FrozenObject[]
  references: { parent: string | null; type: string; name: string; fingerprint: string }[]
  package_uid: string | null
  content_digest: string | null
  created_at: string
  frozen_at: string | null
  exported_at: string | null
}

type DiffNode = {
  type: string
  name: string
  source_version: string
  fingerprint: string
  same_name_candidates: {
    target_id: number
    name: string
    version: string
    fingerprint: string
    content_equal: boolean
  }[]
  decision: string
  target_id: number | null
  decided_at: string | null
}

type ObjectMapping = {
  type: string
  source_fingerprint: string
  source_name: string
  target_id: number
  target_name: string
  resolution: string
}

type Receipt = {
  id: number
  package_uid: string
  name: string
  content_digest: string
  status: string
  diff_tree: DiffNode[]
  object_mapping: ObjectMapping[]
  credential_bound: boolean
  received_at: string
  activated_at: string | null
  re_uploaded: boolean
  integrity: { passed: boolean; checked_at: string }
}

type SourceItem = {
  ref_id: number
  type: string
  name: string
  status: string
  version: string
  fingerprint: string
}

type ActionResp<T = Record<string, unknown>> = {
  ok: boolean
  message: string
  entry: T
}

type ExportResp = {
  ok: boolean
  message: string
  file: Record<string, unknown> & { objects: FrozenObject[]; secret_policy: string }
}

type VerifyResp = {
  ok: boolean
  message: string
  diff_tree?: DiffNode[]
}

const OBJECT_LABELS: Record<string, string> = {
  site: '站点',
  template: '抽取模板',
  extractor: '字段映射',
  schedule: '调度策略',
}
const SOURCE_TYPES = ['template', 'extractor', 'schedule', 'site']

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(`${API}${path}`, init)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload.detail ?? '请求失败')
  }
  return payload as T
}

const post = <T,>(path: string, body?: unknown): Promise<T> =>
  call<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
const put = <T,>(path: string, body?: unknown): Promise<T> =>
  call<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) })

const decisionClass = (decision: string) => {
  if (decision === '未决') return 'badge warn'
  if (decision === '复用' || decision === '已复用') return 'badge ok'
  return 'badge info'
}

export default function SolutionView() {
  const [tab, setTab] = useState<'producer' | 'receiver'>('producer')
  return (
    <section className="page" data-module="solution">
      <header className="page-head">
        <div>
          <h2>采集方案发行包工作区</h2>
          <p className="page-desc">
            制作端冻结抽取模板、字段映射与调度策略的确切版本并导出方案文件；接收端先验证完整性、
            展开引用差异树逐项裁决，未决引用清零且绑定凭证后才启用，绝不边导入边替换现用配置。
          </p>
        </div>
        <div className="tab-switch">
          <button className={tab === 'producer' ? 'btn primary' : 'btn'} type="button" onClick={() => setTab('producer')}>
            制作端
          </button>
          <button className={tab === 'receiver' ? 'btn primary' : 'btn'} type="button" onClick={() => setTab('receiver')}>
            接收端
          </button>
        </div>
      </header>
      {tab === 'producer' ? <ProducerWorkspace /> : <ReceiverWorkspace />}
    </section>
  )
}

/* ============================================================ 制作端 */

function ProducerWorkspace() {
  const [packages, setPackages] = useState<PackageRow[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [sources, setSources] = useState<Record<string, SourceItem[]>>({})
  const [sourceType, setSourceType] = useState('template')
  const [newName, setNewName] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    const data = await call<{ items: PackageRow[] }>('/packages')
    setPackages(data.items)
    setActiveId((prev) => prev ?? (data.items[0]?.id ?? null))
  }, [])

  useEffect(() => {
    reload().catch((e: Error) => setError(e.message))
  }, [reload])

  useEffect(() => {
    Promise.all(
      SOURCE_TYPES.map((type) => call<{ items: SourceItem[] }>(`/sources/${type}`).then((d) => [type, d.items] as const)),
    )
      .then((entries) => setSources(Object.fromEntries(entries)))
      .catch((e: Error) => setError(e.message))
  }, [])

  const active = useMemo(() => packages.find((p) => p.id === activeId) ?? null, [packages, activeId])
  const selectedKeys = useMemo(
    () => new Set((active?.selection ?? []).map((s) => `${s.type}:${s.ref_id}`)),
    [active],
  )

  const guard = <A extends unknown[]>(fn: (...args: A) => Promise<void>) => (...args: A) => {
    setError('')
    setMessage('')
    fn(...args).catch((e: Error) => {
      setMessage('')
      setError(e.message)
    })
  }

  const createPackage = guard(async () => {
    if (!newName.trim()) {
      setError('请填写方案包名称')
      return
    }
    const data = await post<ActionResp<PackageRow>>('/packages', { name: newName.trim(), description: '' })
    setNewName('')
    setMessage(data.message)
    await reload()
    setActiveId(data.entry.id)
  })

  const toggleSource = guard(async (item: SourceItem) => {
    if (!active || active.status !== '编辑中') {
      setError('方案已冻结，不能调整挑选范围')
      return
    }
    const key = `${item.type}:${item.ref_id}`
    const next = selectedKeys.has(key)
      ? active.selection.filter((s) => `${s.type}:${s.ref_id}` !== key)
      : [...active.selection, { type: item.type, ref_id: item.ref_id }]
    await put<ActionResp>(`/packages/${active.id}/selection`, { selection: next })
    setMessage(next.length ? '已加入方案挑选范围' : '已从挑选范围移除')
    await reload()
  })

  const freeze = guard(async () => {
    if (!active) return
    const data = await post<ActionResp<PackageRow>>(`/packages/${active.id}/freeze`)
    setMessage(data.message)
    await reload()
  })

  const exportPackage = guard(async () => {
    if (!active) return
    const data = await post<ExportResp>(`/packages/${active.id}/export`)
    setMessage(`${data.message}（认证秘密不随包转移）`)
    // 下载为本地方案文件，供接收端上传
    const blob = new Blob([JSON.stringify(data.file, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${active.package_uid ?? 'solution'}.json`
    link.click()
    URL.revokeObjectURL(url)
    await reload()
  })

  return (
    <div className="solution-grid">
      <div className="panel">
        <h3>方案包工作区</h3>
        <div className="inline-form">
          <input
            value={newName}
            placeholder="输入新方案包名称"
            onChange={(e) => setNewName(e.target.value)}
          />
          <button className="btn primary" type="button" onClick={createPackage}>
            新建方案包
          </button>
        </div>
        <table className="data-table compact">
          <thead>
            <tr><th>方案包</th><th>状态</th><th>身份 / 摘要</th><th>冻结时间</th></tr>
          </thead>
          <tbody>
            {packages.map((pkg) => (
              <tr
                key={pkg.id}
                className={pkg.id === activeId ? 'row-selected' : ''}
                onClick={() => setActiveId(pkg.id)}
              >
                <td>{pkg.name}</td>
                <td><span className="badge info">{pkg.status}</span></td>
                <td className="mono">
                  {pkg.package_uid ? `${pkg.package_uid} · ${pkg.content_digest?.slice(0, 10)}…` : '—'}
                </td>
                <td>{pkg.frozen_at ?? '—'}</td>
              </tr>
            ))}
            {packages.length === 0 && (
              <tr><td className="empty-state" colSpan={4}>还没有方案包，先新建一个</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>从站点挑选对象并冻结确切版本</h3>
        {!active && <p className="empty-state">请先选择或新建一个方案包</p>}
        {active && (
          <>
            <div className="filter-bar">
              {SOURCE_TYPES.map((type) => (
                <button
                  key={type}
                  className={sourceType === type ? 'btn primary' : 'btn'}
                  type="button"
                  onClick={() => setSourceType(type)}
                >
                  {OBJECT_LABELS[type]}
                </button>
              ))}
            </div>
            <table className="data-table compact">
              <thead>
                <tr><th>选择</th><th>对象编号</th><th>版本</th><th>状态</th><th>版本指纹</th></tr>
              </thead>
              <tbody>
                {(sources[sourceType] ?? []).map((item) => {
                  const key = `${item.type}:${item.ref_id}`
                  const checked = selectedKeys.has(key)
                  return (
                    <tr key={key}>
                      <td>
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={active.status !== '编辑中'}
                          onChange={() => toggleSource(item)}
                        />
                      </td>
                      <td>{item.name}</td>
                      <td>{item.version}</td>
                      <td>{item.status}</td>
                      <td className="mono">{item.fingerprint}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>

            <h4>已选对象（{active.selection.length}）与引用关系</h4>
            <table className="data-table compact">
              <thead>
                <tr><th>类型</th><th>对象</th><th>冻结版本指纹</th></tr>
              </thead>
              <tbody>
                {active.frozen.length === 0 && active.selection.length === 0 && (
                  <tr><td className="empty-state" colSpan={3}>尚未挑选对象</td></tr>
                )}
                {(active.frozen.length ? active.frozen : active.selection.map((s) => ({
                  ...s, name: sources[s.type]?.find((i) => i.ref_id === s.ref_id)?.name ?? `#${s.ref_id}`,
                  version: '', fingerprint: '冻结后生成', content: {},
                }))).map((obj, idx) => (
                  <tr key={`${obj.type}-${obj.name}-${idx}`}>
                    <td>{OBJECT_LABELS[obj.type] ?? obj.type}</td>
                    <td>{obj.name}</td>
                    <td className="mono">{obj.fingerprint}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="page-actions">
              <button className="btn primary" type="button" disabled={active.status !== '编辑中'} onClick={freeze}>
                冻结确切版本
              </button>
              <button className="btn" type="button" disabled={active.status === '编辑中'} onClick={exportPackage}>
                导出方案文件
              </button>
            </div>
            <p className="hint">冻结后对象版本与引用关系固定；导出文件带独立身份与内容摘要，不含环境专属认证秘密。</p>
          </>
        )}
      </div>

      <footer className="page-foot full-span">
        {message && <span className="ok-text">{message}</span>}
        {error && <span className="error-text">{error}</span>}
      </footer>
    </div>
  )
}

/* ============================================================ 接收端 */

function ReceiverWorkspace() {
  const [receipts, setReceipts] = useState<Receipt[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [credentials, setCredentials] = useState<{ credential_ref: string; status: string; target_site: string }[]>([])
  const [fileName, setFileName] = useState('')
  const [pendingFile, setPendingFile] = useState<Record<string, unknown> | null>(null)
  const [previewNodes, setPreviewNodes] = useState<DiffNode[] | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    const data = await call<{ items: Receipt[] }>('/receipts')
    setReceipts(data.items)
    setActiveId((prev) => prev ?? (data.items[0]?.id ?? null))
  }, [])

  useEffect(() => {
    reload().catch((e: Error) => setError(e.message))
    call<{ items: typeof credentials }>('/credentials')
      .then((d) => setCredentials(d.items))
      .catch((e: Error) => setError(e.message))
  }, [reload])

  const active = useMemo(() => receipts.find((r) => r.id === activeId) ?? null, [receipts, activeId])
  const unresolved = active?.diff_tree.filter((n) => n.decision === '未决').length ?? 0

  const guard = <A extends unknown[]>(fn: (...args: A) => Promise<void>) => (...args: A) => {
    setError('')
    setMessage('')
    fn(...args).catch((e: Error) => {
      setMessage('')
      setError(e.message)
    })
  }

  const onPickFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    setFileName(file.name)
    setError('')
    setPreviewNodes(null)
    const reader = new FileReader()
    reader.onload = () => {
      try {
        setPendingFile(JSON.parse(String(reader.result)))
      } catch {
        setError('方案文件不是合法 JSON，请确认文件未损坏')
        setPendingFile(null)
      }
    }
    reader.readAsText(file)
  }

  const preview = guard(async () => {
    if (!pendingFile) return
    const data = await post<VerifyResp>('/verify', {
      file: pendingFile,
    })
    if (!data.ok) {
      setError(data.message)
      setPreviewNodes(null)
      return
    }
    setPreviewNodes(data.diff_tree ?? [])
    setMessage(`完整性验证通过，导入差异预览已生成（未落库、未触碰现用配置）`)
  })

  const upload = guard(async () => {
    if (!pendingFile) return
    const data = await post<ActionResp<Receipt>>('/receipts', { file: pendingFile })
    setMessage(data.message)
    setPendingFile(null)
    setPreviewNodes(null)
    setFileName('')
    await reload()
    setActiveId(data.entry.id)
  })

  const decide = guard(async (node: DiffNode, decision: string) => {
    if (!active) return
    await post<ActionResp>(`/receipts/${active.id}/decisions`, { fingerprint: node.fingerprint, decision })
    setMessage(`对象 ${node.name} 已裁决为「${decision}」`)
    await reload()
  })

  const bindCredential = guard(async (event: React.ChangeEvent<HTMLSelectElement>) => {
    if (!active || !event.target.value) return
    const data = await post<ActionResp>(`/receipts/${active.id}/credentials`, { credential_ref: event.target.value })
    setMessage(data.message)
    await reload()
  })

  const activate = guard(async () => {
    if (!active) return
    const data = await post<ActionResp<Receipt>>(`/receipts/${active.id}/activate`)
    setMessage(data.message)
    await reload()
  })

  const reject = guard(async () => {
    if (!active) return
    const data = await post<ActionResp>(`/receipts/${active.id}/reject`)
    setMessage(data.message)
    await reload()
  })

  return (
    <div className="solution-grid">
      <div className="panel">
        <h3>上传方案文件并预览导入差异</h3>
        <div className="inline-form">
          <input type="file" accept="application/json,.json" onChange={onPickFile} />
          <button className="btn" type="button" disabled={!pendingFile} onClick={preview}>
            完整性验证与差异预览
          </button>
          <button className="btn primary" type="button" disabled={!pendingFile} onClick={upload}>
            确认接收
          </button>
        </div>
        {fileName && <p className="hint">已选择文件：{fileName}</p>}
        {previewNodes && (
          <table className="data-table compact">
            <thead>
              <tr><th>对象</th><th>类型</th><th>同名匹配</th><th>建议处置</th></tr>
            </thead>
            <tbody>
              {previewNodes.map((node) => (
                <tr key={node.fingerprint}>
                  <td>{node.name}</td>
                  <td>{OBJECT_LABELS[node.type] ?? node.type}</td>
                  <td>
                    {node.same_name_candidates.length
                      ? node.same_name_candidates.map((c) => (
                          <span key={c.target_id} className={c.content_equal ? 'ok-text' : 'warn-text'}>
                            {c.name}{c.content_equal ? '（内容一致）' : '（版本不同）'}
                          </span>
                        ))
                      : '目标环境无同名对象'}
                  </td>
                  <td><span className={decisionClass(node.decision)}>{node.decision}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <h4>接收记录</h4>
        <table className="data-table compact">
          <thead>
            <tr><th>方案</th><th>状态</th><th>身份</th><th>接收时间</th></tr>
          </thead>
          <tbody>
            {receipts.map((r) => (
              <tr key={r.id} className={r.id === activeId ? 'row-selected' : ''} onClick={() => setActiveId(r.id)}>
                <td>{r.name}{r.re_uploaded && <span className="badge ok"> 重复接收</span>}</td>
                <td><span className="badge info">{r.status}</span></td>
                <td className="mono">{r.package_uid}</td>
                <td>{r.received_at}</td>
              </tr>
            ))}
            {receipts.length === 0 && <tr><td className="empty-state" colSpan={4}>暂无接收记录</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>引用差异树与启用闸门</h3>
        {!active && <p className="empty-state">请选择一条接收记录</p>}
        {active && (
          <>
            <div className="stat-row">
              <article className="stat-card">
                <span className="stat-label">未决引用</span>
                <strong className={unresolved ? 'stat-value warn-text' : 'stat-value ok-text'}>{unresolved}</strong>
              </article>
              <article className="stat-card">
                <span className="stat-label">凭证绑定</span>
                <strong className={active.credential_bound ? 'stat-value ok-text' : 'stat-value warn-text'}>
                  {active.credential_bound ? '已绑定' : '未绑定'}
                </strong>
              </article>
              <article className="stat-card">
                <span className="stat-label">接收状态</span>
                <strong className="stat-value">{active.status}</strong>
              </article>
            </div>
            <p className="hint mono">内容摘要：{active.content_digest}</p>

            <table className="data-table compact">
              <thead>
                <tr><th>迁入对象</th><th>同名候选（目标环境）</th><th>裁决</th><th>迁入后实际对象</th><th>操作</th></tr>
              </thead>
              <tbody>
                {active.diff_tree.map((node) => {
                  const mapped = active.object_mapping.find((m) => m.source_fingerprint === node.fingerprint)
                  const finished = active.status === '已启用'
                  return (
                    <tr key={node.fingerprint}>
                      <td>
                        {node.name}
                        <span className="hint">（{OBJECT_LABELS[node.type] ?? node.type} · v{node.source_version}）</span>
                      </td>
                      <td>
                        {node.same_name_candidates.length ? (
                          node.same_name_candidates.map((c) => (
                            <div key={c.target_id} className={c.content_equal ? 'ok-text' : 'warn-text'}>
                              #{c.target_id} {c.name}{c.content_equal ? ' · 内容一致' : ' · 版本不同'}
                            </div>
                          ))
                        ) : (
                          <span className="hint">无同名对象，只能另建</span>
                        )}
                      </td>
                      <td><span className={decisionClass(node.decision)}>{node.decision}</span></td>
                      <td>
                        {mapped ? (
                          <a
                            className="link"
                            href={`/${node.type === 'extractor' ? 'extractor' : node.type}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            #{mapped.target_id} {mapped.target_name}（{mapped.resolution}）
                          </a>
                        ) : (
                          <span className="hint">启用后生成</span>
                        )}
                      </td>
                      <td className="row-actions">
                        {!finished && node.same_name_candidates.length > 0 && (
                          <>
                            <button className="link" type="button" onClick={() => decide(node, '复用')}>复用</button>
                            <button className="link" type="button" onClick={() => decide(node, '另建')}>另建</button>
                          </>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>

            <h4>目标环境凭证绑定（认证秘密不随包转移）</h4>
            <div className="inline-form">
              <select defaultValue="" disabled={active.credential_bound || active.status === '已启用'} onChange={bindCredential}>
                <option value="" disabled>
                  {active.credential_bound ? '已绑定凭证' : '选择目标环境凭证'}
                </option>
                {credentials.map((c) => (
                  <option key={c.credential_ref} value={c.credential_ref}>
                    {c.credential_ref}（{c.target_site} / {c.status}）
                  </option>
                ))}
              </select>
            </div>

            <div className="page-actions">
              <button
                className="btn primary"
                type="button"
                disabled={active.status === '已启用' || active.status === '已拒绝'}
                onClick={activate}
              >
                启用方案
              </button>
              <button
                className="btn"
                type="button"
                disabled={active.status === '已启用'}
                onClick={reject}
              >
                拒绝认领
              </button>
            </div>
            <p className="hint">
              未决引用未清零或未绑定凭证时启用会被阻止；启用只新建或复用对象，绝不替换现用配置。
              新方案仅对之后建立的采集作业生效，在途作业保留原方案证据。
            </p>
          </>
        )}
      </div>

      <footer className="page-foot full-span">
        {message && <span className="ok-text">{message}</span>}
        {error && <span className="error-text">{error}</span>}
      </footer>
    </div>
  )
}
