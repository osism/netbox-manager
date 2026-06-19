CLUSTER_NAME ?= netbox-manager-e2e
export CLUSTER_NAME

.PHONY: e2e e2e-up e2e-down

# Provision kind + NetBox, apply example/, assert the API state, tear down.
e2e:
	tests/e2e/run.sh

# Provision kind + NetBox and leave it running for debugging.
e2e-up:
	tests/e2e/deploy_netbox.sh

# Delete the kind cluster created by the E2E test.
e2e-down:
	kind delete cluster --name $(CLUSTER_NAME)
