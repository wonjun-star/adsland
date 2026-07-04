// 서버(docs/API.md) 호출 래퍼 — 접속 코드 쿠키를 위해 항상 credentials: include.
// 서버 응답의 session 스냅샷이 유일한 상태 원천이며, UI는 로컬에서 상태를 추측하지 않는다.

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * @param {string} path 예: '/api/session'
 * @param {{method?: string, body?: object, formData?: FormData}} [options]
 */
export async function api(path, { method = 'GET', body, formData } = {}) {
  const init = { method, credentials: 'include', headers: {} }
  if (formData) {
    init.body = formData // Content-Type 은 브라우저가 boundary 포함해 설정
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  let res
  try {
    res = await fetch(path, init)
  } catch {
    throw new ApiError(0, '서버에 연결하지 못했어요. 네트워크 상태를 확인해 주세요.')
  }

  if (!res.ok) {
    let detail = ''
    try {
      const data = await res.json()
      if (typeof data?.detail === 'string') detail = data.detail
      else if (typeof data?.message === 'string') detail = data.message
    } catch {
      // 본문이 JSON 이 아니면 상태 코드 기반 메시지로 대체
    }
    throw new ApiError(res.status, detail || `요청을 처리하지 못했어요. (HTTP ${res.status})`)
  }

  if (res.status === 204) return null
  return res.json()
}

/**
 * 명함 시안 생성/재생성 (POST /api/session/{id}/design).
 * 템플릿 전환은 {template}, 내용 수정은 {fields} 를 담아 보낸다. 응답은 공통 형태.
 * @param {string} sessionId
 * @param {{template?: string, fields?: object}} body
 */
export function designTemplate(sessionId, body) {
  return api(`/api/session/${sessionId}/design`, { method: 'POST', body })
}

/** 서버가 준 파일 경로/URL → 브라우저가 접근 가능한 URL (GET /api/files/{name}) */
export function fileUrl(pathOrUrl) {
  if (!pathOrUrl) return ''
  const value = String(pathOrUrl)
  if (/^(https?:)?\/\//.test(value) || value.startsWith('/')) return value
  const name = value.split(/[\\/]/).pop()
  return `/api/files/${encodeURIComponent(name)}`
}
