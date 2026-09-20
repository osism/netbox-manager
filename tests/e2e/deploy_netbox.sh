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
#   CHART_VERSION              pinned netbox-chart version (default 8.2.5)
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
# Chart 8.2.5 ships NetBox v4.5.10. NetBox v4.5 introduced peppered "v2"
# API tokens (`Authorization: Bearer nbt_<key>.<secret>`), and this chart
# auto-generates a token pepper -- so its bootstrap now only ever creates a
# v2 token, and `superuser.apiToken` is never materialised as a plaintext
# "v1" token. pynetbox / netbox.netbox still send `Authorization: Token
# <key>` (a v1 token), which NetBox keeps accepting through v4.6 (legacy v1
# support is removed in v4.7). So instead of relying on superuser.apiToken
# we mint our own deterministic v1 token after the rollout (see below).
# Re-pin deliberately, and re-check the token step, when bumping to v4.7.
CHART_VERSION="${CHART_VERSION:-8.2.5}"
NETBOX_TOKEN="${NETBOX_TOKEN:-$(openssl rand -hex 20)}"
NETBOX_SUPERUSER_PASSWORD="${NETBOX_SUPERUSER_PASSWORD:-$(openssl rand -hex 12)}"

echo ">>> Creating kind cluster '${CLUSTER_NAME}'"
if kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  echo "    cluster already exists, reusing it"
else
  kind create cluster --name "${CLUSTER_NAME}" --image "${NODE_IMAGE}"
fi

echo ">>> Deploying NetBox (netbox-chart ${CHART_VERSION}) into '${NAMESPACE}'"
# Pass the superuser password through a 0600 values file rather than
# `--set`, so it never appears on the helm process command line (readable
# via `ps`/`/proc/<pid>/cmdline` by any local user for the up-to-15-minute
# install). The API token is deliberately not set here: on NetBox 4.5 the
# chart bootstrap only creates a v2 token (see the CHART_VERSION note), so
# the client-usable v1 token is minted separately after the rollout.
VALUES="$(mktemp)"
chmod 600 "${VALUES}"
trap 'rm -f "${VALUES}"' EXIT
#
# The worker's wait-for-backend init container defaults to
# `kubectl rollout status deployment netbox`, which fails as soon as the
# netbox Deployment trips its progressDeadlineSeconds (the Kubernetes
# default, 600s -- the chart sets no value for it). NetBox routinely needs
# ~10 minutes to answer /login/ on a CI node, so the deadline fires while
# the pod is still starting, the init container exits 1 and enters
# CrashLoopBackOff, and it then stays locked out for a further backoff
# interval (up to 300s) after NetBox is actually ready -- long enough for
# the helm --wait budget below to expire. Waiting on the Deployment's
# Available condition instead reaches the same gate without ever reading
# the Progressing condition, so the deadline is irrelevant to it.
#
# NOTE: `\$(DEPLOYMENT_NAME)` is escaped on purpose. This heredoc is
# unquoted, so a bare `$(...)` would be command-substituted by the shell
# and silently render `deployment/` -- it must reach the container verbatim
# for Kubernetes to expand it from the container's own env.
#
# This waits for availability, not for a completed rollout. The two are
# equivalent only for a fresh single-replica install, which is what CI
# does; on a reused cluster (see the kind block above) this script runs
# `helm upgrade`, where an old ReplicaSet can keep the Deployment Available
# while the new one is still rolling out.
cat >"${VALUES}" <<EOF
superuser:
  password: "${NETBOX_SUPERUSER_PASSWORD}"
worker:
  waitForBackend:
    args:
      - wait
      - --for=condition=Available
      - deployment/\$(DEPLOYMENT_NAME)
      - --timeout=15m
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
# Deliberately `wait --for=condition=Available` rather than `rollout
# status`: the latter reads the Progressing condition, so it fails whenever
# that condition still reads ProgressDeadlineExceeded. Whether it still does
# once NetBox is serving is an open question -- an isolated reproduction has
# the condition clearing as soon as the Deployment goes Available, while one
# observed CI failure points the other way and has not been explained. `wait`
# never reads the condition, so it is correct either way, which is why this
# does not wait on that question being settled. Same reasoning as the worker
# override above.
kubectl -n "${NAMESPACE}" wait --for=condition=Available deploy/netbox --timeout=15m

# Mint a deterministic v1 API token for the superuser. The chart bootstrap
# only creates a peppered v2 token with a random key (see the CHART_VERSION
# note), which pynetbox / netbox.netbox cannot use -- they authenticate with
# `Authorization: Token <key>`, i.e. a v1 token, which NetBox 4.5/4.6 still
# accept. We create one whose key is NETBOX_TOKEN so the client has a known
# token. The script is fed over stdin (not `shell -c`) so the token never
# lands on a command line inside the pod.
echo ">>> Creating a deterministic v1 API token for the superuser"
kubectl -n "${NAMESPACE}" exec -i deploy/netbox -- \
  /opt/netbox/netbox/manage.py shell --interface python <<PYEOF
from django.contrib.auth import get_user_model
from users.models import Token
from users.choices import TokenVersionChoices
user = get_user_model().objects.get(username="admin")
Token.objects.filter(user=user).delete()
Token.objects.create(user=user, version=TokenVersionChoices.V1, token="${NETBOX_TOKEN}")
print("Created v1 token for", user.username)
PYEOF

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
