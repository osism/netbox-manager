# SPDX-License-Identifier: Apache-2.0

import os
from types import SimpleNamespace

import pytest
import yaml
from loguru import logger

# Set dynaconf env vars before any test imports `netbox_manager.main`, since
# `Dynaconf(...)` and the validator registration run at import time. Force-set
# (not `setdefault`) so an exported `NETBOX_MANAGER_URL`/`TOKEN`/
# `IGNORE_SSL_ERRORS` from a real deployment cannot leak into tests that
# assert these baseline values, and tests do not depend on a real
# `settings.toml` being present.
os.environ["NETBOX_MANAGER_URL"] = "http://localhost:8000"
os.environ["NETBOX_MANAGER_TOKEN"] = "test-token"
os.environ["NETBOX_MANAGER_IGNORE_SSL_ERRORS"] = "false"

# Keep the role helpers hermetic. A real deployment env may export
# `NETBOX_MANAGER_NODE_ROLES` / `..._SWITCH_ROLES`, which dynaconf would load as
# `settings.NODE_ROLES` / `settings.SWITCH_ROLES` and make `get_node_roles()` /
# `get_switch_roles()` return the deployment list -- breaking the default-role
# and `is _DEFAULT_*` assertions. Drop them before `netbox_manager.main` is
# imported so the in-module defaults are the baseline; tests that need an
# override set it explicitly via `monkeypatch.setattr(main.settings, ...)`.
os.environ.pop("NETBOX_MANAGER_NODE_ROLES", None)
os.environ.pop("NETBOX_MANAGER_SWITCH_ROLES", None)

# Cross-tier fixtures. The lightweight attribute-bag factories `make_device` /
# `make_interface` (Tier 1, #247) come first; the Tier 5 (#253) generator seams
# -- the `make_cluster` / `make_config_context` bags and the fake `pynetbox.api`
# client (`make_netbox_api`, backed by `FakeNetBoxEndpoint`) that the loopback /
# cluster / interface-label / portchannel generators query -- follow, alongside
# the `make_device` / `make_interface` keyword extensions those generators read.
# Next come the heavier `mock_ansible_runner` recorder and the loguru->`caplog`
# bridge (Tier 4, #252). The Device Type Library seams (Tier 9, #257) -- the
# fake `pynetbox.api` client (`make_dtl_api`), the `tmp_path` library-tree
# builder (`make_dtl_tree`), the `LogHandler` call-through spy
# (`dtl_log_handler`) and the raisable `pynetbox.RequestError` factory
# (`make_request_error`) -- close the file. The remaining heavy seams
# (`git.Repo`, `subprocess`) belong to later #232 tiers.


@pytest.fixture
def make_device():
    """Build device-like attribute bags for the pure-logic helpers.

    Returns a factory producing a :class:`types.SimpleNamespace` that exposes
    only the attributes the helpers in :mod:`netbox_manager.main` read from a
    pynetbox device -- ``name``, ``role`` (with ``slug`` / ``name``),
    ``device_type`` (with ``model``) and ``custom_fields``. No live NetBox or
    full ``pynetbox`` mock is involved.

    Keyword arguments of the factory:
        name: Device name.
        role_slug / role_name: Attributes for ``device.role``; when both are
            omitted ``device.role`` is ``None`` (an unset role).
        device_type_model: ``model`` for ``device.device_type``; when omitted
            ``device.device_type`` is ``None``.
        custom_fields: Mapping for ``device.custom_fields`` (defaults to ``{}``).
        device_id: ``device.id`` -- the value the generators pass as
            ``dcim.interfaces.filter(device_id=...)`` (defaults to ``None``).
        cluster: ``device.cluster`` -- a ``make_cluster`` object (or ``None``,
            the default, for a device with no cluster). Always present as an
            attribute so ``_generate_cluster_loopback_tasks``' direct
            ``device.cluster`` read never raises ``AttributeError``.
        position: ``device.position`` -- the rack position
            ``calculate_loopback_ips`` reads (defaults to ``None``).

    The Tier 5 generator tests (#253) read ``id`` / ``cluster`` / ``position``;
    the Tier 1 helper tests (#247) predate them and pass none of the three, so
    every one is keyword-only with a benign default. Shared with later #232 test
    tiers; extend it here rather than re-deriving device shapes per test module.
    """

    def _make_device(
        *,
        name="test-device",
        role_slug=None,
        role_name=None,
        device_type_model=None,
        custom_fields=None,
        device_id=None,
        cluster=None,
        position=None,
    ):
        if role_slug is None and role_name is None:
            role = None
        else:
            role = SimpleNamespace()
            if role_slug is not None:
                role.slug = role_slug
            if role_name is not None:
                role.name = role_name

        device_type = (
            SimpleNamespace(model=device_type_model)
            if device_type_model is not None
            else None
        )

        return SimpleNamespace(
            name=name,
            role=role,
            device_type=device_type,
            custom_fields={} if custom_fields is None else custom_fields,
            id=device_id,
            cluster=cluster,
            position=position,
        )

    return _make_device


