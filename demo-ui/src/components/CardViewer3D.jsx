// 명함 시안 3D 뷰어 — three.js로 얇은 카드(앞면에 시안 텍스처)를 렌더한다.
// WebGL 미지원이거나 초기화/텍스처 로드에 실패하면 평면 <img>로 폴백한다(데모가 깨지지 않도록).
// three 는 Vite 가 번들하므로 런타임 CDN 요청이 없다(포터블/도커에서 그대로 동작).

import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { fileUrl } from '../api'

const CARD_W = 1.8 // 가로 (9)
const CARD_H = 1.0 // 세로 (5) → 90×50mm = 9:5
const CARD_D = 0.024 // 두께 (얇은 카드)
const CORNER = 0.06 // 모서리 둥글기
const PAPER = 0xf3f1ec // 시안 로딩 전 앞면 종이색
const EDGE = 0xe9e6df // 뒷면·측면(카드 뒷면/두께) 종이색
const MAX_TILT = THREE.MathUtils.degToRad(38) // 드래그 회전 각도 제한(뒤집힘 방지)

/** WebGL 사용 가능 여부(폴백 판단용). */
function webglAvailable() {
  try {
    const canvas = document.createElement('canvas')
    return !!(
      window.WebGLRenderingContext &&
      (canvas.getContext('webgl') || canvas.getContext('experimental-webgl'))
    )
  } catch {
    return false
  }
}

/** 둥근 모서리 사각형 Shape (카드 실루엣). */
function roundedRectShape(w, h, r) {
  const s = new THREE.Shape()
  const x = -w / 2
  const y = -h / 2
  s.moveTo(x + r, y)
  s.lineTo(x + w - r, y)
  s.quadraticCurveTo(x + w, y, x + w, y + r)
  s.lineTo(x + w, y + h - r)
  s.quadraticCurveTo(x + w, y + h, x + w - r, y + h)
  s.lineTo(x + r, y + h)
  s.quadraticCurveTo(x, y + h, x, y + h - r)
  s.lineTo(x, y + r)
  s.quadraticCurveTo(x, y, x + r, y)
  return s
}

/** ShapeGeometry 의 UV(도형 좌표계)를 0~1 로 재매핑해 텍스처가 카드에 꽉 차게. */
function remapUV(geo, w, h) {
  const pos = geo.attributes.position
  const uv = geo.attributes.uv
  for (let i = 0; i < pos.count; i += 1) {
    uv.setXY(i, (pos.getX(i) + w / 2) / w, (pos.getY(i) + h / 2) / h)
  }
  uv.needsUpdate = true
}

