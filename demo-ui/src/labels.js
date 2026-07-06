// 기계 코드(체크 id, 슬롯 값, 상태 코드) → 고객·상담원이 읽는 한국어 라벨.
// 서버는 결정론적 코드만 내려주고(docs/API.md), 화면 표기는 전부 여기서 변환한다.
// 사전에 없는 코드는 원문 그대로 노출해 디버깅 단서를 남긴다.

// ---------------------------------------------------------------- 상품/슬롯

export const PRODUCT_LABELS = {
  sticker: '스티커',
  namecard: '명함',
  flyer: '전단',
  poster: '포스터',
  label: '라벨',
  postcard: '엽서',
  memopad: '떡메모지',
  photocard: '포토카드',
}

export const SLOT_LABELS = {
  size: '사이즈',
  quantity: '수량',
  material: '용지',
  coating: '코팅',
  cut_type: '재단 형태',
  sides: '인쇄면',
}

const VALUE_LABELS = {
  // 용지 (catalog/*.yaml choices)
  art_250: '아트지 250g',
  art_300: '아트지 300g',
  art_300_matte: '아트지 300g 무광',
  pvc_white: 'PVC 백색(방수)',
  snow_250: '스노우지 250g',
  art_230: '아트지 230g',
  vannouveau_210: '반누보 210g',
  art_100: '아트지 100g',
  art_150: '아트지 150g',
  snow_120: '스노우지 120g',
  art_200: '아트지 200g',
  snow_200: '스노우지 200g',
  art_paper: '아트지 라벨',
  yupo: '유포지(방수)',
  clear_pet: '투명 PET',
  rendezvous_240: '랑데뷰 240g',
  woodfree_100: '모조지 100g',
  woodfree_80: '모조지 80g',
  pearl_300: '펄지 300g',
  // 코팅
  none: '없음',
  matte: '무광',
  gloss: '유광',
  // 재단 형태
  square: '사각 재단',
  circle: '원형 재단',
  die_cut: '도무송(자유형)',
  // 인쇄면
  single: '단면',
  double: '양면',
}

export function productLabel(id) {
  return PRODUCT_LABELS[id] || id || '미정'
}

export function slotLabel(name) {
  return SLOT_LABELS[name] || name
}

export function slotValueLabel(name, value) {
  if (value === null || value === undefined || value === '') return '—'
  if (name === 'quantity') {
    const n = Number(value)
    return Number.isFinite(n) ? `${n.toLocaleString('ko-KR')}매` : String(value)
  }
  if (name === 'size') {
    const s = String(value)
    return /^\d+(\.\d+)?x\d+(\.\d+)?$/i.test(s) ? `${s.replace(/x/i, '×')}mm` : s
  }
  return VALUE_LABELS[String(value)] ?? String(value)
}

export const SOURCE_LABELS = {
  user: '직접 입력',
  inferred: '파일 분석',
  default: '기본값',
}

// ---------------------------------------------------------------- 명함 시안 필드

export const DESIGN_FIELD_LABELS = {
  name: '이름',
  company: '회사',
  title: '직위',
  phone: '연락처',
}

export function designFieldLabel(key) {
  return DESIGN_FIELD_LABELS[key] || key
}

function formatPhone(raw) {
  const d = raw.replace(/\D/g, '')
  if (d.length === 11) return `${d.slice(0, 3)}-${d.slice(3, 7)}-${d.slice(7)}`
  if (d.length === 10) return `${d.slice(0, 3)}-${d.slice(3, 6)}-${d.slice(6)}`
  return raw
}

export function designFieldValue(key, value) {
  if (value === null || value === undefined || value === '') return '—'
  if (key === 'phone') return formatPhone(String(value))
  return String(value)
}

// ---------------------------------------------------------------- 세션 상태