@pytest.fixture
def make_interface():
    """Build interface-like attribute bags for the pure-logic helpers.

    Returns a factory producing a :class:`types.SimpleNamespace` that exposes
    ``type`` (with ``value`` / ``label``, all
    :func:`netbox_manager.main.is_virtual_interface` reads) plus the attributes
    the Tier 5 generators (#253) read off a ``dcim.interfaces.filter(...)``
    record. When neither ``type_value`` nor ``type_label`` is given, ``type`` is
    ``None``.

    Keyword arguments of the factory:
        type_value / type_label: Attributes for ``interface.type``.
        name: ``interface.name`` -- the port name emitted into label / member
            tasks and read off a connected endpoint (defaults to ``None``).
        interface_id: ``interface.id`` -- the identity the PortChannel dedup
            matches connections on (defaults to ``None``).
        cable: ``interface.cable`` -- a truthy value marks the port cabled;
            ``None`` (the default) exercises the not-cabled skip branch.
        connected_endpoints: ``interface.connected_endpoints`` -- a list of
            endpoint records (each itself an interface bag exposing ``device`` /
            ``name`` / ``id``); ``None`` (the default) exercises the
            no-endpoints skip branch.
        device: ``interface.device`` -- the device the port lives on. Set on an
            interface used as a connected endpoint so the generator can read the
            far-side device back off it (mirrors a real NetBox record); ``None``
            (the default) exercises the endpoint-without-device skip branch.

    Shared with later #232 test tiers.
    """

    def _make_interface(
        *,
        type_value=None,
        type_label=None,
        name=None,
        interface_id=None,
        cable=None,
        connected_endpoints=None,
        device=None,
    ):
        if type_value is None and type_label is None:
            interface_type = None
        else:
            interface_type = SimpleNamespace()
            if type_value is not None:
                interface_type.value = type_value
            if type_label is not None:
                interface_type.label = type_label
        return SimpleNamespace(
            type=interface_type,
            name=name,
            id=interface_id,
            cable=cable,
            connected_endpoints=connected_endpoints,
            device=device,
        )

    return _make_interface


@pytest.fixture
def make_cluster():
    """Build cluster-like attribute bags for the loopback / label generators.

    ``make_cluster(cluster_id=1, name="segment-a")`` returns a
    :class:`types.SimpleNamespace` exposing the two attributes the generators
    read off ``device.cluster`` -- ``id`` (the key
    :func:`netbox_manager.main.group_devices_by_cluster` buckets by and the
    value passed to ``extras.config_contexts.filter(clusters=...)``) and
    ``name`` (matched against ``ctx.name`` to select the segment context).
    Shared with later mock-heavy #232 tiers (#254, #255).
    """

    def _make_cluster(*, cluster_id=1, name="segment-a"):
        return SimpleNamespace(id=cluster_id, name=name)

    return _make_cluster


@pytest.fixture
def make_config_context():
    """Build config-context-like attribute bags for the loopback generators.

    ``make_config_context(name="segment-a", data={...})`` returns a
    :class:`types.SimpleNamespace` exposing the two attributes
    :func:`netbox_manager.main._get_cluster_segment_config_context` reads off an
    ``extras.config_contexts.filter(...)`` record -- ``name`` (matched against
    the cluster name) and ``data`` (the config dict returned verbatim, or a
    falsy value to drive the no-data branch). Shared with later mock-heavy #232
    tiers (#254, #255).
    """

    def _make_config_context(*, name="segment-a", data=None):
        return SimpleNamespace(name=name, data=data)

    return _make_config_context


