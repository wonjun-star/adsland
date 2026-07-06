// 접수 대화 영역: 말풍선, 결과 카드, 빠른 선택 버튼, 확정 바, 입력창, PDF 드래그&드롭.

import { useEffect, useRef, useState } from 'react'
import { slotValueLabel } from '../labels'
import TurnCards from './Cards'

// 물어볼 게 하나면 누르는 즉시 접수. 여러 개면 각각 고른 뒤 "이대로 접수"로 한 번에 —
// 하나 눌렀다고 나머지가 나 모르게 넘어가지 않게 한다.
function QuickQuestions({ questions, onSelect, onSelectMany, onOther }) {
  const [picks, setPicks] = useState({})
  const multi = questions.length > 1

  const choose = (slot, value, label) => {
    if (!multi) {
      onSelect(slot, value, label)
      return
    }
    setPicks((prev) => ({ ...prev, [slot]: { slot, value, label } }))
  }

  const submit = () => {
    const chosen = questions.filter((q) => picks[q.slot]).map((q) => picks[q.slot])
    if (chosen.length) onSelectMany(chosen)
  }

  const pickedCount = Object.keys(picks).length

  return (
    <div className="quick-questions">
      {questions.map((q) => (
        <div key={q.slot} className="quick-q">
          {multi && <span className="quick-q-label">{q.label}</span>}
          <div className="quick-options">
            {(q.options || []).map((opt, i) => {
              const selected = picks[q.slot]?.value === opt
              return (
                <button
                  key={i}
                  type="button"
                  className={selected ? 'chip selected' : 'chip'}
                  onClick={() => choose(q.slot, opt, slotValueLabel(q.slot, opt))}
                >
                  {slotValueLabel(q.slot, opt)}
                </button>
              )
            })}
            {q.allow_other && (
              <button type="button" className="chip other" onClick={onOther}>
                기타 · 직접 입력
              </button>
            )}
          </div>
        </div>
      ))}
      {multi && (
        <button type="button" className="picks-submit" disabled={pickedCount === 0} onClick={submit}>
          {pickedCount === questions.length
            ? '이대로 접수 →'
            : `고른 것만 접수 (${pickedCount}/${questions.length}) →`}
        </button>
      )}
    </div>
  )
}

function Message({ msg, latest, busy, onSelect, onSelectMany, onOther, onAutofix, onDesign }) {
  if (msg.role === 'system') {
    return <div className="msg-system">{msg.text}</div>
  }
  if (msg.role === 'user') {
    return (
      <div className="msg-row user">
        <div className="bubble user">
          {msg.isFile && (
            <svg className="clip" viewBox="0 0 20 20" aria-hidden="true"><path d="M14.5 8.5l-5.2 5.2a2.5 2.5 0 01-3.5-3.5l6-6a3.5 3.5 0 015 5l-6 6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
          )}
          {msg.text}
        </div>
      </div>
    )
  }
  return (
    <div className="msg-row assistant">
      <div className="avatar" aria-hidden="true">AI</div>
      <div className="msg-body">
        {msg.text && <div className="bubble assistant">{msg.text}</div>}
        <TurnCards cards={msg.cards} latest={latest} busy={busy} onAutofix={onAutofix} onDesign={onDesign} />
        {latest && !busy && msg.questions?.length > 0 && (
          <QuickQuestions
            questions={msg.questions}
            onSelect={onSelect}
            onSelectMany={onSelectMany}
            onOther={onOther}
          />
        )}
      </div>
    </div>
  )
}

