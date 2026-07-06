// 오더지(작업지시서) — 고객과의 대화로 만들어진 내부 검수자용 주문서.
// GET /api/session/{id}/ordersheet 스냅샷을 그대로 표기하며, 검수자가 보고 바로
// 생산에 넘길 수 있도록 사양·검판·변경·견적을 한 장으로 정리한다.
// 모든 필드는 없을 수 있으므로 방어적으로 렌더한다(서버 없이 마운트돼도 안 깨지게).

import { useCallback, useEffect, useState } from 'react'
import { fileUrl, orderSheet } from '../api'
import {
  checkLabel,
  designFieldLabel,
  designFieldValue,
  escalationLabel,
  money,
  productLabel,
  quoteLineLabel,
  slotLabel,
  slotValueLabel,
  SOURCE_LABELS,
  STATE_LABELS,
  STATUS_META,
} from '../labels'

function StatusPill({ status }) {
  const meta = STATUS_META[status] || { label: status, className: 'unknown' }
  return <span className={`status-badge ${meta.className}`}>{meta.label}</span>
}

/** 상태 배지: 확정 / 담당자 검토 / 진행 중 */
function docStatus(data) {
  if (data.confirmed) return { label: '확정', cls: 'confirmed' }
  if (data.escalated) return { label: '담당자 검토', cls: 'escalated' }
  return { label: STATE_LABELS[data.status] || '진행 중', cls: 'progress' }
}