class FakeNetBoxEndpoint:
    """Recording stand-in for a pynetbox API endpoint used by the generators.

    Mirrors :class:`FakeDtlEndpoint` but for the read-only shape the Tier 5
    generators (#253) touch: ``all()`` and ``filter(**kwargs)``. Behaviour is
    configured at construction and every call is recorded for assertions:

        all_result: records ``all()`` returns (default empty).
        filter_lookup: callable mapping the ``filter(**kwargs)`` kwargs dict to
            the records that call returns (default: always ``[]``). Used for the
            ``dcim.interfaces.filter(device_id=...)`` and
            ``extras.config_contexts.filter(clusters=...)`` seams.
        filter_error: when set, ``filter(...)`` raises it -- drives the
            defensive ``except`` in ``_get_cluster_segment_config_context``.
        all_calls: number of ``all()`` calls.
        filter_calls: list of the keyword dicts passed to ``filter(...)`` (assert
            the generator queried the expected ``device_id`` / ``clusters``).
    """

    def __init__(self, *, all_result=None, filter_lookup=None, filter_error=None):
        self._all_result = [] if all_result is None else list(all_result)
        self._filter_lookup = filter_lookup
        self.filter_error = filter_error
        self.all_calls = 0
        self.filter_calls = []

    def all(self):
        self.all_calls += 1
        return list(self._all_result)

    def filter(self, **kwargs):
        self.filter_calls.append(kwargs)
        if self.filter_error is not None:
            raise self.filter_error
        if self._filter_lookup is None:
            return []
        return list(self._filter_lookup(kwargs))


@pytest.fixture
def make_netbox_api():
    """Return a factory building a fake ``pynetbox.api`` client for generators.

    First materialised here for #253; the loopback / cluster / interface-label /
    portchannel generators in :mod:`netbox_manager.main` each call
    ``create_netbox_api()`` internally, so wire the returned object in with
    ``monkeypatch.setattr(main, "create_netbox_api", lambda: fake_api)``.

    ``make_netbox_api(...)`` returns a :class:`types.SimpleNamespace` exposing
    exactly the three endpoints the generators read, each a
    :class:`FakeNetBoxEndpoint` recording its calls:

        dcim.devices.all(): returns ``devices``.
        dcim.interfaces.filter(device_id=...): returns
            ``interfaces_by_device.get(device_id, [])``.
        extras.config_contexts.filter(clusters=...): returns
            ``config_contexts_by_cluster.get(clusters, [])``, or raises
            ``config_contexts_error`` when that is set.

    Seed the endpoints with the ``make_device`` / ``make_interface`` /
    ``make_cluster`` / ``make_config_context`` bags (whose attributes match what
    each generator reads: ``device.cluster`` / ``device.position`` /
    ``device.custom_fields`` / ``device.device_type.model``, ``interface.cable``
    / ``interface.connected_endpoints`` with ``endpoint.device`` /
    ``endpoint.name``, and ``ctx.name`` / ``ctx.data``). Reach into the returned
    object (e.g. ``fake_api.dcim.interfaces.filter_calls``) to assert which
    devices were queried. Shared with the autoconf / validation and later
    mock-heavy #232 tiers (Tiers 6-9: #254, #255, #256, #258).
    """

    def _make(
        *,
        devices=None,
        interfaces_by_device=None,
        config_contexts_by_cluster=None,
        config_contexts_error=None,
    ):
        interfaces_by_device = interfaces_by_device or {}
        config_contexts_by_cluster = config_contexts_by_cluster or {}
        return SimpleNamespace(
            dcim=SimpleNamespace(
                devices=FakeNetBoxEndpoint(all_result=devices),
                interfaces=FakeNetBoxEndpoint(
                    filter_lookup=lambda kw: interfaces_by_device.get(
                        kw.get("device_id"), []
                    )
                ),
            ),
            extras=SimpleNamespace(
                config_contexts=FakeNetBoxEndpoint(
                    filter_lookup=lambda kw: config_contexts_by_cluster.get(
                        kw.get("clusters"), []
                    ),
                    filter_error=config_contexts_error,
                ),
            ),
        )

    return _make


