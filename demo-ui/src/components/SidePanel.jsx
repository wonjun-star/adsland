// 우측 주문 요약 패널 — 서버 session 스냅샷(유일한 상태 원천)을 그대로 표기.

import {
  CUSTOMER_TYPE_LABELS,
  productLabel,
  progressOf,
  slotLabel,
  slotValueLabel,
  SOURCE_LABELS,
  STATE_LABELS,
} from '../labels'

const STEPS = ['접수', '검판', '견적', '확정']

export default function SidePanel({ session }) {
  const state = session?.state
  const done = progressOf(state)
  const slots = Object.entries(session?.slots || {}).filter(
    ([, s]) => s && s.value !== null && s.value !== undefined,
  )

  return (
    <aside className="side-panel">
      <section className="panel-block">
        <div className="panel-head">
          <h2>진행 상태</h2>
          <span className={`state-badge ${state === 'ESCALATED' ? 'escalated' : state === 'COMPLETED' ? 'completed' : ''}`}>
            {STATE_LABELS[state] || state || '연결 중'}
          </span>
        </div>
        <ol className="steps">
          {STEPS.map((label, i) => {
            const cls = i < done ? 'done' : i === done && state !== 'COMPLETED' ? 'current' : ''
            return (
              <li key={label} className={cls}>
                <span className="step-dot" aria-hidden="true">
                  {i < done ? (
                    <svg viewBox="0 0 12 12"><path d="M2.5 6.5l2.5 2.5 4.5-5.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
                  ) : (
                    i + 1
                  )}
                </span>
                <span className="step-label">{label}</span>
              </li>
            )
          })}
        </ol>
        {session?.escalated && (
          <p className="panel-note">담당자 검토가 함께 진행 중인 주문이에요.</p>
        )}
      </section>

      <section className="panel-block">
        <div className="panel-head">
          <h2>주문 요약</h2>
        </div>
        <dl className="summary-rows">
          <div>
            <dt>상품</dt>
            <dd>{session?.product ? productLabel(session.product) : '아직 미정'}</dd>
          </div>
          {session?.customer_type && (
            <div>
              <dt>고객 유형</dt>
              <dd>{CUSTOMER_TYPE_LABELS[session.customer_type] || session.customer_type}</dd>
            </div>
          )}
        </dl>

        {slots.length > 0 ? (
          <table className="slot-table">
            <tbody>
              {slots.map(([name, slot]) => (
                <tr key={name}>
                  <th scope="row">{slotLabel(name)}</th>
                  <td>
                    <span className="slot-value">{slotValueLabel(name, slot.value)}</span>
                    {slot.source && (
                      <span className={`source-chip ${slot.source}`}>
                        {SOURCE_LABELS[slot.source] || slot.source}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="panel-empty">
            사양이 정해지면 여기에 정리돼요. 파일을 올리면 사이즈 같은 값은 파일에서 바로 읽어와요.
          </p>
        )}
      </section>
    </aside>
  )
}
