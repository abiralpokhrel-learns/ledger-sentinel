.PHONY: setup run pipeline api dashboard test docker-build docker-run compose-up compose-down clean

setup:
	python -m venv .venv
	.venv/Scripts/pip install -r requirements.txt 2>/dev/null || .venv/bin/pip install -r requirements.txt

run: pipeline dashboard

pipeline:
	python data/generate_synthetic_data.py
	python -m app.main

api:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

dashboard:
	streamlit run dashboard/app.py --server.port 8501

test:
	pytest tests/ -q

docker-build:
	docker build -t ledger-sentinel:local .

docker-run:
	docker run --rm -d --name ledger-sentinel -p 8000:8000 -p 8501:8501 ledger-sentinel:local

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

clean:
	rm -f ledger_sentinel.db ledger_sentinel.db-wal ledger_sentinel.db-shm
