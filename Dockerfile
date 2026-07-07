# 1단계: React UI 빌드
FROM node:22-slim AS ui
WORKDIR /ui
COPY demo-ui/package*.json ./
RUN npm ci
COPY demo-ui/ ./
RUN npm run build

# 2단계: 파이썬 런타임
FROM python:3.12-slim
WORKDIR /app

# Ghostscript: EPS → 래스터 변환에 필요 (없으면 EPS는 'PDF로 저장' 안내로 폴백).
# pikepdf(qpdf)·pdfium 휠은 manylinux 제공이라 그 외 시스템 패키지는 불필요.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi "uvicorn[standard]" pikepdf pypdfium2 reportlab Pillow numpy \
    "pydantic>=2" "sqlalchemy>=2" pyyaml anthropic httpx python-multipart

COPY core/ core/
COPY synth/ synth/
COPY evals/ evals/
COPY api/ api/
COPY docs/ docs/
COPY --from=ui /ui/dist demo-ui/dist

# 샘플 PDF는 이미지 빌드 시 생성 (시드 고정 → 재현 가능)
RUN python -m synth.generate_clean && python -m synth.inject_defects

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
