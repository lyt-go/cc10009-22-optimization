import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/contentextract'
const columns = [
  "提取器编号",
  "内容类型",
  "正文选择器",
  "时间提取规则",
  "作者提取规则",
  "图片下载策略",
  "结构化标记",
  "清洗后字数上限"
]
const actions = [
  "提交提取器评审",
  "发布提取器",
  "废弃提取器"
]
const statuses = [
  "草稿",
  "提取器评审中",
  "已发布",
  "已废弃"
]
const stats = [
  {
    "label": "提取器总数",
    "value": 0
  },
  {
    "label": "已发布提取器",
    "value": 0
  },
  {
    "label": "评审中提取器",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type VersionRow = {
  提取版本号: string
  提取器编号: string
  字段覆盖结果: Record<string, boolean> | null
  覆盖率: number | null
  冻结时间: string
}

type ManifestRow = {
  清单编号: string
  提取版本号: string
  结果摘要: { 报告编号?: string; 结论版本?: number; 快照编号?: string; 总URL数?: number } | null
  报告导出结果: { 交付编号?: string; 记录数?: number; 冲突记录数?: number } | null
  更新时间: string | null
}

export default function ContentextractView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [versions, setVersions] = useState<VersionRow[]>([])
  const [manifest, setManifest] = useState<ManifestRow[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('提取器列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '内容提取列表读取失败')
    }
  }, [filters])

  const reloadSide = useCallback(async () => {
    try {
      const [versionsResp, manifestResp] = await Promise.all([
        request(`${ENDPOINT}/versions`),
        request(`${ENDPOINT}/manifest`),
      ])
      if (versionsResp.ok) {
        setVersions(((await versionsResp.json()).items ?? []) as VersionRow[])
      }
      if (manifestResp.ok) {
        setManifest(((await manifestResp.json()).items ?? []) as ManifestRow[])
      }
    } catch {
      // 正文解析侧清单读取失败不阻断主列表
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadSide()
  }, [reload, reloadSide])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('内容提取动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '内容提取操作失败')
    }
  }

  return (
    <section className="page" data-module="contentextract">
      <header className="page-head">
        <div>
          <h2>内容提取管理</h2>
          <p className="page-desc">管理网页内容的结构化提取器，针对文章、列表、详情等不同页面模板。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('提取器登记入口尚未接入审批流')}>
            登记提取器
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出内容提取清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无内容提取数据，可先登记提取器</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条内容提取记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      <section className="review-panel">
        <h3>结构化提取版本数据（字段覆盖结果，质量重算与导出共用）</h3>
        <table className="data-table">
          <thead>
            <tr>
              <th>提取版本号</th>
              <th>提取器编号</th>
              <th>字段覆盖结果</th>
              <th>覆盖率</th>
              <th>冻结时间</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((item) => (
              <tr key={item.提取版本号}>
                <td>{item.提取版本号}</td>
                <td>{item.提取器编号}</td>
                <td>
                  {item.字段覆盖结果
                    ? Object.entries(item.字段覆盖结果)
                        .map(([field, ok]) => `${field}${ok ? '✓' : '✗'}`)
                        .join(' ')
                    : '未提供'}
                </td>
                <td>{item.覆盖率 ?? '—'}</td>
                <td>{item.冻结时间}</td>
              </tr>
            ))}
            {versions.length === 0 && (
              <tr><td className="empty-state" colSpan={5}>暂无提取版本数据</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="review-panel">
        <h3>正文解析侧清单（质量侧写入的结果摘要与回写的报告导出结果）</h3>
        <table className="data-table">
          <thead>
            <tr>
              <th>清单编号</th>
              <th>提取版本号</th>
              <th>结果摘要</th>
              <th>报告导出结果</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            {manifest.map((item) => (
              <tr key={item.清单编号}>
                <td>{item.清单编号}</td>
                <td>{item.提取版本号}</td>
                <td>
                  {item.结果摘要
                    ? `${item.结果摘要.报告编号 ?? ''} 第${item.结果摘要.结论版本 ?? '?'}版 · 快照 ${item.结果摘要.快照编号 ?? ''} · URL ${item.结果摘要.总URL数 ?? 0}`
                    : '—'}
                </td>
                <td>
                  {item.报告导出结果
                    ? `${item.报告导出结果.交付编号 ?? ''} · 记录 ${item.报告导出结果.记录数 ?? 0} · 冲突 ${item.报告导出结果.冲突记录数 ?? 0}`
                    : '—'}
                </td>
                <td>{item.更新时间 ?? '—'}</td>
              </tr>
            ))}
            {manifest.length === 0 && (
              <tr><td className="empty-state" colSpan={5}>暂无清单记录</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </section>
  )
}
