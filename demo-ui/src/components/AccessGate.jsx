// 접속 코드 게이트 — 서버가 401을 주면 여기서 코드를 받아 POST /api/access.

import { useState } from 'react'
import { api, ApiError } from '../api'

export default function AccessGate({ onUnlocked }) {
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    const value = code.trim()
    if (!value || busy) return
    setBusy(true)
    setError('')
    try {
      await api('/api/access', { method: 'POST', body: { code: value } })
      onUnlocked()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('접속 코드가 올바르지 않아요. 다시 확인해 주세요.')
      } else {
        setError(err.message || '접속에 실패했어요. 잠시 후 다시 시도해 주세요.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <div className="gate-mark" aria-hidden="true">
          <svg viewBox="0 0 32 32">
            <rect width="32" height="32" rx="7" fill="currentColor" />
            <path d="M10 6.5h8.5L23 11v14.5H10z" fill="#fff" />
            <path d="M13 15.5h7M13 18.5h7M13 21.5h4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </div>
        <h1>AI 파일접수·검판 데스크</h1>
        <p>초대받은 분만 사용할 수 있는 프로토타입이에요. 전달받은 접속 코드를 입력해 주세요.</p>
        <label className="gate-label" htmlFor="access-code">접속 코드</label>
        <input
          id="access-code"
          type="password"
          autoComplete="off"
          autoFocus
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="접속 코드"
        />
        {error && <p className="gate-error" role="alert">{error}</p>}
        <button type="submit" className="btn primary" disabled={busy || !code.trim()}>
          {busy ? '확인 중…' : '입장하기'}
        </button>
      </form>
    </div>
  )
}
