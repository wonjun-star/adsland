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

/** before/after 원시값을 사람이 읽는 텍스트로. 빈 값은 '—'. */
function changeText(v) {
  if (v === null || v === undefined || v === '') return '—'
  return String(v)
}

/** 접수본→최종본 / 보정 전→후 등 두 이미지를 나란히. 이미지가 없으면 폴백 표시. */
function BeforeAfter({
  before,
  after,
  beforeAlt,
  afterAlt,
  beforeLabel = '보정 전',
  afterLabel = '보정 후',
  variant,
}) {
  const b = fileUrl(before)
  const a = fileUrl(after)
  return (
    <div className={variant ? `fix-compare ${variant}` : 'fix-compare'}>
      <figure>
        <div className="preview-frame">
          {b ? <img src={b} alt={beforeAlt} /> : <span className="preview-empty">미리보기 없음</span>}
        </div>
        <figcaption>{beforeLabel}</figcaption>
      </figure>
      <svg className="fix-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h14m0 0l-5-5m5 5l-5 5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
      <figure>
        <div className="preview-frame after">
          {a ? <img src={a} alt={afterAlt} /> : <span className="preview-empty">미리보기 없음</span>}
        </div>
        <figcaption className="after-label">{afterLabel}</figcaption>
      </figure>
    </div>
  )
}

/** 검판 항목 한 줄 (문제 항목은 상세·조치까지, 통과 항목은 접힘 안에서 간단히). */
function CheckRow({ result, latest, busy, onAutofix, passOnly }) {
  const meta = STATUS_META[result.status] || { className: 'unknown' }
  const measured = measuredSummary(result)
  const canAutofix = latest && (result.status === 'fail' || result.status === 'warn') && result.autofix?.available
  return (
    <li className={`check-row ${meta.className}`}>
      <div className="check-row-head">
        <span className="check-name">{checkLabel(result.check_id)}</span>
        <StatusBadge status={result.status} />
      </div>
      {measured && <div className="check-measured">{measured}</div>}
      {!passOnly && result.message && <p className={`check-message ${meta.className}`}>{result.message}</p>}
      {passOnly && result.message && (
        <details className="check-message-toggle">
          <summary>설명 보기</summary>
          <p className="check-message pass">{result.message}</p>
        </details>
      )}
      {canAutofix && (
        <div className="check-action">
          <button type="button" className="btn small accent" disabled={busy} onClick={() => onAutofix(result.check_id)}>
            {result.check_id === 'colorspace' ? 'CMYK로 자동 변환' : '자동 보정 적용'}
          </button>
          {result.check_id === 'colorspace' && (
            <span className="autofix-caveat">색이 약간 달라질 수 있어요</span>
          )}
        </div>
      )}
      {!passOnly && result.fix_guide && <FixGuide guide={result.fix_guide} />}
    </li>
  )
}

// 애즈랜드 작업 가이드 근거 + 고치는 법 (통과 못 한 항목에 표시)
const PROGRAM_LABEL = { illustrator: '일러스트', photoshop: '포토샵', indesign: '인디자인', common: '공통' }

function FixGuide({ guide }) {
  const steps = Object.entries(guide.how_to_fix || {})
  return (
    <details className="fix-guide">
      <summary>가이드대로 고치는 법</summary>
      <div className="fix-guide-body">
        <p className="fix-rule">📐 {guide.rule}</p>
        {guide.why && <p className="fix-why">{guide.why}</p>}
        {steps.length > 0 && (
          <ul className="fix-steps">
            {steps.map(([prog, step]) => (
              <li key={prog}>
                <span className="fix-prog">{PROGRAM_LABEL[prog] || prog}</span> {step}
              </li>
            ))}
          </ul>
        )}
        {guide.autofixable && <p className="fix-auto">✓ 저희가 자동으로 보정해드릴 수 있어요.</p>}
        {guide.guide_url && (
          <a className="fix-link" href={guide.guide_url} target="_blank" rel="noreferrer">
            애즈랜드 작업 가이드 보기 →
          </a>
        )}
      </div>
    </details>
  )
}

