# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``_run_autoconf_for_devices`` in ``netbox_manager.main`` (#254).

Tier 6 group 4 of the coverage effort tracked in #232. ``_run_autoconf_for_devices``
is the dispatcher: it calls the five task generators, merges the interface-label
tasks into the autoconf ``device_interface`` tasks, optionally applies a
device-name filter across every task source, and then either previews the
result (dry-run) or routes the assembled ``all_tasks_by_type`` payload into
``_write_autoconf_files``.

The five generators (``_generate_loopback_interfaces``,
``_generate_cluster_loopback_tasks``, ``_generate_portchannel_tasks``,
``_generate_device_interface_labels``, ``_generate_autoconf_tasks``) are Tier 5
(#253) / group 3 above -- here they are patched to fixed task lists/dicts so the
merge, filter and routing logic is exercised in isolation. ``_write_autoconf_files``
is Tier 3 (#259) -- most tests spy on it to assert the routing decision and the
handed-over payload, and one integration test lets the real writer run against
``tmp_path``. The pure filter/split helpers are Tier 1 (#247). No live NetBox or
``ansible_runner`` is involved.
"""

import logging
from copy import deepcopy

from netbox_manager import main


def _patch_generators(
    monkeypatch,
    *,
    loopback=None,
    cluster=None,
    portchannel=None,
    labels=None,
    autoconf=None,
):
    """Patch the five autoconf generators to return fixed copies.

    Each generator is replaced with a lambda returning a fresh ``deepcopy`` of
    the supplied value so the dispatcher can mutate/merge freely without the
    fixture data leaking between calls or into the caller's assertions.
    """
    loopback = loopback or []
    cluster = cluster or {}
    portchannel = portchannel or []
    labels = labels or []
    autoconf = autoconf or {"device": [], "device_interface": [], "ip_address": []}

    monkeypatch.setattr(
        main, "_generate_loopback_interfaces", lambda: deepcopy(loopback)
    )
    monkeypatch.setattr(
        main, "_generate_cluster_loopback_tasks", lambda: deepcopy(cluster)
    )
    monkeypatch.setattr(
        main, "_generate_portchannel_tasks", lambda: deepcopy(portchannel)
    )
    monkeypatch.setattr(
        main, "_generate_device_interface_labels", lambda: deepcopy(labels)
    )
    monkeypatch.setattr(main, "_generate_autoconf_tasks", lambda: deepcopy(autoconf))


class _WriteSpy:
    """Records calls to ``_write_autoconf_files`` and returns a sentinel count."""

    def __init__(self, monkeypatch, return_value=1):
        self.calls = []
        self.return_value = return_value
        monkeypatch.setattr(main, "_write_autoconf_files", self._record)

    def _record(self, tasks_by_type, file_prefix, resources_dir=None):
        self.calls.append((tasks_by_type, file_prefix, resources_dir))
        return self.return_value


class TestRunAutoconfForDevices:
    """Group 4 -- ``_run_autoconf_for_devices`` merge, filter and routing."""

    def test_label_merge_passthrough_and_leftovers(self, monkeypatch):
        # node-0:Ethernet3 exists in both autoconf and labels (label side wins),
        # node-1:Ethernet0 is autoconf-only (passthrough), node-9:Ethernet7 is
        # label-only (appended last).
        autoconf = {
            "device": [],
            "device_interface": [
                {
                    "device_interface": {
                        "device": "node-0",
                        "name": "Ethernet3",
                        "primary_mac_address": "aa:bb:cc:00:00:03",
                        "enabled": False,
                    }
                },
                {
                    "device_interface": {
                        "device": "node-1",
                        "name": "Ethernet0",
                        "primary_mac_address": "aa:bb:cc:00:00:10",
                    }
                },
            ],
            "ip_address": [],
        }
        labels = [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "Ethernet3",
                    "label": "uplink",
                    "enabled": True,
                }
            },
            {
                "device_interface": {
                    "device": "node-9",
                    "name": "Ethernet7",
                    "label": "spare",
                }
            },
        ]
        _patch_generators(monkeypatch, labels=labels, autoconf=autoconf)
        spy = _WriteSpy(monkeypatch)

        main._run_autoconf_for_devices(None, "out")

        payload = spy.calls[0][0]
        assert payload["device_interface"] == [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "Ethernet3",
                    "primary_mac_address": "aa:bb:cc:00:00:03",
                    "enabled": True,
                    "label": "uplink",
                }
            },
            {
                "device_interface": {
                    "device": "node-1",
                    "name": "Ethernet0",
                    "primary_mac_address": "aa:bb:cc:00:00:10",
                }
            },
            {
                "device_interface": {
                    "device": "node-9",
                    "name": "Ethernet7",
                    "label": "spare",
                }
            },
        ]

    def test_device_and_ip_address_buckets_pass_through(self, monkeypatch):
        autoconf = {
            "device": [
                {"device": {"name": "node-0", "primary_ip4": "192.168.16.10/32"}}
            ],
            "device_interface": [],
            "ip_address": [{"ip_address": {"address": "10.0.0.1/32"}}],
        }
        _patch_generators(monkeypatch, autoconf=autoconf)
        spy = _WriteSpy(monkeypatch)

        main._run_autoconf_for_devices(None, "out")

        payload = spy.calls[0][0]
        assert payload["device"] == autoconf["device"]
        assert payload["ip_address"] == autoconf["ip_address"]

    def test_device_filter_intersects_every_source(self, monkeypatch):
        loopback = [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "Loopback0",
                    "src": "loopback",
                }
            },
            {
                "device_interface": {
                    "device": "node-1",
                    "name": "Loopback0",
                    "src": "loopback",
                }
            },
        ]
        cluster = {
            "ip_address": [
                {"ip_address": {"device": "node-0", "src": "cluster"}},
                {"ip_address": {"device": "node-1", "src": "cluster"}},
            ]
        }
        portchannel = [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "PortChannel1",
                    "src": "pc",
                }
            },
            {
                "device_interface": {
                    "device": "node-1",
                    "name": "PortChannel1",
                    "src": "pc",
                }
            },
        ]
        autoconf = {
            "device": [
                {"device": {"name": "node-0", "src": "autoconf-dev"}},
                {"device": {"name": "node-1", "src": "autoconf-dev"}},
            ],
            "device_interface": [
                {
                    "device_interface": {
                        "device": "node-0",
                        "name": "Ethernet0",
                        "src": "autoconf-di",
                    }
                },
                {
                    "device_interface": {
                        "device": "node-1",
                        "name": "Ethernet0",
                        "src": "autoconf-di",
                    }
                },
            ],
            "ip_address": [],
        }
        _patch_generators(
            monkeypatch,
            loopback=loopback,
            cluster=cluster,
            portchannel=portchannel,
            autoconf=autoconf,
        )
        spy = _WriteSpy(monkeypatch)

        main._run_autoconf_for_devices({"node-0"}, "out")

        payload = spy.calls[0][0]
        # Every source contributes exactly its node-0 half; no node-1 survives.
        srcs = []
        for tasks in payload.values():
            for task in tasks:
                inner = next(iter(task.values()))
                assert inner.get("device", inner.get("name")) == "node-0"
                srcs.append(inner["src"])
        assert sorted(srcs) == [
            "autoconf-dev",
            "autoconf-di",
            "cluster",
            "loopback",
            "pc",
        ]

    def test_device_filter_none_keeps_everything(self, monkeypatch):
        loopback = [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "Loopback0",
                    "src": "loopback",
                }
            },
            {
                "device_interface": {
                    "device": "node-1",
                    "name": "Loopback0",
                    "src": "loopback",
                }
            },
        ]
        cluster = {
            "ip_address": [
                {"ip_address": {"device": "node-0", "src": "cluster"}},
                {"ip_address": {"device": "node-1", "src": "cluster"}},
            ]
        }
        portchannel = [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "PortChannel1",
                    "src": "pc",
                }
            },
            {
                "device_interface": {
                    "device": "node-1",
                    "name": "PortChannel1",
                    "src": "pc",
                }
            },
        ]
        autoconf = {
            "device": [
                {"device": {"name": "node-0", "src": "autoconf-dev"}},
                {"device": {"name": "node-1", "src": "autoconf-dev"}},
            ],
            "device_interface": [
                {
                    "device_interface": {
                        "device": "node-0",
                        "name": "Ethernet0",
                        "src": "autoconf-di",
                    }
                },
                {
                    "device_interface": {
                        "device": "node-1",
                        "name": "Ethernet0",
                        "src": "autoconf-di",
                    }
                },
            ],
            "ip_address": [],
        }
        _patch_generators(
            monkeypatch,
            loopback=loopback,
            cluster=cluster,
            portchannel=portchannel,
            autoconf=autoconf,
        )
        spy = _WriteSpy(monkeypatch)

        main._run_autoconf_for_devices(None, "out")

        payload = spy.calls[0][0]
        pairs = set()
        for tasks in payload.values():
            for task in tasks:
                inner = next(iter(task.values()))
                pairs.add((inner["src"], inner.get("device", inner.get("name"))))
        # Both devices survive for every source when no filter is applied.
        assert pairs == {
            ("loopback", "node-0"),
            ("loopback", "node-1"),
            ("cluster", "node-0"),
            ("cluster", "node-1"),
            ("pc", "node-0"),
            ("pc", "node-1"),
            ("autoconf-dev", "node-0"),
            ("autoconf-dev", "node-1"),
            ("autoconf-di", "node-0"),
            ("autoconf-di", "node-1"),
        }

    def test_dry_run_previews_and_writes_nothing(self, monkeypatch, tmp_path, caplog):
        # The real writer is left in place; the dry-run path must return before
        # reaching it, so no file appears and the writer is never invoked.
        loopback = [{"device_interface": {"device": "node-0", "name": "Loopback0"}}]
        cluster = {"ip_address": [{"ip_address": {"device": "node-0"}}]}
        autoconf = {
            "device": [],
            "device_interface": [
                {"device_interface": {"device": "node-0", "name": "Ethernet0"}}
            ],
            "ip_address": [],
        }
        _patch_generators(
            monkeypatch, loopback=loopback, cluster=cluster, autoconf=autoconf
        )
        out = tmp_path / "out"

        with caplog.at_level(logging.INFO):
            result = main._run_autoconf_for_devices(None, str(out), dryrun=True)

        assert result == 0
        assert not out.exists()
        assert "would generate the following loopback interface tasks:" in caplog.text
        assert (
            "would generate the following cluster-based loopback IP tasks:"
            in caplog.text
        )
        assert "would generate the following other autoconf tasks:" in caplog.text
        # PortChannel source was empty, so its preview header is absent.
        assert "PortChannel LAG interface tasks:" not in caplog.text

    def test_file_write_path_routes_assembled_payload(self, monkeypatch, tmp_path):
        loopback = [{"device_interface": {"device": "node-0", "name": "Loopback0"}}]
        cluster = {
            "ip_address": [
                {"ip_address": {"device": "node-0", "address": "10.0.0.1/32"}}
            ]
        }
        portchannel = [
            {
                "device_interface": {
                    "device": "node-0",
                    "name": "PortChannel1",
                    "type": "lag",
                }
            }
        ]
        autoconf = {
            "device": [{"device": {"name": "node-0"}}],
            "device_interface": [
                {"device_interface": {"device": "node-0", "name": "Ethernet0"}}
            ],
            "ip_address": [],
        }
        _patch_generators(
            monkeypatch,
            loopback=loopback,
            cluster=cluster,
            portchannel=portchannel,
            autoconf=autoconf,
        )
        spy = _WriteSpy(monkeypatch, return_value=7)

        result = main._run_autoconf_for_devices(
            None, str(tmp_path), prefix="299-autoconf"
        )

        assert result == 7
        expected_payload = {
            # loopback -> then autoconf -> then portchannel appended last.
            "device_interface": [
                {"device_interface": {"device": "node-0", "name": "Loopback0"}},
                {"device_interface": {"device": "node-0", "name": "Ethernet0"}},
                {
                    "device_interface": {
                        "device": "node-0",
                        "name": "PortChannel1",
                        "type": "lag",
                    }
                },
            ],
            "ip_address": [
                {"ip_address": {"device": "node-0", "address": "10.0.0.1/32"}}
            ],
            "device": [{"device": {"name": "node-0"}}],
        }
        assert spy.calls == [(expected_payload, "299-autoconf", str(tmp_path))]

    def test_file_write_path_writes_real_files(self, monkeypatch, tmp_path):
        # Integration with the Tier 3-covered writer: the count equals the number
        # of non-empty buckets and the expected files land on disk.
        loopback = [{"device_interface": {"device": "node-0", "name": "Loopback0"}}]
        autoconf = {
            "device": [{"device": {"name": "node-0"}}],
            "device_interface": [],
            "ip_address": [],
        }
        _patch_generators(monkeypatch, loopback=loopback, autoconf=autoconf)

        result = main._run_autoconf_for_devices(
            None, str(tmp_path), prefix="299-autoconf"
        )

        assert result == 2
        assert (tmp_path / "299-autoconf-device-interface.yml").is_file()
        assert (tmp_path / "299-autoconf-device.yml").is_file()

    def test_returns_zero_when_filter_removes_everything(self, monkeypatch):
        loopback = [{"device_interface": {"device": "node-9", "name": "Loopback0"}}]
        cluster = {"ip_address": [{"ip_address": {"device": "node-9"}}]}
        portchannel = [
            {"device_interface": {"device": "node-9", "name": "PortChannel1"}}
        ]
        autoconf = {
            "device": [],
            "device_interface": [
                {"device_interface": {"device": "node-9", "name": "Ethernet0"}}
            ],
            "ip_address": [],
        }
        _patch_generators(
            monkeypatch,
            loopback=loopback,
            cluster=cluster,
            portchannel=portchannel,
            autoconf=autoconf,
        )
        spy = _WriteSpy(monkeypatch)

        result = main._run_autoconf_for_devices({"node-0"}, "out")

        assert result == 0
        assert spy.calls == []

    def test_returns_zero_when_generators_empty(self, monkeypatch):
        _patch_generators(monkeypatch)
        spy = _WriteSpy(monkeypatch)

        result = main._run_autoconf_for_devices(None, "out")

        assert result == 0
        assert spy.calls == []