function fmtCreated(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}.${pad(d.getMonth() + 1)}.${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function Section({ title, meta, children }) {
  return (
    <section className="sheet-section">
      <div className="sheet-section-head">
        <h3>{title}</h3>
        {meta && <span className="sheet-section-meta">{meta}</span>}
      </div>
      {children}
    </section>
  )
}

export default function OrderSheet({ sessionId, session }) {
  const [state, setState] = useState({ loading: true, error: '', data: null })

  const load = useCallback(async () => {
    if (!sessionId) {
      setState({ loading: false, error: '', data: null })
      return
    }
    setState((s) => ({ ...s, loading: true, error: '' }))
    try {
      const data = await orderSheet(sessionId)
      setState({ loading: false, error: '', data })
    } catch (e) {
      setState({ loading: false, error: e.message || '오더지를 불러오지 못했어요.', data: null })
    }
  }, [sessionId])

  // 대화가 진행되면(턴/상태/확정 변화) 최신 세션으로 오더지를 다시 불러온다.
  useEffect(() => {
    load()
  }, [load, session?.turn_count, session?.state, session?.confirmed, session?.escalated])

  const { loading, error, data } = state

  if (!sessionId) {
    return (
      <div className="sheet-view">
        <div className="sheet-doc sheet-placeholder">
          <p>상담이 시작되면 여기에 작업지시서가 만들어져요.</p>
        </div>
      </div>
    )
  }

  if (loading && !data) {
    return (
      <div className="sheet-view">
        <div className="sheet-doc sheet-placeholder">
          <p>작업지시서를 불러오는 중…</p>
        </div>
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="sheet-view">
        <div className="sheet-doc sheet-placeholder">
          <p>{error}</p>
          <button type="button" className="btn ghost" onClick={load}>
            다시 시도
          </button>
        </div>
      </div>
    )
  }

  const d = data || {}
  const specs = d.specs || []
  const content = d.card_content || {}
  const contentKeys = Object.keys(content).filter((k) => content[k] !== null && content[k] !== undefined && content[k] !== '')
  const file = d.file || {}
  const preflight = d.preflight || {}
  const issues = preflight.issues || []
  const changes = d.changes || []
  const quote = d.quote || null
  const estimate = Boolean(d.quote_is_estimate)
  const reasons = d.escalation_reasons || []
  const st = docStatus(d)
  const filePreview = fileUrl(file.preview_url)

  return (
    <div className="sheet-view">
      <div className="sheet-doc">
        <div className="sheet-topbar">
          <span className="sheet-kicker">작업지시서 · 내부 검수용</span>
          <button type="button" className="btn ghost small" onClick={load} disabled={loading}>
            {loading ? '갱신 중…' : '새로고침'}
          </button>
        </div>

        <header className="sheet-header">
          <div className="sheet-header-main">
            <span className="sheet-orderno">{d.order_no || '발번 전'}</span>
            <span className="sheet-product">{d.product ? productLabel(d.product) : '상품 미정'}</span>
          </div>
          <div className="sheet-header-side">
            <span className={`sheet-status ${st.cls}`}>{st.label}</span>
            {d.created_at && <span className="sheet-created">접수 {fmtCreated(d.created_at)}</span>}
          </div>
        </header>

        {reasons.length > 0 && (
          <div className="sheet-esc">
            <strong>담당자 검토 사유</strong>
            <ul>
              {reasons.map((code, i) => (
                <li key={i}>{escalationLabel(code)}</li>
              ))}
            </ul>
          </div>
        )}

        <Section title="상품·사양" meta={d.product ? productLabel(d.product) : undefined}>
          {specs.length > 0 ? (
            <div className="table-scroll">
              <table className="sheet-spec-table">
                <tbody>
                  {specs.map((s) => (
                    <tr key={s.slot}>
                      <th scope="row">{s.label || slotLabel(s.slot)}</th>
                      <td className="sheet-spec-value">{slotValueLabel(s.slot, s.value)}</td>
                      <td className="sheet-spec-src">
                        {s.source && (
                          <span className={`source-chip ${s.source}`}>
                            {SOURCE_LABELS[s.source] || s.source}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="sheet-empty">아직 확정된 사양이 없어요.</p>
          )}
        </Section>

        {contentKeys.length > 0 && (
          <Section title="고객 정보 (명함 내용)">
            <div className="table-scroll">
              <table className="sheet-spec-table">
                <tbody>
                  {contentKeys.map((k) => (
                    <tr key={k}>
                      <th scope="row">{designFieldLabel(k)}</th>
                      <td className="sheet-spec-value" colSpan={2}>{designFieldValue(k, content[k])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        )}

        <Section title="접수 파일" meta={file.pages ? `${file.pages}페이지` : undefined}>
          {file.name || filePreview ? (
            <div className="sheet-file">
              {filePreview ? (
                <div className="sheet-preview">
                  <img src={filePreview} alt="접수 파일 미리보기" />
                </div>
              ) : (
                <div className="sheet-preview empty">
                  <span>미리보기 없음</span>
                </div>
              )}
              <div className="sheet-file-meta">
                <div className="sheet-file-name">
                  <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 2.5h6L15.5 7v11h-11z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" /><path d="M11 2.5V7h4.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" /></svg>
                  <span>{file.name || '이름 없는 파일'}</span>
                </div>
                {file.pages !== null && file.pages !== undefined && (
                  <span className="sheet-file-pages">{file.pages}페이지</span>
                )}
              </div>
            </div>
          ) : (
            <p className="sheet-empty">아직 접수된 파일이 없어요.</p>
          )}
        </Section>

        <Section title="검판 결과">
          {preflight.gate_ok === null || preflight.gate_ok === undefined ? (
            <p className="sheet-empty">파일이 접수되면 검판 결과가 표시돼요.</p>
          ) : (
            <>
              <div className={`sheet-banner ${preflight.gate_ok ? 'ok' : 'attention'}`}>
                {preflight.gate_ok ? (
                  <>
                    <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10.5l4 4 8-9" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
                    인쇄 가능 — 모든 항목 통과
                  </>
                ) : (
                  <>
                    <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3l8 14H2z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /><path d="M10 8.2v4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /><circle cx="10" cy="14.6" r="1" fill="currentColor" /></svg>
                    확인이 필요한 항목 {issues.length}건
                  </>
                )}
              </div>
              {issues.length > 0 && (
                <ul className="sheet-issues">
                  {issues.map((it, i) => {
                    const meta = STATUS_META[it.status] || { className: 'unknown' }
                    return (
                      <li key={i} className={`sheet-issue ${meta.className}`}>
                        <div className="sheet-issue-head">
                          <span className="sheet-issue-name">{checkLabel(it.check_id)}</span>
                          <StatusPill status={it.status} />
                        </div>
                        {it.message && <p className="sheet-issue-msg">{it.message}</p>}
                      </li>
                    )
                  })}
                </ul>
              )}
            </>
          )}
        </Section>

        {changes.length > 0 && (
          <Section title="변경 내역 (접수본 → 최종본)">
            <ul className="sheet-changes">
              {changes.map((c, i) => {
                const before = fileUrl(c.before_url)
                const after = fileUrl(c.after_url)
                const hasDelta = c.before !== undefined || c.after !== undefined
                return (
                  <li key={i} className="sheet-change">
                    <div className="sheet-change-head">
                      <span className="sheet-change-label">{c.label || checkLabel(c.check_id) || '변경 항목'}</span>
                      {hasDelta && (
                        <span className="change-delta">
                          <span className="before">{c.before ?? '—'}</span>
                          <span className="change-arrow" aria-hidden="true">→</span>
                          <span className="after">{c.after ?? '—'}</span>
                        </span>
                      )}
                    </div>
                    {(before || after) && (
                      <div className="sheet-change-imgs">
                        <figure>
                          <div className="sheet-preview small">{before ? <img src={before} alt="변경 전" /> : <span>없음</span>}</div>
                          <figcaption>접수본</figcaption>
                        </figure>
                        <figure>
                          <div className="sheet-preview small after">{after ? <img src={after} alt="변경 후" /> : <span>없음</span>}</div>
                          <figcaption className="after">최종본</figcaption>
                        </figure>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          </Section>
        )}

        <Section title={estimate ? '예상 견적' : '견적'}>
          {quote ? (
            <div className="sheet-quote">
              <div className="sheet-quote-total">
                <span className="sheet-quote-amount">{money(quote.total)}</span>
                <span className="sheet-quote-note">
                  {quote.vat_included !== false ? '부가세 포함' : '부가세 별도'}
                  {estimate ? ' · 확정 시 정확한 금액' : ''}
                </span>
              </div>
              <div className="table-scroll">
                <table className="sheet-quote-table">
                  <tbody>
                    {(quote.lines || []).map((line, i) => (
                      <tr key={i}>
                        <th scope="row">{quoteLineLabel(line)}</th>
                        <td className="amount">{money(line.amount)}</td>
                      </tr>
                    ))}
                    {quote.supply_amount !== undefined && (
                      <tr className="subtotal">
                        <th scope="row">공급가액</th>
                        <td className="amount">{money(quote.supply_amount)}</td>
                      </tr>
                    )}
                    {quote.vat !== undefined && (
                      <tr className="subtotal">
                        <th scope="row">부가세 (10%)</th>
                        <td className="amount">{money(quote.vat)}</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <p className="sheet-empty">사양이 확정되면 견적이 계산돼요.</p>
          )}
        </Section>

        <footer className="sheet-footer">
          <span>대화 {d.turn_count ?? 0}턴</span>
          <span className="sheet-footer-id">{d.session_id || sessionId}</span>
        </footer>
      </div>
    </div>
  )
}
