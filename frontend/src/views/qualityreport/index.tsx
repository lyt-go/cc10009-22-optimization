import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/qualityreport'
const columns = [
  "报告编号",
  "作业编号",
  "总URL数",
  "成功URL数",
  "失败URL数",
  "内容完整率",
  "字段覆盖率",
  "报告生成时间"
]
const actions = [
  "生成报告",
  "确认报告",
  "归档报告"
]
const statuses = [
  "草稿",
  "生成中",
  "已确认",
  "已归档"
]
const stats = [
  {
    "label": "质量报告总数",
    "value": 0
  },
  {
    "label": "已确认报告",
    "value": 0
  },
  {
    "label": "平均成功率",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type DeliverySummary = {
  交付编号: string
  结论版本: number
  快照编号: string
  水位: number
  操作键: string
  记录数: number
  冲突记录数: number
  生成时间: string
}

type ExportRecord = {
  记录编号: string
  作业编号: string
  版本依据: string | null
  指标来源: string
  冲突: boolean
  优先级: string
  总URL数: number
  成功URL数: number
  失败URL数: number
  内容完整率: number
  字段覆盖率: number
  来源URLs: Array<{
    URL: string
    写入序号: number
    提取版本: string
    结果状态: string
    内容完整: string
    字段覆盖率: number | null
    指标来源: string
  }>
}

type Delivery = {
  交付编号: string
  结论版本: number
  快照: { 快照编号: string; 水位: number; 冻结时间: string }
  质量结论: {
    总URL数: number
    成功URL数: number
    失败URL数: number
    内容完整率: number
    字段覆盖率: number
    重算作业: string[]
    复用作业: string[]
    冲突作业: string[]
    解释: string[]
  }
  导出数据集: ExportRecord[]
  生成时间: string
}

export default function QualityreportView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [deliveries, setDeliveries] = useState<DeliverySummary[]>([])
  const [reviewReport, setReviewReport] = useState<string>('')
  const [reviewReportId, setReviewReportId] = useState<number | null>(null)
  const [delivery, setDelivery] = useState<Delivery | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('质量报告列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集质量列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('采集质量动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集质量操作失败')
    }
  }

  async function runRecalc(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/recalc`, {
        method: 'POST',
        body: JSON.stringify({ values: { 操作键: `OP-${row.报告编号}-${Date.now()}` } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message || '增量重算未生效')
      }
      setNotice(payload.message)
      await reload()
      await loadDeliveries(row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '增量重算失败')
    }
  }

  async function loadDeliveries(row: Row) {
    setErrorMessage('')
    setDelivery(null)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/deliveries`)
      if (!response.ok) {
        throw new Error('交付对象列表读取失败')
      }
      const payload = await response.json()
      setDeliveries(payload.items ?? [])
      setReviewReport(String(row.报告编号 ?? ''))
      setReviewReportId(Number(row.id))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '交付对象列表读取失败')
    }
  }

  async function loadDelivery(version: number) {
    if (reviewReportId === null) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${reviewReportId}/deliveries/${version}`)
      if (!response.ok) {
        throw new Error('交付对象读取失败')
      }
      setDelivery((await response.json()) as Delivery)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '交付对象读取失败')
    }
  }

  return (
    <section className="page" data-module="qualityreport">
      <header className="page-head">
        <div>
          <h2>采集质量管理</h2>
          <p className="page-desc">生成每次采集作业的质量报告，统计成功率、完整率和字段覆盖情况。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('质量报告登记入口尚未接入审批流')}>
            登记质量报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出采集质量清单
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
                <button className="link" type="button" onClick={() => void runRecalc(row)}>
                  增量重算
                </button>
                <button className="link" type="button" onClick={() => void loadDeliveries(row)}>
                  复核交付对象
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无采集质量数据，可先登记质量报告</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条采集质量记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {reviewReport && (
        <section className="review-panel">
          <h3>交付对象复核：{reviewReport}</h3>
          {deliveries.length === 0 && <p className="empty-state">暂无交付对象，可先执行「增量重算」生成新版本</p>}
          {deliveries.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>交付编号</th>
                  <th>结论版本</th>
                  <th>快照编号</th>
                  <th>水位</th>
                  <th>记录数</th>
                  <th>冲突记录数</th>
                  <th>生成时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {deliveries.map((item) => (
                  <tr key={item.交付编号}>
                    <td>{item.交付编号}</td>
                    <td>第 {item.结论版本} 版</td>
                    <td>{item.快照编号}</td>
                    <td>{item.水位}</td>
                    <td>{item.记录数}</td>
                    <td>{item.冲突记录数}</td>
                    <td>{item.生成时间}</td>
                    <td>
                      <button className="link" type="button" onClick={() => void loadDelivery(item.结论版本)}>
                        逐项复核
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {delivery && (
            <div className="delivery-detail">
              <h4>
                {delivery.交付编号}（快照 {delivery.快照.快照编号}，水位 {delivery.快照.水位}）
              </h4>
              <p className="page-desc">
                总URL数 {delivery.质量结论.总URL数}，成功 {delivery.质量结论.成功URL数}，
                失败 {delivery.质量结论.失败URL数}，内容完整率 {delivery.质量结论.内容完整率}，
                字段覆盖率 {delivery.质量结论.字段覆盖率}；
                重算作业 [{delivery.质量结论.重算作业.join('、') || '无'}]，
                复用作业 [{delivery.质量结论.复用作业.join('、') || '无'}]
              </p>
              {delivery.质量结论.解释.length > 0 && (
                <ul className="explain-list">
                  {delivery.质量结论.解释.map((line) => <li key={line}>{line}</li>)}
                </ul>
              )}
              <table className="data-table">
                <thead>
                  <tr>
                    <th>记录编号</th>
                    <th>作业编号</th>
                    <th>版本依据</th>
                    <th>指标来源</th>
                    <th>优先级</th>
                    <th>总URL数</th>
                    <th>内容完整率</th>
                    <th>字段覆盖率</th>
                    <th>来源URL（可追溯）</th>
                  </tr>
                </thead>
                <tbody>
                  {delivery.导出数据集.map((record) => (
                    <tr key={record.记录编号} className={record.冲突 ? 'conflict-row' : ''}>
                      <td>{record.记录编号}</td>
                      <td>{record.作业编号}</td>
                      <td>{record.版本依据 ?? '—'}</td>
                      <td>{record.指标来源}</td>
                      <td>{record.冲突 ? '高（冲突）' : record.优先级}</td>
                      <td>{record.总URL数}</td>
                      <td>{record.内容完整率}</td>
                      <td>{record.字段覆盖率}</td>
                      <td>
                        <ul className="url-list">
                          {record.来源URLs.map((item) => (
                            <li key={`${record.记录编号}-${item.URL}`}>
                              {item.URL} · {item.提取版本} · 序号{item.写入序号} · {item.指标来源}
                              {item.字段覆盖率 === null ? ' · 字段覆盖缺失' : ''}
                            </li>
                          ))}
                          {record.来源URLs.length === 0 && <li>无来源URL</li>}
                        </ul>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </section>
  )
}
