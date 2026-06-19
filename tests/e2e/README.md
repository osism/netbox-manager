# End-to-end tests

This directory contains a real end-to-end (E2E) test for
`netbox-manager`. It exercises the full apply path against a live
NetBox instead of mocking it:

1. **Provision** — deploy a fresh NetBox on a local
   [kind](https://kind.sigs.k8s.io/) cluster using the pinned
   [netbox-chart](https://github.com/netbox-community/netbox-chart)
   Helm chart.
2. **Apply** — run `netbox-manager run --fail-fast` against the bundled
   `example/` data (device types + numbered resources).
3. **Verify** — assert the resulting state through the NetBox REST API
   with `pynetbox` (`verify.py`) and run `netbox-manager validate`.

A cluster created by the run is always torn down on exit, including on
failure; a pre-existing cluster of the same name is reused and left in
place.

## Prerequisites

Install these and make sure the Docker daemon is running:

- [Docker](https://docs.docker.com/get-docker/) (or Podman) — kind needs
  a container runtime
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/)
- `netbox-manager` and `pynetbox` on the active Python environment:

  ```bash
  pip install -e .
  ```

NetBox + PostgreSQL + Valkey on kind need a few GB of free RAM. The run
is kept minimal with `persistence.enabled=false` and `replicaCount=1`.

## Usage

From the repository root:

```bash
make e2e        # provision, apply, assert, then tear down
make e2e-up     # provision + deploy only, leave the cluster running
make e2e-down   # delete the kind cluster
```

`make e2e-up` prints the generated API token and the `kubectl
port-forward` command so you can poke at NetBox manually while
debugging.

## Configuration

The scripts read these environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `CLUSTER_NAME` | `netbox-manager-e2e` | kind cluster name |
| `NAMESPACE` | `netbox` | Kubernetes namespace for NetBox |
| `NODE_IMAGE` | `kindest/node:v1.35.5` | kind node image (pins the Kubernetes version) |
| `CHART_VERSION` | `8.3.18` | pinned `netbox-chart` version |
| `NETBOX_TOKEN` | random | superuser/client API token |
| `NETBOX_SUPERUSER_PASSWORD` | random | superuser password |

The chart version is pinned for reproducibility; bump it deliberately
and re-pin the expected counts (see below) when NetBox changes.

## What is asserted

`verify.py` checks the values pinned in `expected.py`:

- exact object counts (devices, interfaces, cables, IPs, MACs, VLANs,
  prefixes, tenant/site/location, rack);
- the 16 device names;
- tenant/site/location slugs;
- the OOB VLAN (vid 100, role `OOB`);
- the four prefixes;
- `testbed-node-0:Ethernet0` as an access port with the `OOB Testbed`
  untagged VLAN;
- `192.168.16.10/32` assigned to `testbed-node-0:Loopback0`;
- `18:C0:86:3A:B7:F1` on `testbed-node-0:Ethernet0`;
- the primary IPv4/IPv6 and OOB IP wiring of `testbed-node-0`.

`run.sh` then runs `netbox-manager validate`, which must exit `0`.

## Re-pinning the expected counts

`example/` is overlaid from `osism/testbed` via gilt, so the assertions
must evolve with it. The exact counts in `expected.py` come from a
known-good baseline run. After changing `example/` (or bumping
`CHART_VERSION`), regenerate them:

```bash
make e2e-up
kubectl -n netbox port-forward svc/netbox 8080:80 &
export NETBOX_MANAGER_URL=http://127.0.0.1:8080
export NETBOX_MANAGER_TOKEN=<token printed by make e2e-up>
export NETBOX_MANAGER_IGNORE_SSL_ERRORS=true
# ... run `netbox-manager run`, inspect the live counts, update
# expected.py, then re-run tests/e2e/verify.py until it is green.
make e2e-down
```

`dcim.interfaces` in particular is not a stanza count: NetBox
auto-instantiates one interface per device-type interface template on
device creation, so its expected value must come from a baseline run.

## CI

The E2E test runs as the `netbox-manager-e2e` Zuul job in the
`periodic-daily` pipeline only — not in the PR `check` gate, which stays
fast (linters + unit tests). See `.zuul.yaml` and
`playbooks/pre-e2e.yml` / `playbooks/test-e2e.yml`.

`pre-e2e.yml` sets `net.ipv6.conf.*.accept_ra=2` on the node before kind
runs. The CI node is IPv6-only and gets its default route via SLAAC /
Router Advertisements; when kind makes Docker create a dual-stack network
Docker enables `net.ipv6.conf.all.forwarding`, and with the default
`accept_ra=1` the kernel stops honouring RAs — the default route then
lapses and the node goes unreachable mid-run. `accept_ra=2` keeps RAs
honoured while forwarding is on, so the route survives.
