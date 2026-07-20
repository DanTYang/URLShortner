# Convenience wrapper around the common workflows. Run `make help` for a list.
#
# The real work is done by the AWS SAM CLI and pytest; these targets just
# capture the exact invocations so you never have to remember the flags.

.DEFAULT_GOAL := help
ENV ?= dev

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install test/dev dependencies
	python3 -m pip install -r tests/requirements.txt

.PHONY: test
test: ## Run the unit test suite (offline, mocked AWS)
	python3 -m pytest

.PHONY: build
build: ## Build all functions & layers with SAM
	sam build

.PHONY: validate
validate: ## Validate & lint the SAM/CloudFormation template
	sam validate --lint

.PHONY: deploy
deploy: build ## Build then deploy (ENV=dev|staging|prod)
	sam deploy --config-env $(ENV)

.PHONY: deploy-guided
deploy-guided: build ## First-time interactive deploy
	sam deploy --guided

.PHONY: local
local: ## Run the API locally at http://127.0.0.1:3000 (needs Docker)
	sam local start-api

.PHONY: logs
logs: ## Tail logs for the redirect function (ENV=dev|staging|prod)
	sam logs --stack-name url-shortener-$(ENV) --name RedirectFunction --tail

.PHONY: outputs
outputs: ## Print the deployed stack's outputs (API URL, dashboard, ...)
	aws cloudformation describe-stacks --stack-name url-shortener-$(ENV) \
		--query 'Stacks[0].Outputs' --output table

.PHONY: delete
delete: ## Tear down the whole stack (ENV=dev|staging|prod)
	sam delete --stack-name url-shortener-$(ENV)
