#!/usr/bin/env bash
#
# End-to-end test for netbox-manager (phases 1-3):
#
#   1. provision NetBox on a local kind cluster (deploy_netbox.sh)
#   2. apply the bundled example/ data with `netbox-manager run`
#   3. assert the resulting API state (verify.py + `netbox-manager validate`)
#
# A kind cluster created by this run is always torn down on exit --
# including on failure -- so a broken run never leaks a cluster. A
# pre-existing cluster of the same name (e.g. a `make e2e-up` debug
# cluster) is reused and left in place.
#
# Must be run from a checkout with the example/ data present; the helper
# scripts and netbox-manager itself need to be on PATH (activate the
# virtualenv first, e.g. `pipenv run make e2e`).
#
# Environment overrides: see deploy_netbox.sh, plus
#   NETBOX_TOKEN   shared superuser/client API token (default: random)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

if [[ ! -d example/resources ]]; then
  echo "error: must be run from the netbox-manager repository root" >&2
  exit 1
fi

CLUSTER_NAME="${CLUSTER_NAME:-netbox-manager-e2e}"
NAMESPACE="${NAMESPACE:-netbox}"
# A single deterministic token shared by the chart superuser and the
# netbox-manager client; deploy_netbox.sh inherits it via the environment.
NETBOX_TOKEN="${NETBOX_TOKEN:-$(openssl rand -hex 20)}"
export CLUSTER_NAME NAMESPACE NETBOX_TOKEN

# Detect cluster ownership before provisioning: deploy_netbox.sh reuses a
# pre-existing cluster of this name, so we must only tear down a cluster
# this run actually created -- never a reused `make e2e-up` debug cluster
# or an unrelated cluster that happens to share the name.
CREATED_CLUSTER=0
if ! kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  CREATED_CLUSTER=1
fi

PF_PID=""
# Dump cluster state to stdout (always captured in the CI job log) so a
# failed run is debuggable -- the cluster is torn down on exit, taking any
# pod logs with it, so we must snapshot before that happens.
dump_diagnostics() {
  echo "==================== kind / NetBox diagnostics ===================="
  kubectl get nodes -o wide 2>&1 || true
  echo "----- pods (all namespaces) -----"
  kubectl get pods -A -o wide 2>&1 || true
  echo "----- events (${NAMESPACE}, recent) -----"
  kubectl -n "${NAMESPACE}" get events --sort-by=.lastTimestamp 2>&1 | tail -n 60 || true
  for p in $(kubectl -n "${NAMESPACE}" get pods -o name 2>/dev/null); do
    echo "----- describe ${p} -----"
    kubectl -n "${NAMESPACE}" describe "${p}" 2>&1 || true
    echo "----- logs ${p} (current) -----"
    kubectl -n "${NAMESPACE}" logs "${p}" --all-containers --tail=80 2>&1 || true
    echo "----- logs ${p} (previous) -----"
    kubectl -n "${NAMESPACE}" logs "${p}" --all-containers --previous --tail=80 2>&1 || true
  done
  echo "=================================================================="
}
cleanup() {
  rc=$?
  if [[ -n "${PF_PID}" ]]; then
    kill "${PF_PID}" 2>/dev/null || true
  fi
  if [[ "${rc}" -ne 0 ]]; then
    echo ">>> E2E run failed (exit ${rc}); dumping cluster diagnostics before teardown"
    dump_diagnostics || true
  fi
  if [[ "${CREATED_CLUSTER}" == "1" ]]; then
    echo ">>> Deleting kind cluster '${CLUSTER_NAME}'"
    kind delete cluster --name "${CLUSTER_NAME}" || true
  else
    echo ">>> Leaving pre-existing kind cluster '${CLUSTER_NAME}' in place"
  fi
}
trap cleanup EXIT

# --- Phase 1: provision NetBox on kind -------------------------------------
# Suppress the API token in deploy_netbox.sh's summary: the full run does
# not need it echoed, and this run's logs may be retained (e.g. CI).
PRINT_NETBOX_TOKEN=0 tests/e2e/deploy_netbox.sh

echo ">>> Port-forwarding svc/netbox -> 127.0.0.1:8080"
kubectl -n "${NAMESPACE}" port-forward svc/netbox 8080:80 &
PF_PID=$!

# Give the port-forward a moment to start accepting connections;
# netbox-manager additionally waits for the NetBox API to become ready.
ready=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:8080/api/" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready}" != "1" ]]; then
  echo "error: NetBox API not reachable on 127.0.0.1:8080 after 30s" >&2
  exit 1
fi
# A still-probing curl can succeed against a different service if 8080 was
# already taken (kubectl then failed to bind and exited): fail loudly
# rather than testing against the wrong target.
if ! kill -0 "${PF_PID}" 2>/dev/null; then
  echo "error: port-forward exited early (is 127.0.0.1:8080 already in use?)" >&2
  exit 1
fi

# --- Phase 2: apply example/ with netbox-manager ---------------------------
export NETBOX_MANAGER_URL="http://127.0.0.1:8080"
export NETBOX_MANAGER_TOKEN="${NETBOX_TOKEN}"
export NETBOX_MANAGER_DEVICETYPE_LIBRARY="example/devicetypes"
export NETBOX_MANAGER_MODULETYPE_LIBRARY="example/moduletypes"
export NETBOX_MANAGER_RESOURCES="example/resources"
export NETBOX_MANAGER_IGNORE_SSL_ERRORS=true

echo ">>> Installing the netbox.netbox Ansible collection"
ansible-galaxy collection install -r requirements.yml

echo ">>> Applying example/ with 'netbox-manager run --fail-fast'"
netbox-manager run --fail-fast

# --- Phase 3: verify -------------------------------------------------------
echo ">>> Verifying API state (tests/e2e/verify.py)"
python3 tests/e2e/verify.py

echo ">>> Running 'netbox-manager validate'"
netbox-manager validate

echo ">>> E2E test passed."
