.PHONY: demo data run improve review serve test large clean help

PY ?= python3

help:
	@echo "make demo     - generate data, cold run, review, re-run, print the improvement table"
	@echo "make data     - generate the sample month (planted anomalies + answer key)"
	@echo "make run      - one reconciliation, scored against ground truth"
	@echo "make improve  - 4 rounds of run -> review -> run"
	@echo "make serve    - the human review desk on :8000"
	@echo "make test     - the test suite"
	@echo "make large    - the same thing on a 518-row month"
	@echo "make clean    - delete data/ and reports/"

data:
	$(PY) outlier.py generate --out sample

# --provider mock pins the deterministic offline model so every target below is
# reproducible even on a machine with live API keys set (otherwise `auto`
# picks up any ambient key and hits the network).
run: data
	$(PY) outlier.py run --provider mock

demo: clean data
	@echo "\n=== ROUND 1: cold start, no rules in memory ==="
	$(PY) outlier.py run --run-id RUN-COLD --provider mock
	@echo "\n=== reviewer works the queue (simulated) ==="
	$(PY) outlier.py review --run RUN-COLD
	@echo "\n=== ROUND 2: same month, with what the reviewer taught it ==="
	$(PY) outlier.py run --run-id RUN-WARM --round 2 --provider mock
	@echo "\n=== full learning curve, 4 rounds ==="
	rm -f data/outlier.db
	$(PY) outlier.py improve --rounds 4 --provider mock
	@echo "\nwrote reports/  -> open reports/IMPROVEMENT.md and the reconciliation reports"

improve: data
	$(PY) outlier.py improve --rounds 4 --provider mock

review: data
	$(PY) outlier.py run --run-id RUN-DESK --provider mock
	$(PY) outlier.py serve

serve:
	$(PY) outlier.py serve

test:
	$(PY) -m pytest -q

large:
	$(PY) outlier.py generate --out big --size large
	$(PY) outlier.py run --provider mock --bank big/bank_statement.csv --ledger big/ledger_export.csv \
	    --truth big/ground_truth.json --run-id RUN-LARGE

clean:
	rm -rf data reports