@pytest.fixture
def caplog(caplog):
    """Bridge loguru log output into pytest's ``caplog`` capture.

    ``netbox_manager.main`` logs via loguru, whose messages pytest's stock
    ``caplog`` fixture does not see (loguru does not propagate through the
    standard ``logging`` root). This override -- the recipe from the loguru
    documentation -- registers ``caplog.handler`` as a loguru sink for the
    duration of the test, so ``caplog.text`` / ``caplog.records`` capture loguru
    messages as usual, and removes the sink again on teardown.

    Shared with the later #232 tiers (#256, #258, #259) that assert on the
    warn / error / info log branches; reuse it rather than re-deriving the
    bridge per test module.
    """
    handler_id = logger.add(
        caplog.handler,
        format="{message}",
        level=0,
        filter=lambda record: record["level"].no >= caplog.handler.level,
        enqueue=False,
    )
    yield caplog
    try:
        logger.remove(handler_id)
    except ValueError:
        # init_logger()'s bare logger.remove() drops *all* sinks, including
        # this one -- tests that reach it (the #256/#258/#259 main-path tiers)
        # would otherwise fail here in teardown.
        pass


@pytest.fixture
def mock_ansible_runner(monkeypatch):
    """Replace ``main.ansible_runner`` with a call-recording fake.

    ``handle_file`` invokes ``ansible_runner.run(...)`` to execute a rendered
    playbook. This fixture swaps the module reference on ``netbox_manager.main``
    for a recorder that never runs Ansible, so tests can assert both *whether*
    ``run`` was reached and *with which kwargs*.

    The returned object exposes:
        calls: list of the keyword-argument dicts passed to each ``run`` call
            (stays empty when ``run`` is never reached -- dry-run, show-playbooks,
            no-tasks-after-filter, or an early error return).
        playbooks: list (parallel to ``calls``) of the parsed content of each
            ``playbook`` file, snapshotted at call time. Assert on this rather
            than reading ``calls[n]["playbook"]`` back from disk afterwards --
            the temp playbook currently survives ``handle_file`` only due to a
            leak (no ``dir=`` on the ``NamedTemporaryFile``) that #267 fixes.
        status: the ``result.status`` string ``run`` reports back; set it to
            ``"failed"`` before calling ``handle_file`` to drive the fail-fast
            failure gate.

    ``main`` is imported inside the fixture body to preserve the
    env-vars-before-import discipline established at the top of this module.
    Shared with the later #232 tiers (#258) that mock playbook execution.
    """
    from netbox_manager import main

    class _RecordingAnsibleRunner:
        def __init__(self):
            self.calls = []
            self.playbooks = []
            self.status = "successful"

        def run(self, **kwargs):
            self.calls.append(kwargs)
            playbook = kwargs.get("playbook")
            if playbook is not None and os.path.isfile(playbook):
                with open(playbook) as fp:
                    self.playbooks.append(yaml.safe_load(fp))
            else:
                self.playbooks.append(None)
            return SimpleNamespace(status=self.status)

    fake = _RecordingAnsibleRunner()
    monkeypatch.setattr(main, "ansible_runner", fake)
    return fake


# --- Device Type Library (dtl.py) seams, Tier 9 (#257) --------------------
#
# ``netbox_manager.dtl`` talks to NetBox only through ``pynetbox.api`` and to
# the image endpoint through ``requests.patch``; everything else runs against
# the local filesystem. The fixtures below materialise those two seams plus the
# ``LogHandler`` spy so the DTL importer can be exercised without a live NetBox.


class FakeNetBoxRecord:
    """Attribute bag standing in for a single pynetbox record.

    ``dtl.py`` keys every lookup dict by ``str(item)`` -- manufacturers by
    ``name``, device/module types by ``model``, port templates by their port
    ``name`` -- so the fake record has to control its own ``__str__``.
    ``types.SimpleNamespace`` cannot (its ``__str__`` renders the whole repr),
    hence this small class: ``display`` drives ``str()`` while every keyword
    becomes a plain attribute the importer can read (``id``, ``model``,
    ``manufacturer``, ``device_type``, ``module_type`` ...).
    """

    def __init__(self, display, **attrs):
        self._display = display
        for key, value in attrs.items():
            setattr(self, key, value)

    def __str__(self):
        return str(self._display)

    def __repr__(self):
        return f"FakeNetBoxRecord({self._display!r})"


