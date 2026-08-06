.PHONY: install lint test bootstrap-tfstate plan apply clean

PYTHON := python3.12
VENV := .venv

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements-dev.txt
	$(VENV)/bin/pre-commit install

lint:
	$(VENV)/bin/pre-commit run --all-files

test:
	$(VENV)/bin/pytest tests/ -v

bootstrap-tfstate:
	./scripts/bootstrap_tfstate.sh

plan:
	cd terraform/envs/prod && terraform init && terraform plan

apply:
	cd terraform/envs/prod && terraform apply

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
