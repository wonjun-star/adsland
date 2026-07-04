import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// dev 서버에서 /api 요청은 FastAPI(localhost:8000)로 프록시한다.
// 빌드 산출물(dist)은 FastAPI가 /에서 직접 서빙하므로 프록시가 필요 없다.
export default defineConfig({
  plugins: [react()],
  // three.js 를 함께 번들하면 단일 청크가 500kB 를 넘는다(오프라인/포터블 동작을 위해 의도된 것).
  // 런타임 CDN 요청이 없으니 청크 크기 경고 한계를 올려 빌드를 깔끔하게 유지한다.
  build: {
    chunkSizeWarningLimit: 1200,
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
