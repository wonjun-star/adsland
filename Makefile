# 배포(도커/리눅스)용. 윈도우 로컬은 README의 파이썬 직접 실행 명령 참조.
PY ?= python

.PHONY: demo eval test gen-samples ui

gen-samples:
	$(PY) -m synth.generate_clean
	$(PY) -m synth.inject_defects

test:
	$(PY) -m pytest

eval:
	$(PY) -m evals.run_preflight_eval
	$(PY) -m evals.run_dialog_eval

demo:
	$(PY) -m uvicorn api.main:app --host 0.0.0.0 --port 8000