export default function ChatPane({ messages, busy, session, onSend, onSelect, onSelectMany, onUpload, onAutofix, onDesign, onConfirm, onReject }) {
  const [draft, setDraft] = useState('')
  const [dragging, setDragging] = useState(false)
  const endRef = useRef(null)
  const fileRef = useRef(null)
  const composerRef = useRef(null)
  const dragDepth = useRef(0)

  const focusComposer = () => composerRef.current?.focus()

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, busy])

  const completed = session?.state === 'COMPLETED'
  const awaiting = session?.state === 'PROOF_CONFIRM'
  const canSend = Boolean(session) && !busy && !completed

  const lastAssistantId = [...messages].reverse().find((m) => m.role === 'assistant')?.id

  const submit = () => {
    const text = draft.trim()
    if (!text || !canSend) return
    setDraft('')
    onSend(text)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  const pickFiles = (files) => {
    if (!files || files.length === 0) return
    onUpload(files) // 여러 개면 순서대로 접수 (앞면·뒷면 등)
  }

  const handleDrop = (e) => {
    e.preventDefault()
    dragDepth.current = 0
    setDragging(false)
    if (!canSend) return
    pickFiles(e.dataTransfer?.files)
  }

  return (
    <main
      className="chat-pane"
      onDragEnter={(e) => {
        e.preventDefault()
        dragDepth.current += 1
        setDragging(true)
      }}
      onDragLeave={(e) => {
        e.preventDefault()
        dragDepth.current = Math.max(0, dragDepth.current - 1)
        if (dragDepth.current === 0) setDragging(false)
      }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={handleDrop}
    >
      <div className="chat-scroll" role="log" aria-live="polite">
        {messages.map((m) => (
          <Message
            key={m.id}
            msg={m}
            latest={m.id === lastAssistantId}
            busy={busy}
            onSelect={onSelect}
            onSelectMany={onSelectMany}
            onOther={focusComposer}
            onAutofix={onAutofix}
            onDesign={onDesign}
          />
        ))}
        {busy && (
          <div className="msg-row assistant">
            <div className="avatar" aria-hidden="true">AI</div>
            <div className="bubble assistant typing" aria-label="응답 작성 중">
              <span /><span /><span />
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {awaiting && !busy && (
        <div className="confirm-bar">
          <div className="confirm-copy">
            <strong>사양·검판·견적이 모두 준비됐어요.</strong>
            <span>확정하면 이 사양 그대로 제작이 시작돼요.</span>
          </div>
          <div className="confirm-actions">
            <button type="button" className="btn ghost" onClick={onReject}>더 수정할게요</button>
            <button type="button" className="btn primary" onClick={onConfirm}>이대로 확정하기</button>
          </div>
        </div>
      )}

      <div className="composer">
        <input
          ref={fileRef}
          type="file"
          accept="application/pdf,.pdf"
          multiple
          hidden
          onChange={(e) => {
            pickFiles(e.target.files)
            e.target.value = ''
          }}
        />
        <button
          type="button"
          className="btn icon"
          title="PDF 파일 첨부"
          aria-label="PDF 파일 첨부"
          disabled={!canSend}
          onClick={() => fileRef.current?.click()}
        >
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M14.5 8.5l-5.2 5.2a2.5 2.5 0 01-3.5-3.5l6-6a3.5 3.5 0 015 5l-6 6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </button>
        <textarea
          ref={composerRef}
          rows={1}
          value={draft}
          disabled={!canSend}
          placeholder={
            completed
              ? '주문이 완료됐어요. 새 상담을 시작하려면 상단의 "새 상담"을 눌러 주세요.'
              : '주문 내용을 입력하세요. 예) 90×90 스티커 500매, 무광 코팅으로요'
          }
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button type="button" className="btn primary send" disabled={!canSend || !draft.trim()} onClick={submit}>
          전송
        </button>
      </div>
      <p className="composer-hint">PDF 파일을 이 영역에 끌어다 놓으면 바로 검판이 시작돼요.</p>

      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          <div className="drop-box">
            <svg viewBox="0 0 24 24"><path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 19h16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
            <strong>PDF를 놓으면 접수·검판을 시작해요</strong>
          </div>
        </div>
      )}
    </main>
  )
}
