#!/usr/bin/env bash
#
# Provision NetBox on a local kind cluster via the pinned netbox-chart
# Helm chart (phase 1 of the E2E test).
#
# This script is safe to run standalone (`make e2e-up`): it creates the
# cluster and deploys NetBox, but it does NOT start a port-forward or
# tear anything down -- that is run.sh's responsibility.
#
# Environment overrides:
#   CLUSTER_NAME               kind cluster name (default netbox-manager-e2e)
#   NAMESPACE                  NetBox namespace (default netbox)
#   NODE_IMAGE                 kind node image (default kindest/node:v1.35.5)
#   CHART_VERSION              pinned netbox-chart version (default 8.3.18)
#   NETBOX_TOKEN               superuser API token (default: random)
#   NETBOX_SUPERUSER_PASSWORD  superuser password (default: random)

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-netbox-manager-e2e}"
NAMESPACE="${NAMESPACE:-netbox}"
# Pin the node image (and thus the Kubernetes version) explicitly: kind's
# built-in default tracks the installed kind release, so an unpinned
# `kind create cluster` would yield a different Kubernetes version on every
# kind bump. Digest-pinned to the image shipped with the kind release in
# playbooks/pre-e2e.yml; re-pin both together. Override for other versions.
NODE_IMAGE="${NODE_IMAGE:-kindest/node:v1.35.5@sha256:ce977ae6d65918d0b58a5f8b5e940429c2ce42fa3a5619ec2bbc60b949c0ac95}"
CHART_VERSION="${CHART_VERSION:-8.3.18}"
NETBOX_TOKEN="${NETBOX_TOKEN:-$(openssl rand -hex 20)}"
NETBOX_SUPERUSER_PASSWORD="${NETBOX_SUPERUSER_PASSWORD:-$(openssl rand -hex 12)}"

echo ">>> Creating kind cluster '${CLUSTER_NAME}'"
if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  echo "    cluster already exists, reusing it"
else
  kind create cluster --name "${CLUSTER_NAME}" --image "${NODE_IMAGE}"
fi

echo ">>> Deploying NetBox (netbox-chart ${CHART_VERSION}) into '${NAMESPACE}'"
# Pass the superuser secrets through a 0600 values file rather than
# `--set`, so the token and password never appear on the helm process
# command line (readable via `ps`/`/proc/<pid>/cmdline` by any local user
# for the up-to-15-minute install).
VALUES="$(mktemp)"
chmod 600 "${VALUES}"
trap 'rm -f "${VALUES}"' EXIT
cat >"${VALUES}" <<EOF
superuser:
  apiToken: "${NETBOX_TOKEN}"
  password: "${NETBOX_SUPERUSER_PASSWORD}"
EOF
# Valkey defaults to replication (1 primary + 3 replicas). On a single-node
# kind cluster the extra replicas don't fit the node's CPU, so one stays
# Pending forever and `helm --wait` times out. A test needs no Redis HA, so
# run a single standalone Valkey pod (valkey.architecture=standalone).
helm upgrade --install netbox \
  oci://ghcr.io/netbox-community/netbox-chart/netbox \
  --version "${CHART_VERSION}" \
  --namespace "${NAMESPACE}" --create-namespace \
  -f "${VALUES}" \
  --set persistence.enabled=false \
  --set postgresql.enabled=true \
  --set valkey.enabled=true \
  --set valkey.architecture=standalone \
  --set replicaCount=1 \
  --wait --timeout 15m
rm -f "${VALUES}"
trap - EXIT

echo ">>> Waiting for the NetBox deployment to become available"
kubectl -n "${NAMESPACE}" rollout status deploy/netbox --timeout=15m

echo
echo "NetBox is deployed."
echo
echo "  Namespace : ${NAMESPACE}"
# Echo the token only for the standalone `make e2e-up` debug workflow; the
# full run sets PRINT_NETBOX_TOKEN=0 to keep the superuser token out of
# logs that may be retained (e.g. CI).
if [[ "${PRINT_NETBOX_TOKEN:-1}" != "0" ]]; then
  echo "  API token : ${NETBOX_TOKEN}"
fi
echo
echo "Port-forward it for local access with:"
echo
echo "  kubectl -n ${NAMESPACE} port-forward svc/netbox 8080:80"
echo
