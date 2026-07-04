// 어시스턴트 말풍선 아래에 붙는 결과 카드들 (docs/API.md cards[] 계약).
// 카드의 숫자·판정은 전부 서버(결정론적 엔진) 출력이며 UI는 표기만 담당한다.

import { fileUrl } from '../api'
import {
  checkLabel,
  designFieldLabel,
  designFieldValue,
  escalationLabel,
  measuredSummary,
  money,
  productLabel,
  quoteLineLabel,
  slotLabel,
  slotValueLabel,
  STATUS_META,
} from '../labels'
import CardViewer3D from './CardViewer3D'

const DESIGN_FIELD_ORDER = ['name', 'company', 'title', 'phone']

function StatusBadge({ status }) {
  const meta = STATUS_META[status] || { label: status, className: 'unknown' }
  return <span className={`status-badge ${meta.className}`}>{meta.label}</span>
}

function PreflightCard({ card, latest, busy, onAutofix }) {
  const results = card.results || []
  return (
    <div className="card">
      {card.gate_ok ? (
        <div className="gate-banner ok">
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10.5l4 4 8-9" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
          인쇄 가능한 파일이에요
        </div>
      ) : (
        <div className="gate-banner attention">
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3l8 14H2z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" /><path d="M10 8.2v4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /><circle cx="10" cy="14.6" r="1" fill="currentColor" /></svg>
          인쇄 전에 확인이 필요한 항목이 있어요
        </div>
      )}
      <div className="table-scroll">
        <table className="check-table">
          <thead>
            <tr>
              <th>검사 항목</th>
              <th>판정</th>
              <th>측정 결과</th>
              <th aria-label="조치"></th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr key={r.check_id}>
                <th scope="row">{checkLabel(r.check_id)}</th>
                <td><StatusBadge status={r.status} /></td>
                <td className="measured">{measuredSummary(r)}</td>
                <td className="action-cell">
                  {latest && r.status === 'fail' && r.autofix?.available && (
                    <button
                      type="button"
                      className="btn small accent"
                      disabled={busy}
                      onClick={() => onAutofix(r.check_id)}
                    >
                      자동 보정 적용
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function QuoteCard({ card }) {
  const lines = card.lines || []
  return (
    <div className="card">
      <div className="card-title">
        견적서
        {card.product && <span className="card-title-sub">{productLabel(card.product)}</span>}
      </div>
      <div className="table-scroll">
        <table className="quote-table">
          <tbody>
            {lines.map((line, i) => (
              <tr key={i}>
                <th scope="row">{quoteLineLabel(line)}</th>
                <td className="amount">{money(line.amount)}</td>
              </tr>
            ))}
            {card.supply_amount !== undefined && (
              <tr className="subtotal">
                <th scope="row">공급가액</th>
                <td className="amount">{money(card.supply_amount)}</td>
              </tr>
            )}
            {card.vat !== undefined && (
              <tr className="subtotal">
                <th scope="row">부가세 (10%)</th>
                <td className="amount">{money(card.vat)}</td>
              </tr>
            )}
          </tbody>
          <tfoot>
            <tr className="total">
              <th scope="row">합계{card.vat_included !== false ? ' (부가세 포함)' : ''}</th>
              <td className="amount total-amount">{money(card.total)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  )
}

function AutofixPreviewCard({ card }) {
  const before = fileUrl(card.before_url ?? card.before)
  const after = fileUrl(card.after_url ?? card.after)
  return (
    <div className="card">
      <div className="card-title">
        자동 보정 결과
        <span className="card-title-sub">{checkLabel(card.check_id)}</span>
      </div>
      <div className="fix-compare">
        <figure>
          <div className="preview-frame"><img src={before} alt="보정 전 미리보기" /></div>
          <figcaption>보정 전</figcaption>
        </figure>
        <svg className="fix-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h14m0 0l-5-5m5 5l-5 5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
        <figure>
          <div className="preview-frame after"><img src={after} alt="보정 후 미리보기" /></div>
          <figcaption className="after-label">보정 후</figcaption>
        </figure>
      </div>
    </div>
  )
}

function FilePreviewCard({ card }) {
  return (
    <div className="card">
      <div className="card-title">업로드 파일 미리보기</div>
      <div className="preview-frame single">
        <img src={fileUrl(card.url)} alt="업로드한 파일의 1페이지 미리보기" />
      </div>
    </div>
  )
}

function EscalationCard({ card }) {
  const reasons = card.reasons || []
  return (
    <div className="card escalation">
      <div className="escalation-head">
        <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="6" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.7" /><path d="M3.5 17c.9-3.2 3.5-4.8 6.5-4.8s5.6 1.6 6.5 4.8" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" /></svg>
        <div>
          <strong>담당자 검토 큐로 이동했어요</strong>
          <p>기계가 단정하기 어려운 부분이라 검판 담당자가 직접 확인한 뒤 이 대화로 이어서 안내드려요.</p>
        </div>
      </div>
      {reasons.length > 0 && (
        <ul className="escalation-reasons">
          {reasons.map((code, i) => (
            <li key={i}>{escalationLabel(code)}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function OrderConfirmedCard({ card }) {
  const summary = card.summary || {}
  const slots = summary.slots || {}
  return (
    <div className="card order-confirmed">
      <div className="order-head">
        <svg viewBox="0 0 48 48" aria-hidden="true">
          <circle cx="24" cy="24" r="22" fill="currentColor" opacity="0.12" />
          <circle cx="24" cy="24" r="16" fill="currentColor" />
          <path d="M17 24.5l5 5 9.5-11" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <div>
          <strong>주문이 확정되었습니다</strong>
          <p>접수부터 검판·견적까지 완료된 사양으로 제작을 시작해요.</p>
        </div>
      </div>
      <div className="order-no">
        <span>주문번호</span>
        <code>{card.order_no}</code>
      </div>
      <div className="table-scroll">
        <table className="spec-table">
          <tbody>
            {summary.product && (
              <tr>
                <th scope="row">상품</th>
                <td>{productLabel(summary.product)}</td>
              </tr>
            )}
            {Object.entries(slots).map(([name, value]) =>
              value === null || value === undefined ? null : (
                <tr key={name}>
                  <th scope="row">{slotLabel(name)}</th>
                  <td>{slotValueLabel(name, value)}</td>
                </tr>
              ),
            )}
            {summary.total !== null && summary.total !== undefined && (
              <tr className="total">
                <th scope="row">결제 금액 (부가세 포함)</th>
                <td className="total-amount">{money(summary.total)}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function DesignPreviewCard({ card, latest, busy, onDesign }) {
  const templates = card.templates || []
  const current = card.template
  const currentName = templates.find((t) => t.id === current)?.name || current
  const fields = card.fields || {}
  const rows = DESIGN_FIELD_ORDER.filter((k) => fields[k] !== null && fields[k] !== undefined && fields[k] !== '')

  return (
    <div className="card">
      <div className="card-title">
        명함 시안
        {currentName && <span className="card-title-sub">{currentName}</span>}
      </div>

      <CardViewer3D previewUrl={card.preview_url} label={`${currentName || '명함'} 시안 미리보기`} />
      <p className="design-hint">드래그해서 돌려보세요</p>

      {templates.length > 0 && (
        <div className="design-templates" role="group" aria-label="템플릿 선택">
          {templates.map((t) => {
            const active = t.id === current
            return (
              <button
                key={t.id}
                type="button"
                className={active ? 'btn small accent' : 'btn small'}
                aria-pressed={active}
                disabled={busy || !latest || active}
                onClick={() => onDesign?.(t.id, t.name)}
              >
                {t.name}
              </button>
            )
          })}
        </div>
      )}

      {rows.length > 0 && (
        <div className="table-scroll">
          <table className="design-fields">
            <tbody>
              {rows.map((k) => (
                <tr key={k}>
                  <th scope="row">{designFieldLabel(k)}</th>
                  <td>{designFieldValue(k, fields[k])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Card({ card, latest, busy, onAutofix, onDesign }) {
  switch (card.type) {
    case 'preflight_report':
      return <PreflightCard card={card} latest={latest} busy={busy} onAutofix={onAutofix} />
    case 'quote':
      return <QuoteCard card={card} />
    case 'autofix_preview':
      return <AutofixPreviewCard card={card} />
    case 'file_preview':
      return <FilePreviewCard card={card} />
    case 'design_preview':
      return <DesignPreviewCard card={card} latest={latest} busy={busy} onDesign={onDesign} />
    case 'escalation':
      return <EscalationCard card={card} />
    case 'order_confirmed':
      return <OrderConfirmedCard card={card} />
    default:
      return null
  }
}

export default function TurnCards({ cards, latest, busy, onAutofix, onDesign }) {
  if (!cards || cards.length === 0) return null
  return (
    <div className="cards">
      {cards.map((card, i) => (
        <Card key={i} card={card} latest={latest} busy={busy} onAutofix={onAutofix} onDesign={onDesign} />
      ))}
    </div>
  )
}
