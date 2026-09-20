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
WATCH_PID=""
# Sample the rollout every 10s while NetBox installs. The states that decide
# whether this run passes are transient: the netbox Deployment's Progressing
# condition flipping to ProgressDeadlineExceeded (and back, once the pod goes
# Ready), and the worker's wait-for-backend restart count climbing while it is
# locked out. A post-mortem dump taken minutes later cannot recover any of
# them, so they have to be recorded as they happen -- on passing runs too,
# since a pass only tells us something if we know whether it crossed the
# deadline at all.
watch_rollout() {
  # --context: this starts before deploy_netbox.sh creates the cluster, so
  # without it the first samples would go to whatever context happens to be
  # current -- possibly a real cluster with a netbox namespace. Until the kind
  # cluster exists the context does not resolve, kubectl exits non-zero, and
  # the guard below simply skips the sample.
  # --request-timeout: a wedged API server must not silently stop evidence
  # collection; `|| true` catches failures, not hangs.
  local ctx="kind-${CLUSTER_NAME}"
  while :; do
    ts="$(date -u +%H:%M:%SZ)"
    cond="$(kubectl --context "${ctx}" --request-timeout=5s \
      -n "${NAMESPACE}" get deploy netbox \
      -o jsonpath='{range .status.conditions[*]}{.type}={.status}/{.reason} {end}' \
      2>/dev/null || true)"
    init="$(kubectl --context "${ctx}" --request-timeout=5s \
      -n "${NAMESPACE}" get pods \
      -l app.kubernetes.io/component=worker \
      -o jsonpath='{range .items[*]}restarts={.status.initContainerStatuses[0].restartCount} ready={.status.initContainerStatuses[0].ready}{end}' \
      2>/dev/null || true)"
    if [[ -n "${cond}${init}" ]]; then
      echo "[watch ${ts}] deploy/netbox ${cond}| wait-for-backend ${init}"
    fi
    sleep 10
  done
}

# Condition timestamps, restart counts: the minimum needed to tell whether a
# run crossed the 600s progress deadline and whether the worker survived it.
# Cheap enough to emit on every run, unlike the full dump below.
#
# Every diagnostic request is bounded with --request-timeout. This runs from
# cleanup() on every exit, ahead of `kind delete cluster`, so an unresponsive
# API server would otherwise hang teardown until Zuul kills the job at the
# 40-minute mark -- turning a clean failure into a lost one. `|| true` covers
# a command that fails, not one that never returns.
dump_rollout_summary() {
  echo "----- deployments (${NAMESPACE}) -----"
  kubectl --request-timeout=10s -n "${NAMESPACE}" get deploy -o wide 2>&1 || true
  for d in $(kubectl --request-timeout=10s -n "${NAMESPACE}" get deploy -o name 2>/dev/null); do
    echo "--- ${d} conditions ---"
    kubectl --request-timeout=10s -n "${NAMESPACE}" get "${d}" \
      -o jsonpath='{range .status.conditions[*]}{.type}={.status}/{.reason} (since {.lastTransitionTime}){"\n"}{end}' \
      2>&1 || true
  done
  echo "----- pod readiness transitions (${NAMESPACE}) -----"
  kubectl --request-timeout=10s -n "${NAMESPACE}" get pods \
    -o jsonpath='{range .items[*]}{.metadata.name}{"  Ready="}{range .status.conditions[?(@.type=="Ready")]}{.status}{" since "}{.lastTransitionTime}{end}{"\n"}{end}' \
    2>&1 || true
  echo "----- pods (${NAMESPACE}) -----"
  kubectl --request-timeout=10s -n "${NAMESPACE}" get pods -o wide 2>&1 || true
}

# Dump cluster state to stdout (always captured in the CI job log) so a
# failed run is debuggable -- the cluster is torn down on exit, taking any
# pod logs with it, so we must snapshot before that happens.
dump_diagnostics() {
  echo "==================== kind / NetBox diagnostics ===================="
  kubectl --request-timeout=10s get nodes -o wide 2>&1 || true
  echo "----- pods (all namespaces) -----"
  kubectl --request-timeout=10s get pods -A -o wide 2>&1 || true
  echo "----- events (${NAMESPACE}, recent) -----"
  kubectl --request-timeout=10s -n "${NAMESPACE}" get events --sort-by=.lastTimestamp 2>&1 | tail -n 60 || true
  for p in $(kubectl --request-timeout=10s -n "${NAMESPACE}" get pods -o name 2>/dev/null); do
    echo "----- describe ${p} -----"
    kubectl --request-timeout=10s -n "${NAMESPACE}" describe "${p}" 2>&1 || true
    echo "----- logs ${p} (current) -----"
    kubectl --request-timeout=10s -n "${NAMESPACE}" logs "${p}" --all-containers --timestamps --tail=80 2>&1 || true
    echo "----- logs ${p} (previous) -----"
    kubectl --request-timeout=10s -n "${NAMESPACE}" logs "${p}" --all-containers --timestamps --previous --tail=80 2>&1 || true
  done
  echo "=================================================================="
}
cleanup() {
  rc=$?
  if [[ -n "${PF_PID}" ]]; then
    kill "${PF_PID}" 2>/dev/null || true
  fi
  if [[ -n "${WATCH_PID}" ]]; then
    kill "${WATCH_PID}" 2>/dev/null || true
  fi
  # The summary goes out on success as well: a green run is only evidence
  # about the progress-deadline race if we can see whether it hit it.
  echo ">>> Rollout summary (exit ${rc})"
  dump_rollout_summary || true
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
# The watch only needs to cover the install: the progress-deadline race is
# decided inside helm's wait, and leaving it running for the rest of the
# suite would add noise without adding evidence.
watch_rollout &
WATCH_PID=$!

# Suppress the API token in deploy_netbox.sh's summary: the full run does
# not need it echoed, and this run's logs may be retained (e.g. CI).
PRINT_NETBOX_TOKEN=0 tests/e2e/deploy_netbox.sh

kill "${WATCH_PID}" 2>/dev/null || true
WATCH_PID=""

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