class FakeDtlEndpoint:
    """Recording stand-in for a pynetbox endpoint (``dcim.<collection>``).

    Exposes the three methods ``dtl.py`` calls -- ``all()`` / ``filter(...)`` /
    ``create(...)`` -- and records every invocation for assertions:

        all_calls: number of ``all()`` calls.
        filter_calls: list of the keyword dicts passed to ``filter(...)`` (the
            importer keys lookups on ``device_type_id`` vs ``module_type_id``;
            assert the right kwarg is used here).
        create_calls: list of the payloads passed to ``create(...)``.

    Behaviour is configured at construction:

        all_result / filter_result: the records ``all()`` / ``filter()`` return
            (default empty -- i.e. nothing exists yet).
        create_error: when set, ``create(...)`` raises it (drive the
            ``pynetbox.RequestError`` branches); otherwise ``create`` echoes the
            payload back as records, mirroring what the real API returns:
            a *list* payload (port templates, manufacturers) yields one
            :class:`FakeNetBoxRecord` per dict -- ``name`` from ``d["name"]``, a
            sequential ``id``, and ``device_type`` / ``module_type`` wrapped as
            ``SimpleNamespace(id=...)`` when the creator stamped those keys, which
            is exactly what ``log_*_ports_created`` reads; a *dict* payload
            (a single device/module type) yields one record exposing
            ``model`` / ``id`` / ``manufacturer``.
    """

    def __init__(self, *, all_result=None, filter_result=None, create_error=None):
        self.all_result = [] if all_result is None else list(all_result)
        self.filter_result = [] if filter_result is None else list(filter_result)
        self.create_error = create_error
        self.all_calls = 0
        self.filter_calls = []
        self.create_calls = []
        self._next_id = 1

    def all(self):
        self.all_calls += 1
        return list(self.all_result)

    def filter(self, **kwargs):
        self.filter_calls.append(kwargs)
        return list(self.filter_result)

    def create(self, payload):
        self.create_calls.append(payload)
        if self.create_error is not None:
            raise self.create_error
        if isinstance(payload, list):
            return [self._echo_list_item(item) for item in payload]
        return self._echo_object(payload)

    def _echo_list_item(self, item):
        attrs = {"name": item["name"], "id": self._next_id}
        self._next_id += 1
        if "type" in item:
            attrs["type"] = item["type"]
        if "device_type" in item:
            attrs["device_type"] = SimpleNamespace(id=item["device_type"])
        if "module_type" in item:
            attrs["module_type"] = SimpleNamespace(id=item["module_type"])
        return FakeNetBoxRecord(item["name"], **attrs)

    def _echo_object(self, item):
        record = FakeNetBoxRecord(
            item.get("model", ""),
            model=item.get("model"),
            id=self._next_id,
            manufacturer=SimpleNamespace(**item["manufacturer"]),
        )
        self._next_id += 1
        return record


@pytest.fixture
def make_dtl_record():
    """Return a factory building :class:`FakeNetBoxRecord` instances.

    ``make_dtl_record(display, **attrs)`` -- ``display`` is what ``str(record)``
    returns (the key ``dtl.py`` looks records up by); keywords become
    attributes. Use it to seed ``all_result`` / ``filter_result`` on a
    :class:`FakeDtlEndpoint` (e.g. ``make_dtl_record("Arista", id=1)`` for an
    existing manufacturer, ``make_dtl_record("PP1", id=9)`` for an existing
    power port resolved by name). Shared with later #232 dtl tiers.
    """
    return FakeNetBoxRecord


@pytest.fixture
def make_dtl_api():
    """Return a factory building a fake ``pynetbox.api`` client for dtl tests.

    ``make_dtl_api(version="4.2")`` returns a :class:`types.SimpleNamespace`
    exposing exactly what ``dtl.NetBox`` / ``dtl.DeviceTypes`` touch:

        version: the string ``verify_compatibility`` parses.
        http_session: a namespace whose ``verify`` starts at the sentinel
            ``"untouched"`` so the ``IGNORE_SSL_ERRORS`` branch of
            ``connect_api`` is observable.
        dcim: a namespace of :class:`FakeDtlEndpoint` collections --
            ``manufacturers`` / ``device_types`` / ``module_types`` and the nine
            ``*_templates`` used by the component creators.

    Inject it into ``NetBox.connect_api`` with
    ``monkeypatch.setattr(dtl.pynetbox, "api", lambda *a, **k: api)``; configure
    the individual endpoints (``all_result`` / ``filter_result`` /
    ``create_error``) before constructing ``NetBox`` -- its ``__init__`` eagerly
    reads ``version`` and calls ``manufacturers.all()`` /
    ``device_types.all()``. Shared with later #232 dtl tiers.
    """

    def _make(version="4.2"):
        template_names = [
            "interface_templates",
            "power_port_templates",
            "console_port_templates",
            "power_outlet_templates",
            "console_server_port_templates",
            "rear_port_templates",
            "front_port_templates",
            "device_bay_templates",
            "module_bay_templates",
        ]
        dcim = SimpleNamespace(
            manufacturers=FakeDtlEndpoint(),
            device_types=FakeDtlEndpoint(),
            module_types=FakeDtlEndpoint(),
            **{name: FakeDtlEndpoint() for name in template_names},
        )
        return SimpleNamespace(
            version=version,
            http_session=SimpleNamespace(verify="untouched"),
            dcim=dcim,
        )

    return _make


