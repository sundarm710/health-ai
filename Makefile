JAVA ?= /opt/homebrew/opt/openjdk@17/bin/java
.PHONY: up down reset psql db-ui sim-seed sim-api sim demo synthea corpus baseline charts test review

up:        ## start Postgres + HAPI FHIR server (http://localhost:8080) and wait until healthy
	docker compose up -d
	@printf 'waiting for HAPI'; until curl -sf localhost:8080/fhir/metadata -o /dev/null; do printf .; sleep 3; done; echo ' ready'

down:      ## stop containers, keep data
	docker compose down

reset:     ## stop containers AND delete the Postgres volume (all FHIR data)
	docker compose down -v

psql:      ## SQL shell into the HAPI database
	docker compose exec db psql -U hapi -d hapi

db-ui:     ## open the visual DB browser (login pre-filled; password is `hapi`)
	open "http://localhost:8081/?pgsql=db&username=hapi&db=hapi&ns=public"

sim-seed:  ## seed 1 hospital + 2 doctors + 2 nurses into HAPI (idempotent; needs `make up`)
	uv run python -m fhirlab.sim.seed

sim-api:   ## NICU middleware (FastAPI) on :8000, docs at /docs  [terminal 1]
	uv run uvicorn fhirlab.sim.api:app --port 8000 --reload

sim:       ## console (Client · Server · Database pages) on :8502; needs sim-api running  [terminal 2]
	uv run streamlit run app/sim_app.py --server.port 8502

demo:      ## narrated tour of every FHIR REST interaction (needs `make up`)
	uv run python -m fhirlab.client demo

synthea:   ## 500 patients, seed 42 -> data/synthea/fhir
	$(JAVA) -jar tools/synthea-with-dependencies.jar -p 500 -s 42 -cs 42 \
	  --exporter.baseDirectory=data/synthea --exporter.years_of_history=5 \
	  --exporter.hospital.fhir.export=false --exporter.practitioner.fhir.export=false \
	  --exporter.csv.export=false --exporter.ccda.export=false \
	  --generate.only_alive_patients=true Massachusetts

corpus:    ## facts -> clean v2 + golden -> 5 corruption levels
	uv run python -m fhirlab.corpus.build

baseline:  ## rules-only over dev+test, all levels
	uv run python -m fhirlab.eval.run --config rules_only

charts:
	uv run python -m fhirlab.eval.charts

test:
	uv run pytest -q

review:    ## expert annotation + trends UI on :8501
	uv run streamlit run app/review_app.py