export const STATE_LABELS = {
  INTAKE: '접수 대기',
  CLASSIFY: '요청 파악',
  SLOT_FILLING: '사양 확인',
  FILE_CHECK: '검판 진행',
  PROOF_CONFIRM: '확정 대기',
  PAYMENT_MOCK: '결제 진행',
  COMPLETED: '주문 완료',
  ESCALATED: '담당자 검토',
}

export const CUSTOMER_TYPE_LABELS = {
  A: '완성 파일 보유',
  B: '파일 보완 필요',
  C: '시안 제작 필요',
}

/** 진행 표시(접수→검판→견적→확정)에서 완료된 단계 수 (0~4) */
export function progressOf(state) {
  switch (state) {
    case 'INTAKE':
    case 'CLASSIFY':
      return 0
    case 'SLOT_FILLING':
    case 'FILE_CHECK':
    case 'ESCALATED':
      return 1
    case 'PROOF_CONFIRM': // 견적까지 나온 상태 — 확정만 남음
    case 'PAYMENT_MOCK':
      return 3
    case 'COMPLETED':
      return 4
    default:
      return 0
  }
}

// ---------------------------------------------------------------- 프리플라이트

export const CHECK_LABELS = {
  bleed: '재단 여백(도련)',
  resolution: '이미지 해상도',
  colorspace: '색상 모드',
  font_embed: '폰트 임베딩',
  trim_safety: '재단 안전 여백',
  ink_total: '총 잉크량',
  black_type: '검정 표현',
  page_size: '페이지 크기',
  page_count: '페이지 수',
  transparency: '투명 효과',
  dieline: '칼선(도무송)',
  min_line: '최소 선 굵기',
}

export function checkLabel(id) {
  return CHECK_LABELS[id] || id
}

export const STATUS_META = {
  pass: { label: '통과', className: 'pass' },
  warn: { label: '주의', className: 'warn' },
  fail: { label: '불가', className: 'fail' },
  uncertain: { label: '확인 필요', className: 'uncertain' },
}

export function fmtNum(n) {
  const num = Number(n)
  if (!Number.isFinite(num)) return String(n)
  return (Math.round(num * 100) / 100).toLocaleString('ko-KR', { maximumFractionDigits: 2 })
}

export function money(n) {
  const num = Number(n)
  return Number.isFinite(num) ? `${num.toLocaleString('ko-KR')}원` : String(n)
}

function listPreview(arr, max = 3) {
  const items = arr.map(String)
  if (items.length <= max) return items.join(', ')
  return `${items.slice(0, max).join(', ')} 외 ${items.length - max}건`
}