@pytest.fixture
def make_dtl_tree(tmp_path):
    """Return a factory building a device-type-library tree under ``tmp_path``.

    ``make_dtl_tree(vendors=..., loose_files=..., images=...)`` lays out::

        tmp_path/devicetypes/<Vendor>/<file>.{yaml,yml}
        tmp_path/devicetypes/<loose file>        # e.g. .gitkeep
        tmp_path/images/<Vendor>/<slug>.front.*  # matches dtl's image glob

    and returns the ``devicetypes`` directory as a ``str`` (what ``Repo`` walks).

    Arguments:
        vendors: mapping ``vendor folder -> {filename: content}``. A ``dict``
            content is dumped with ``yaml.safe_dump``; a ``str`` content is
            written verbatim (use it for the unparseable-YAML case).
        loose_files: filenames written directly into ``devicetypes/`` (plain
            files such as ``.gitkeep`` that ``get_devices`` must ignore).
        images: mapping ``vendor folder -> [image filename]`` written under a
            sibling ``images/<Vendor>/`` tree. ``create_device_types`` resolves
            images by replacing ``devicetypes`` with ``images`` in the source
            path and globbing ``<slug>.front.*`` / ``<slug>.rear.*``.

    Shared with later #232 dtl tiers.
    """

    def _make(*, vendors=None, loose_files=None, images=None):
        library = tmp_path / "devicetypes"
        library.mkdir(exist_ok=True)
        for vendor, files in (vendors or {}).items():
            vendor_dir = library / vendor
            vendor_dir.mkdir(parents=True, exist_ok=True)
            for filename, content in files.items():
                path = vendor_dir / filename
                if isinstance(content, str):
                    path.write_text(content)
                else:
                    path.write_text(yaml.safe_dump(content))
        for name in loose_files or []:
            (library / name).write_text("")
        for vendor, image_names in (images or {}).items():
            image_dir = tmp_path / "images" / vendor
            image_dir.mkdir(parents=True, exist_ok=True)
            for image_name in image_names:
                (image_dir / image_name).write_bytes(b"\x89PNG\r\n")
        return str(library)

    return _make


@pytest.fixture
def dtl_log_handler():
    """Return a call-through spy over ``dtl.LogHandler``.

    ``log_device_ports_created`` / ``log_module_ports_created`` *return*
    ``len(created_ports)``, and that return value feeds the
    ``counter["updated"]`` bump -- so the spy has to pass calls through to a real
    ``LogHandler`` (``Mock(wraps=...)``) rather than stub the returns. Tests
    assert on ``spy.log_device_ports_created.call_args`` (the created-port list
    and the ``port_type`` label) and on ``spy.log.call_args`` (the error / image
    messages) while the real handler still produces the counts. The wrapped
    handler runs with ``verbose=False`` so ``verbose_log`` stays quiet.
    Shared with later #232 dtl tiers.
    """
    from unittest.mock import Mock

    from netbox_manager import dtl

    return Mock(wraps=dtl.LogHandler(SimpleNamespace(verbose=False)))


@pytest.fixture
def make_request_error():
    """Return a factory building a raisable ``pynetbox.RequestError``.

    ``make_request_error(message="boom")`` constructs the exception via
    ``RequestError.__new__`` and sets ``.error`` directly, deliberately
    bypassing the real ``__init__`` (which requires a live ``requests.Response``
    object). ``.error`` is the only attribute ``dtl.py`` reads off a caught
    ``RequestError``, and the instance is still caught by
    ``except pynetbox.RequestError``. Set it as a ``FakeDtlEndpoint``'s
    ``create_error`` to drive the error branches. Shared with later #232 tiers
    that assert error propagation.
    """
    import pynetbox

    def _make(message="boom"):
        error = pynetbox.RequestError.__new__(pynetbox.RequestError)
        Exception.__init__(error, message)
        error.error = message
        return error

    return _make
