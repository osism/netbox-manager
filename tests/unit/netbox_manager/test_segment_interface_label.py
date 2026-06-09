# SPDX-License-Identifier: Apache-2.0

"""Tests for segment-level device interface label resolution (issue #220).

The interface label of a source device (switch/router/firewall) comes from its
``device_interface_label`` custom field, falling back to the
``_segment_device_interface_label`` segment config context key when the custom
field is not set. The per-device custom field always wins.
"""

from types import SimpleNamespace

from netbox_manager import main


def _device(custom_fields=None, cluster=None):
    return SimpleNamespace(
        name="source-device",
        custom_fields={} if custom_fields is None else custom_fields,
        cluster=cluster,
    )


def _cluster(cluster_id=1, name="segment-a"):
    return SimpleNamespace(id=cluster_id, name=name)


def test_custom_field_takes_precedence_over_segment(mocker):
    segment = mocker.patch.object(
        main,
        "_get_cluster_segment_config_context",
        return_value={"_segment_device_interface_label": "segment-label"},
    )
    device = _device(
        custom_fields={"device_interface_label": "device-label"},
        cluster=_cluster(),
    )

    assert main._resolve_device_interface_label(None, device, {}) == "device-label"
    segment.assert_not_called()


def test_falls_back_to_segment_when_custom_field_unset(mocker):
    mocker.patch.object(
        main,
        "_get_cluster_segment_config_context",
        return_value={"_segment_device_interface_label": "segment-label"},
    )
    device = _device(custom_fields={}, cluster=_cluster())

    assert main._resolve_device_interface_label(None, device, {}) == "segment-label"


def test_empty_custom_field_falls_back_to_segment(mocker):
    mocker.patch.object(
        main,
        "_get_cluster_segment_config_context",
        return_value={"_segment_device_interface_label": "segment-label"},
    )
    device = _device(
        custom_fields={"device_interface_label": ""},
        cluster=_cluster(),
    )

    assert main._resolve_device_interface_label(None, device, {}) == "segment-label"


def test_returns_none_when_neither_source_set(mocker):
    mocker.patch.object(
        main,
        "_get_cluster_segment_config_context",
        return_value={},
    )
    device = _device(custom_fields={}, cluster=_cluster())

    assert main._resolve_device_interface_label(None, device, {}) is None


def test_custom_field_used_when_device_has_no_cluster(mocker):
    segment = mocker.patch.object(main, "_get_cluster_segment_config_context")
    device = _device(
        custom_fields={"device_interface_label": "device-label"},
        cluster=None,
    )

    assert main._resolve_device_interface_label(None, device, {}) == "device-label"
    segment.assert_not_called()


def test_no_segment_lookup_when_device_has_no_cluster(mocker):
    segment = mocker.patch.object(main, "_get_cluster_segment_config_context")
    device = _device(custom_fields={}, cluster=None)

    assert main._resolve_device_interface_label(None, device, {}) is None
    segment.assert_not_called()


def test_segment_context_is_fetched_once_per_cluster(mocker):
    segment = mocker.patch.object(
        main,
        "_get_cluster_segment_config_context",
        return_value={"_segment_device_interface_label": "segment-label"},
    )
    cluster = _cluster(cluster_id=7)
    cache: dict = {}
    first = _device(custom_fields={}, cluster=cluster)
    second = _device(custom_fields={}, cluster=cluster)

    assert main._resolve_device_interface_label(None, first, cache) == "segment-label"
    assert main._resolve_device_interface_label(None, second, cache) == "segment-label"
    segment.assert_called_once()


def test_absent_segment_label_is_cached(mocker):
    segment = mocker.patch.object(
        main,
        "_get_cluster_segment_config_context",
        return_value={},
    )
    cluster = _cluster(cluster_id=9)
    cache: dict = {}
    first = _device(custom_fields={}, cluster=cluster)
    second = _device(custom_fields={}, cluster=cluster)

    assert main._resolve_device_interface_label(None, first, cache) is None
    assert main._resolve_device_interface_label(None, second, cache) is None
    segment.assert_called_once()