/** CheckResult.measured → 한 줄 한국어 요약. 알려진 체크는 전용 문구, 나머지는 일반 요약. */
export function measuredSummary(result) {
  const m = result.measured || {}
  const req = result.required || {}
  switch (result.check_id) {
    case 'bleed':
      if (m.min_mm !== undefined && m.min_mm !== null) {
        return `도련 최소 ${fmtNum(m.min_mm)}mm (기준 ${fmtNum(req.min_mm ?? 3)}mm)`
      }
      break
    case 'resolution':
      if (m.min_dpi === null) return '배치된 이미지 없음'
      if (m.min_dpi !== undefined) return `유효 해상도 최저 ${fmtNum(m.min_dpi)}dpi`
      break
    case 'colorspace': {
      const n = Array.isArray(m.rgb_objects) ? m.rgb_objects.length : (m.rgb_draw_total ?? 0)
      return n > 0 ? `화면용 RGB 색상 ${n}곳` : '인쇄용 CMYK로 준비됨'
    }
    case 'trim_safety': {
      const n = m.violation_count ?? (Array.isArray(m.violations) ? m.violations.length : 0)
      return n > 0 ? `재단 안전선 안쪽 침범 ${n}곳` : '안전 여백 확보'
    }
    case 'font_embed':
      if (m.unembedded_used_font_count === 0) return '사용 폰트 전부 임베딩됨'
      if (Array.isArray(m.unembedded_used_fonts) && m.unembedded_used_fonts.length > 0) {
        return `미임베딩 폰트 ${m.unembedded_used_fonts.length}종 (${listPreview(m.unembedded_used_fonts)})`
      }
      break
    case 'ink_total':
      if (m.max_ink_percent !== undefined) {
        return `최대 잉크량 ${fmtNum(m.max_ink_percent)}%`
      }
      break
    case 'page_size': {
      const parts = []
      if (Array.isArray(m.file_size_mm)) parts.push(`파일 ${fmtNum(m.file_size_mm[0])}×${fmtNum(m.file_size_mm[1])}mm`)
      if (Array.isArray(m.order_size_mm)) parts.push(`주문 ${fmtNum(m.order_size_mm[0])}×${fmtNum(m.order_size_mm[1])}mm`)
      if (m.rotated) parts.push('회전 배치')
      if (parts.length) return parts.join(' · ')
      break
    }
    case 'page_count':
      if (m.file_pages !== undefined) {
        return m.order_pages !== undefined && m.order_pages !== null
          ? `파일 ${m.file_pages}페이지 · 주문 기준 ${m.order_pages}페이지`
          : `파일 ${m.file_pages}페이지`
      }
      break
    case 'dieline':
      if (m.dieline_present !== undefined) {
        if (!m.dieline_present) return '칼선 별색 없음'
        const names = Array.isArray(m.dieline_spot_names) && m.dieline_spot_names.length > 0
          ? ` (${listPreview(m.dieline_spot_names)})`
          : ''
        return `칼선 별색 있음${names}`
      }
      break
    case 'min_line':
      if (m.min_width_pt === null) return '측정 대상 선 없음'
      if (m.min_width_pt !== undefined) return `최소 선 굵기 ${fmtNum(m.min_width_pt)}pt`
      break
    case 'transparency':
      if (m.count !== undefined) return m.count > 0 ? `투명 효과 ${m.count}건` : '투명 효과 없음'
      break
    case 'black_type':
      if (Array.isArray(m.rich_black_texts)) {
        return m.rich_black_texts.length > 0
          ? `4도 혼합 검정 텍스트 ${m.rich_black_texts.length}건`
          : '본문 검정 먹 1도'
      }
      break
    default:
      break
  }
  // 전용 요약이 없으면 내부 계측 필드명을 노출하지 않는다 (설명은 message가 담당).
  return ''
}

// ---------------------------------------------------------------- 견적/에스컬레이션

export function quoteLineLabel(line) {
  if (line.item === 'base') return '기본 인쇄비'
  const idx = String(line.item || '').indexOf(':')
  if (idx > 0) {
    const slot = line.item.slice(0, idx)
    const value = line.item.slice(idx + 1)
    return `${slotLabel(slot)} · ${slotValueLabel(slot, value)}`
  }
  return line.description || line.item
}

const ESCALATION_BASE = {
  turns_exceeded: '대화가 길어져 담당자가 이어받는 편이 빨라요',
  slot_thrashing: '같은 항목이 여러 번 바뀌어 담당자 확인이 필요해요',
  preflight_uncertain: '기계 검판만으로 판단하기 어려운 항목이 있어요',
  quote_over_threshold: '예상 금액이 커서 담당자 확인을 거쳐요',
  llm_parse_failures: '요청 해석이 반복해서 어긋나 담당자에게 전달했어요',
  negative_sentiment: '불편을 드린 것 같아 담당자가 직접 확인할게요',
  customer_type_C_design_needed: '시안 제작이 필요한 주문이라 담당자가 직접 안내드려요',
}

export function escalationLabel(code) {
  const raw = String(code)
  const idx = raw.indexOf(':')
  const base = idx === -1 ? raw : raw.slice(0, idx)
  const suffix = idx === -1 ? '' : raw.slice(idx + 1)
  const label = ESCALATION_BASE[base]
  if (!label) return raw
  if (!suffix) return label
  const suffixLabel = CHECK_LABELS[suffix] || SLOT_LABELS[suffix] || suffix
  return `${label} (${suffixLabel})`
}
