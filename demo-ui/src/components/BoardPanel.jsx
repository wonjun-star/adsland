// 비교 데모 좌측 패널 — 기존 게시판 CS 왕복을 재연 (GET /api/demo/board).
// 응답의 정확한 필드명이 흔들려도 최대한 관용적으로 읽는다 (author/name, body/text 등).

import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

function parseDate(value) {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

function isStaff(post) {
  const hint = `${post.role || ''} ${post.author || ''}`
  return /staff|admin|manager|담당|관리자|고객센터|상담/i.test(hint)
}

function normalize(data) {
  const raw = Array.isArray(data)
    ? data
    : data?.posts || data?.thread || data?.messages || data?.replies || []
  const posts = (Array.isArray(raw) ? raw : []).map((p, i) => {
    const tsRaw = p.ts ?? p.time ?? p.timestamp ?? p.created_at ?? p.date ?? ''
    return {
      id: i,
      author: p.author || p.name || p.writer || (isStaff(p) ? '담당자' : '고객'),
      body: p.body ?? p.text ?? p.content ?? '',
      staff: isStaff(p),
      tsRaw: String(tsRaw),
      date: parseDate(tsRaw),
    }
  })
  const title = (!Array.isArray(data) && (data?.title || data?.subject)) || '파일 접수 문의드립니다'
  return { title, posts }
}

function fmtTs(post) {
  if (!post.date) return post.tsRaw
  const d = post.date
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function fmtDelta(ms) {
  const minutes = Math.round(ms / 60000)
  if (minutes < 1) return '1분 미만'
  if (minutes < 60) return `${minutes}분`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours < 24) return rest > 0 ? `${hours}시간 ${rest}분` : `${hours}시간`
  const days = Math.floor(hours / 24)
  return `${days}일 ${hours % 24}시간`
}

export default function BoardPanel() {
  const [board, setBoard] = useState({ loading: true, error: '', title: '', posts: [] })

  const load = useCallback(async () => {
    setBoard((b) => ({ ...b, loading: true, error: '' }))
    try {
      const data = await api('/api/demo/board')
      setBoard({ loading: false, error: '', ...normalize(data) })
    } catch (e) {
      setBoard({ loading: false, error: e.message || '게시판 데이터를 불러오지 못했어요.', title: '', posts: [] })
    }
  }, [])

  // 마운트 시 1회 로드 (합성 스레드는 고정 데이터)
  useEffect(() => {
    load()
  }, [load])

  const { posts } = board
  const first = posts.find((p) => p.date)?.date
  const last = [...posts].reverse().find((p) => p.date)?.date
  const totalMs = first && last ? last - first : null

  return (
    <aside className="board-panel">
      <div className="board-head">
        <h2>기존 방식 — 게시판 문의</h2>
        <p>같은 주문을 게시판으로 접수하면 이렇게 흘러가요.</p>
      </div>

      {board.loading && <div className="board-note">게시판 스레드를 불러오는 중…</div>}
      {board.error && (
        <div className="board-note">
          {board.error}
          <button type="button" className="btn small ghost" onClick={load}>다시 시도</button>
        </div>
      )}

      {!board.loading && !board.error && (
        <>
          <div className="board-title">{board.title}</div>
          <ol className="board-thread">
            {posts.map((post, i) => {
              const prev = posts[i - 1]
              const gap = post.date && prev?.date ? post.date - prev.date : null
              return (
                <li key={post.id}>
                  {gap !== null && gap > 10 * 60000 && (
                    <div className="board-gap">
                      <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.5" /><path d="M8 4.8V8l2.2 1.6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
                      답변까지 <strong>{fmtDelta(gap)}</strong> 대기
                    </div>
                  )}
                  <article className={`board-post ${post.staff ? 'staff' : 'customer'}`}>
                    <header>
                      <span className="board-author">{post.author}</span>
                      <time className="board-ts">{fmtTs(post)}</time>
                    </header>
                    <p>{post.body}</p>
                  </article>
                </li>
              )
            })}
          </ol>
          {totalMs !== null && totalMs > 0 && (
            <div className="board-total">
              접수 완료까지 총 <strong>{fmtDelta(totalMs)}</strong> 소요
              <span>오른쪽 AI 접수는 같은 일을 30초 안에 끝내요.</span>
            </div>
          )}
        </>
      )}
    </aside>
  )
}
