// 접수 데스크 루트 — 세션 시작/턴 처리/카드 전달.
// 상태의 유일한 원천은 서버 응답의 session 스냅샷이다 (docs/API.md 원칙).

import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import { api, ApiError, designTemplate } from './api'
import { slotLabel } from './labels'
import AccessGate from './components/AccessGate'
import BoardPanel from './components/BoardPanel'
import ChatPane from './components/ChatPane'
import OrderSheet from './components/OrderSheet'
import SidePanel from './components/SidePanel'

let nextMessageId = 1

// 메인 보기 모드: 상담(채팅) / 오더지(작업지시서) / 비교(기존 게시판 대비)
const VIEWS = [
  { id: 'chat', label: '상담' },
  { id: 'sheet', label: '오더지' },
  { id: 'compare', label: '비교' },
]

export default function App() {
  const [locked, setLocked] = useState(null) // null = 접속 확인 중
  const [session, setSession] = useState(null)
  const [messages, setMessages] = useState([])
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState('chat') // 'chat' | 'sheet' | 'compare'
  const initialized = useRef(false)

  const push = useCallback((msg) => {
    setMessages((prev) => [...prev, { id: nextMessageId++, ...msg }])
  }, [])

  const pushReply = useCallback(
    (data) => {
      setSession(data.session)
      push({
        role: 'assistant',
        text: data.reply?.text || '',
        quickOptions: data.reply?.quick_options || [],
        questions: data.reply?.questions || [],
        cards: data.cards || [],
      })
    },
    [push],
  )

  /** 새 상담 시작 (POST /api/session). 401이면 접속 코드 게이트로. */
  const startSession = useCallback(async () => {
    setBusy(true)
    setMessages([])
    setSession(null)
    try {
      const data = await api('/api/session', { method: 'POST' })
      setLocked(false)
      pushReply(data)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setLocked(true)
        return
      }
      setLocked(false)
      push({
        role: 'system',
        text: '서버에 연결하지 못했어요. 서버가 켜져 있는지 확인한 뒤 상단의 "새 상담"으로 다시 시도해 주세요.',
      })
    } finally {
      setBusy(false)
    }
  }, [push, pushReply])

  useEffect(() => {
    if (initialized.current) return // StrictMode 이중 마운트에서 세션 중복 생성 방지
    initialized.current = true
    startSession()
  }, [startSession])

  /** 서버 턴 공통 처리: 요청 → session 교체 → 어시스턴트 말풍선 추가. */
  const runTurn = useCallback(
    async (request) => {
      setBusy(true)
      try {
        const data = await request()
        pushReply(data)
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          setLocked(true)
          return
        }
        push({ role: 'system', text: e.message || '요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.' })
      } finally {
        setBusy(false)
      }
    },
    [push, pushReply],
  )

  const sendMessage = useCallback(
    (text) => {
      const t = String(text).trim()
      if (!t || !session || busy) return
      push({ role: 'user', text: t })
      runTurn(() => api(`/api/session/${session.id}/message`, { method: 'POST', body: { text: t } }))
    },
    [session, busy, push, runTurn],
  )

  // 여러 파일을 올리면 한 번에 접수한다 (예: 명함 앞면·뒷면 → 양면으로 병합).
  // 하나 보고 기다렸다 또 하나 보는 게 아니라, 한꺼번에 검판해 결과를 한 번만 보여준다.
  const uploadFiles = useCallback(
    async (fileList) => {
      if (!session || busy || !fileList) return
      const files = Array.from(fileList).filter(
        (f) =>
          f.type === 'application/pdf' ||
          f.type === 'image/png' ||
          f.type === 'image/jpeg' ||
          /\.(pdf|png|jpe?g)$/i.test(f.name || ''),
      )
      if (files.length === 0) {
        push({ role: 'system', text: 'PDF 또는 이미지(JPG·PNG)만 접수할 수 있어요.' })
        return
      }
      const names = files.map((f) => f.name || 'upload.pdf')
      push({ role: 'user', text: names.join('\n'), isFile: true })
      const formData = new FormData()
      for (const f of files) formData.append('files', f)
      await runTurn(() => api(`/api/session/${session.id}/uploads`, { method: 'POST', formData }))
    },
    [session, busy, push, runTurn],
  )

  const applyAutofix = useCallback(
    (checkId) => {
      if (!session || busy) return
      push({ role: 'user', text: '자동 보정을 적용해 주세요.' })
      runTurn(() => api(`/api/session/${session.id}/autofix`, { method: 'POST', body: { check_id: checkId } }))
    },
    [session, busy, push, runTurn],
  )

  const applyDesign = useCallback(
    (template, name) => {
      if (!session || busy) return
      push({ role: 'user', text: name ? `'${name}' 템플릿으로 보여 주세요.` : '다른 템플릿으로 보여 주세요.' })
      runTurn(() => designTemplate(session.id, { template }))
    },
    [session, busy, push, runTurn],
  )

  const selectOption = useCallback(
    (slot, value, label) => {
      if (!session || busy) return
      push({ role: 'user', text: label ?? String(value) })
      runTurn(() => api(`/api/session/${session.id}/select`, { method: 'POST', body: { slot, value } }))
    },
    [session, busy, push, runTurn],
  )

  // 여러 질문을 버튼으로 한꺼번에 고른 뒤 접수 — 순서대로 반영하고 마지막 응답만 보여준다
  // (하나 고를 때마다 왕복하며 나머지 버튼이 사라지던 문제 해결).
  const selectMany = useCallback(
    async (picks) => {
      if (!session || busy || !picks?.length) return
      if (picks.length === 1) {
        selectOption(picks[0].slot, picks[0].value, picks[0].label)
        return
      }
      push({ role: 'user', text: picks.map((p) => p.label ?? String(p.value)).join(' · ') })
      setBusy(true)
      try {
        let data
        for (const p of picks) {
          data = await api(`/api/session/${session.id}/select`, {
            method: 'POST',
            body: { slot: p.slot, value: p.value },
          })
        }
        pushReply(data)
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          setLocked(true)
          return
        }
        push({ role: 'system', text: e.message || '요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.' })
      } finally {
        setBusy(false)
      }
    },
    [session, busy, push, pushReply, selectOption],
  )

  const confirmOrder = useCallback(() => {
    if (!session || busy) return
    push({ role: 'user', text: '네, 이대로 확정할게요.' })
    runTurn(() => api(`/api/session/${session.id}/confirm`, { method: 'POST' }))
  }, [session, busy, push, runTurn])

  // 최종 확인 카드에서 '바꾸기' → 그 항목을 다시 고르게 (선택지 버튼을 띄운다)
  const reopenSlot = useCallback(
    (slot) => {
      if (!session || busy) return
      push({ role: 'user', text: `${slotLabel(slot)} 바꿀래요.` })
      runTurn(() => api(`/api/session/${session.id}/reopen`, { method: 'POST', body: { slot } }))
    },
    [session, busy, push, runTurn],
  )

  if (locked === null) {
    return <div className="boot">접속 확인 중…</div>
  }
  if (locked) {
    return <AccessGate onUnlocked={startSession} />
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32">
              <rect width="32" height="32" rx="7" fill="currentColor" />
              <path d="M10 6.5h8.5L23 11v14.5H10z" fill="#fff" />
              <path d="M13 15.5h7M13 18.5h7M13 21.5h4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </span>
          <div className="brand-text">
            <h1>AI 파일접수·검판 데스크</h1>
            <span>파일 접수부터 견적 확정까지</span>
          </div>
        </div>

        <nav className="view-tabs" role="tablist" aria-label="보기 전환">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              type="button"
              role="tab"
              aria-selected={view === v.id}
              className={view === v.id ? 'view-tab active' : 'view-tab'}
              onClick={() => setView(v.id)}
            >
              {v.label}
            </button>
          ))}
        </nav>

        <div className="topbar-actions">
          <button type="button" className="btn ghost" onClick={startSession} disabled={busy}>
            새 상담
          </button>
        </div>
      </header>

      {view === 'compare' && (
        <div className="compare-banner">
          같은 주문, 두 방식 — 기존 게시판 왕복 <strong>반나절</strong> vs AI 접수 <strong>30초</strong>
        </div>
      )}

      {view === 'sheet' ? (
        <OrderSheet sessionId={session?.id} session={session} />
      ) : (
        <div className={view === 'compare' ? 'workspace compare' : 'workspace'}>
          {view === 'compare' && <BoardPanel />}
          <ChatPane
            messages={messages}
            busy={busy}
            session={session}
            onSend={sendMessage}
            onSelect={selectOption}
            onSelectMany={selectMany}
            onUpload={uploadFiles}
            onAutofix={applyAutofix}
            onDesign={applyDesign}
            onReopen={reopenSlot}
            onConfirm={confirmOrder}
          />
          <SidePanel session={session} />
        </div>
      )}
    </div>
  )
}