function PreflightCard({ card, latest, busy, onAutofix }) {
  const results = card.results || []
  const problems = results.filter((r) => r.status !== 'pass')
  const passes = results.filter((r) => r.status === 'pass')
  return (
    <div className="card preflight-card">
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

      {problems.length > 0 && (
        <ul className="check-list">
          {problems.map((r) => (
            <CheckRow key={r.check_id} result={r} latest={latest} busy={busy} onAutofix={onAutofix} />
          ))}
        </ul>
      )}

      {(card.advisories || []).length > 0 && (
        <ul className="advisory-list">
          {card.advisories.map((a) => (
            <li key={a.key} className="advisory">
              <span className="advisory-icon" aria-hidden="true">💡</span>
              <span>
                {a.text}
                {a.guide_url && (
                  <a className="advisory-link" href={a.guide_url} target="_blank" rel="noreferrer">
                    {' '}가이드 →
                  </a>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}

      {passes.length > 0 && (
        <details className="check-passes">
          <summary>통과한 항목 {passes.length}개 보기</summary>
          <ul className="check-list passes">
            {passes.map((r) => (
              <CheckRow key={r.check_id} result={r} passOnly />
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function QuoteCard({ card }) {
  const lines = card.lines || []
  const estimate = card.estimate === true
  const hasBreakdown = lines.length > 0 || card.supply_amount !== undefined || card.vat !== undefined
  return (
    <div className="card quote-card">
      <div className="quote-head">
        <span className="quote-label">{estimate ? '예상 견적' : '견적'}</span>
        {card.product && <span className="card-title-sub">{productLabel(card.product)}</span>}
        {estimate && <span className="estimate-badge">확정 시 정확</span>}
      </div>

      <div className="quote-total">
        <span className="quote-total-amount">{money(card.total)}</span>
        <span className="quote-total-note">
          {card.vat_included !== false ? '부가세 포함' : '부가세 별도'}
          {estimate ? ' · 예상 금액' : ''}
        </span>
      </div>

      {card.lead_time && (
        <div className="quote-lead">
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" strokeWidth="1.6" /><path d="M10 6v4.2l2.8 1.8" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
          제작 기간 <strong>영업일 {card.lead_time}일</strong>
        </div>
      )}

      {estimate && <p className="estimate-note">사양이 확정되면 정확한 금액으로 업데이트돼요.</p>}

      {hasBreakdown && (
        <details className="quote-breakdown">
          <summary>세부 내역 보기</summary>
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
                  <th scope="row">{estimate ? '예상 합계' : '합계'}</th>
                  <td className="amount total-amount">{money(card.total)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </details>
      )}
    </div>
  )
}

function AutofixPreviewCard({ card }) {
  return (
    <div className="card">
      <div className="card-title">
        자동 보정 결과
        <span className="card-title-sub">{checkLabel(card.check_id)}</span>
      </div>
      <BeforeAfter
        before={card.before_url ?? card.before}
        after={card.after_url ?? card.after}
        beforeAlt="보정 전 미리보기"
        afterAlt="보정 후 미리보기"
      />
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

function ChangeItem({ item }) {
  const label = item.label || checkLabel(item.check_id) || '변경 항목'
  const hasDelta = item.before !== undefined || item.after !== undefined
  const hasImages = Boolean(item.before_url || item.after_url)
  return (
    <li className="change-item">
      <div className="change-item-head">
        <span className="change-label">{label}</span>
        {hasDelta && (
          <span className="change-delta">
            <span className="before">{changeText(item.before)}</span>
            <span className="change-arrow" aria-hidden="true">→</span>
            <span className="after">{changeText(item.after)}</span>
          </span>
        )}
      </div>
      {hasImages && (
        <BeforeAfter
          before={item.before_url}
          after={item.after_url}
          beforeAlt={`${label} 변경 전`}
          afterAlt={`${label} 변경 후`}
          beforeLabel="변경 전"
          afterLabel="변경 후"
        />
      )}
    </li>
  )
}

function ChangeSummaryCard({ card }) {
  const items = card.items || []
  const hasHero = Boolean(card.original_url || card.final_url)
  return (
    <div className="card change-summary">
      <div className="card-title">
        변경 내역 (접수본 → 최종본)
        {card.product && <span className="card-title-sub">{productLabel(card.product)}</span>}
      </div>
      {hasHero && (
        <BeforeAfter
          variant="hero"
          before={card.original_url}
          after={card.final_url}
          beforeAlt="접수본 미리보기"
          afterAlt="최종본 미리보기"
          beforeLabel="접수본"
          afterLabel="최종본"
        />
      )}
      {items.length > 0 ? (
        <ul className="change-list">
          {items.map((item, i) => (
            <ChangeItem key={i} item={item} />
          ))}
        </ul>
      ) : (
        !hasHero && <p className="change-empty">표시할 변경 내역이 없어요.</p>
      )}
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

// 최종 확인 (맥도날드 키오스크식) — 각 항목 훑고, 틀린 건 '바꾸기', 다 맞으면 '이대로 주문'.
function ConfirmReviewCard({ card, latest, busy, onReopen, onConfirm }) {
  const specs = card.specs || []
  const disabled = busy || !latest
  const show3D = card.product === 'namecard' && Boolean(card.preview_url)
  return (
    <div className="card confirm-review">
      <div className="card-title">
        주문 확인
        <span className="card-title-sub">확정 전에 한 번만 봐주세요</span>
      </div>
      {show3D && (
        <div className="review-3d">
          <CardViewer3D previewUrl={card.preview_url} backUrl={card.back_url} label="주문 도안 3D 미리보기" />
          <p className="design-hint">드래그해서 앞뒤로 돌려보세요</p>
        </div>
      )}
      <ul className="review-list">
        {specs.map((s) => (
          <li key={s.slot} className="review-row">
            <span className="review-check" aria-hidden="true">
              <svg viewBox="0 0 20 20"><path d="M4 10.5l4 4 8-9" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </span>
            <span className="review-label">{s.label}</span>
            <span className="review-value">{s.value_label}</span>
            <button type="button" className="review-change" disabled={disabled} onClick={() => onReopen?.(s.slot)}>
              바꾸기
            </button>
          </li>
        ))}
      </ul>
      {card.total !== null && card.total !== undefined && (
        <div className="review-total">
          <span>{card.estimate ? '예상 결제 금액' : '결제 금액'}</span>
          <strong>{money(card.total)}</strong>
        </div>
      )}
      <button type="button" className="btn primary review-confirm" disabled={disabled} onClick={onConfirm}>
        다 맞아요 · 이대로 주문
      </button>
    </div>
  )
}

// 파일을 눈에 보이게 바꾼 직후(뒷면 뒤집기 등) 결과를 3D로 바로 보여준다.
function Preview3DCard({ card }) {
  if (!card.preview_url) return null
  return (
    <div className="card">
      <div className="card-title">바뀐 도안 확인</div>
      <CardViewer3D previewUrl={card.preview_url} backUrl={card.back_url} label="바뀐 도안 3D 미리보기" />
      <p className="design-hint">{card.caption || '드래그해서 앞뒤로 돌려보세요'}</p>
    </div>
  )
}

function OrderConfirmedCard({ card }) {
  const summary = card.summary || {}
  const slots = summary.slots || {}
  const finalUrl = fileUrl(card.final_url)
  const changes = card.changes || []
  // 명함은 최종 확정본을 3D로 돌려볼 수 있게 (그 외 상품·파일 없음이면 평면 이미지)
  const show3D = summary.product === 'namecard' && Boolean(card.final_url)
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

      {(finalUrl || card.file_name) && (
        <div className="order-final">
          {show3D ? (
            <>
              <CardViewer3D previewUrl={card.final_url} backUrl={card.back_url} label="최종 확정본 3D 미리보기" />
              <p className="design-hint">드래그해서 앞뒤로 돌려보세요</p>
            </>
          ) : (
            finalUrl && (
              <div className="preview-frame single">
                <img src={finalUrl} alt="최종 확정본 미리보기" />
              </div>
            )
          )}
          {card.file_name && (
            <div className="order-file">
              <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 2.5h6L15.5 7v11h-11z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" /><path d="M11 2.5V7h4.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" /></svg>
              <span>{card.file_name}</span>
            </div>
          )}
        </div>
      )}

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

      {changes.length > 0 && (
        <div className="order-changes">
          <div className="order-changes-head">접수본에서 이렇게 조정됐어요</div>
          <ul className="change-list">
            {changes.map((c, i) => (
              <li key={i} className="change-item">
                <div className="change-item-head">
                  <span className="change-label">{c.label}</span>
                  {(c.before !== undefined || c.after !== undefined) && (
                    <span className="change-delta">
                      <span className="before">{changeText(c.before)}</span>
                      <span className="change-arrow" aria-hidden="true">→</span>
                      <span className="after">{changeText(c.after)}</span>
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
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

function Card({ card, latest, busy, onAutofix, onDesign, onReopen, onConfirm }) {
  switch (card.type) {
    case 'preflight_report':
      return <PreflightCard card={card} latest={latest} busy={busy} onAutofix={onAutofix} />
    case 'quote':
      return <QuoteCard card={card} />
    case 'autofix_preview':
      return <AutofixPreviewCard card={card} />
    case 'change_summary':
      return <ChangeSummaryCard card={card} />
    case 'file_preview':
      return <FilePreviewCard card={card} />
    case 'design_preview':
      return <DesignPreviewCard card={card} latest={latest} busy={busy} onDesign={onDesign} />
    case 'confirm_review':
      return <ConfirmReviewCard card={card} latest={latest} busy={busy} onReopen={onReopen} onConfirm={onConfirm} />
    case 'preview_3d':
      return <Preview3DCard card={card} />
    case 'escalation':
      return <EscalationCard card={card} />
    case 'order_confirmed':
      return <OrderConfirmedCard card={card} />
    default:
      return null
  }
}

export default function TurnCards({ cards, latest, busy, onAutofix, onDesign, onReopen, onConfirm }) {
  if (!cards || cards.length === 0) return null
  return (
    <div className="cards">
      {cards.map((card, i) => (
        <Card
          key={i}
          card={card}
          latest={latest}
          busy={busy}
          onAutofix={onAutofix}
          onDesign={onDesign}
          onReopen={onReopen}
          onConfirm={onConfirm}
        />
      ))}
    </div>
  )
}