export default function CardViewer3D({ previewUrl, label }) {
  const mountRef = useRef(null)
  const apiRef = useRef(null) // { setTexture } — 씬 구성 후 텍스처 교체용 핸들
  const [failed, setFailed] = useState(false)

  // 씬은 마운트당 한 번 구성하고, 언마운트 시 renderer/geometry/texture 를 모두 dispose 한다.
  // (StrictMode 이중 마운트에서도 mount→cleanup→mount 로 안전하게 재생성됨)
  useEffect(() => {
    const mount = mountRef.current
    if (!mount || !webglAvailable()) {
      setFailed(true)
      return undefined
    }

    let disposed = false
    let raf = 0
    let renderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    } catch {
      setFailed(true)
      return undefined
    }

    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap
    renderer.domElement.style.display = 'block'
    renderer.domElement.style.width = '100%'
    renderer.domElement.style.height = '100%'
    renderer.domElement.style.touchAction = 'none'
    renderer.domElement.style.cursor = 'grab'
    mount.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100)
    camera.position.set(0, 0.3, 3.3)
    camera.lookAt(0, -0.04, 0)

    // 조명: 부드러운 환경광 + 방향광(그림자) — 종이 질감이 은은하게.
    scene.add(new THREE.HemisphereLight(0xffffff, 0xd7dde2, 0.8))
    scene.add(new THREE.AmbientLight(0xffffff, 0.18))
    const key = new THREE.DirectionalLight(0xffffff, 1.05)
    key.position.set(1.6, 2.8, 2.4)
    key.castShadow = true
    key.shadow.mapSize.set(1024, 1024)
    key.shadow.radius = 6
    key.shadow.bias = -0.0004
    const sc = key.shadow.camera
    sc.near = 0.5
    sc.far = 12
    sc.left = -2.2
    sc.right = 2.2
    sc.top = 2.2
    sc.bottom = -2.2
    sc.updateProjectionMatrix()
    scene.add(key)
    const fill = new THREE.DirectionalLight(0xffffff, 0.28)
    fill.position.set(-2, 1, 1.5)
    scene.add(fill)

    // 카드 본체(둥근 모서리, 얇은 압출) — 뒷면/측면은 은은한 단색.
    const shape = roundedRectShape(CARD_W, CARD_H, CORNER)
    const bodyGeo = new THREE.ExtrudeGeometry(shape, {
      depth: CARD_D,
      bevelEnabled: false,
      curveSegments: 18,
    })
    bodyGeo.center()
    bodyGeo.computeBoundingBox()
    const frontZ = bodyGeo.boundingBox.max.z

    const bodyMat = new THREE.MeshStandardMaterial({ color: EDGE, roughness: 0.85, metalness: 0.02 })
    const body = new THREE.Mesh(bodyGeo, bodyMat)
    body.castShadow = true

    // 앞면 시안 판(텍스처가 붙는 곳) — 본체 앞면 바로 위에 얹는다.
    const frontGeo = new THREE.ShapeGeometry(shape, 18)
    remapUV(frontGeo, CARD_W, CARD_H)
    const frontMat = new THREE.MeshStandardMaterial({ color: PAPER, roughness: 0.72, metalness: 0.0 })
    const front = new THREE.Mesh(frontGeo, frontMat)
    front.position.z = frontZ + 0.002

    const card = new THREE.Group()
    card.add(body)
    card.add(front)
    scene.add(card)

    // 바닥 소프트 섀도(실물이 놓인 느낌) — 배경 그라디언트는 CSS 가 담당(alpha 캔버스).
    const groundGeo = new THREE.PlaneGeometry(12, 12)
    const groundMat = new THREE.ShadowMaterial({ opacity: 0.16 })
    const ground = new THREE.Mesh(groundGeo, groundMat)
    ground.rotation.x = -Math.PI / 2
    ground.position.y = -0.72
    ground.receiveShadow = true
    scene.add(ground)

    // 텍스처 로더 — anisotropy 최대, sRGB 색공간.
    const maxAniso = renderer.capabilities.getMaxAnisotropy()
    const loader = new THREE.TextureLoader()
    let currentTexture = null

    function setTexture(url) {
      const src = fileUrl(url)
      // 기존 텍스처 dispose 후 종이색으로 리셋
      if (currentTexture) {
        currentTexture.dispose()
        currentTexture = null
      }
      frontMat.map = null
      frontMat.color.set(PAPER)
      frontMat.needsUpdate = true
      if (!src) return
      loader.load(
        src,
        (tex) => {
          if (disposed) {
            tex.dispose()
            return
          }
          tex.colorSpace = THREE.SRGBColorSpace
          tex.anisotropy = maxAniso
          tex.needsUpdate = true
          if (currentTexture) currentTexture.dispose()
          currentTexture = tex
          frontMat.map = tex
          frontMat.color.set(0xffffff)
          frontMat.needsUpdate = true
        },
        undefined,
        () => {
          // 로드 실패: 종이색 앞면 유지(크래시 없이 넘어감)
        },
      )
    }

    // 인터랙션: 드래그로 회전(각도 제한) + 가만두면 아주 천천히 흔들리는 idle.
    let dragging = false
    let lastX = 0
    let lastY = 0
    let tRX = 0 // 목표 회전 X
    let tRY = 0 // 목표 회전 Y
    let cRX = 0 // 현재 회전 X
    let cRY = 0 // 현재 회전 Y
    let idle = Math.random() * Math.PI * 2

    const clamp = (v) => Math.max(-MAX_TILT, Math.min(MAX_TILT, v))
    const onDown = (e) => {
      dragging = true
      lastX = e.clientX
      lastY = e.clientY
      renderer.domElement.style.cursor = 'grabbing'
    }
    const onMove = (e) => {
      if (!dragging) return
      const dx = e.clientX - lastX
      const dy = e.clientY - lastY
      lastX = e.clientX
      lastY = e.clientY
      tRY = clamp(tRY + dx * 0.008)
      tRX = clamp(tRX + dy * 0.008)
    }
    const onUp = () => {
      dragging = false
      renderer.domElement.style.cursor = 'grab'
    }
    renderer.domElement.addEventListener('pointerdown', onDown)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)

    function resize() {
      const w = Math.max(1, mount.clientWidth)
      const h = Math.max(1, mount.clientHeight)
      renderer.setSize(w, h, false)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
    }
    const ro = new ResizeObserver(resize)
    ro.observe(mount)
    resize()

    function animate() {
      raf = requestAnimationFrame(animate)
      let gx = tRX
      let gy = tRY
      if (!dragging) {
        idle += 0.006
        gy = tRY + Math.sin(idle) * 0.11
        gx = tRX + Math.sin(idle * 0.65) * 0.05
      }
      cRX += (gx - cRX) * 0.09
      cRY += (gy - cRY) * 0.09
      card.rotation.x = cRX
      card.rotation.y = cRY
      renderer.render(scene, camera)
    }
    animate()

    apiRef.current = { setTexture }

    return () => {
      disposed = true
      cancelAnimationFrame(raf)
      ro.disconnect()
      renderer.domElement.removeEventListener('pointerdown', onDown)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      if (currentTexture) currentTexture.dispose()
      bodyGeo.dispose()
      frontGeo.dispose()
      groundGeo.dispose()
      bodyMat.dispose()
      frontMat.dispose()
      groundMat.dispose()
      renderer.dispose()
      if (renderer.domElement.parentNode) {
        renderer.domElement.parentNode.removeChild(renderer.domElement)
      }
      apiRef.current = null
    }
  }, [])

  // preview_url 이 바뀌면(템플릿 전환) 기존 텍스처 dispose 후 새로 로드.
  useEffect(() => {
    apiRef.current?.setTexture(previewUrl)
  }, [previewUrl])

  if (failed) {
    const src = fileUrl(previewUrl)
    return (
      <div className="card-3d fallback">
        {src ? (
          <img src={src} alt={label || '명함 시안 미리보기'} />
        ) : (
          <span className="card-3d-empty">미리보기를 준비하고 있어요</span>
        )}
      </div>
    )
  }

  return <div ref={mountRef} className="card-3d" role="img" aria-label={label || '명함 시안 3D 미리보기'} />
}
