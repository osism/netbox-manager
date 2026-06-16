# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the task builders in ``netbox_manager.main`` (issue #249).

Covers groups 1 and 2 of the Tier 2 scope tracked in #232: the settings-aware
builders that turn a YAML resource value into an Ansible task dict --
``create_netbox_task`` (the ``netbox.netbox.netbox_*`` module envelope) and
``create_uri_task`` (the ``ansible.builtin.uri`` direct API call). Both read
``settings.URL`` / ``settings.TOKEN`` / ``settings.IGNORE_SSL_ERRORS`` at call
time; the baseline comes from the conftest dynaconf env stub and branch-specific
values are injected with ``monkeypatch.setattr(main.settings, ..., raising=False)``
(``raising=False`` because dynaconf resolves keys through ``__getattr__``).

These builders mutate their ``value`` argument (``pop("state")`` and, for
``device_interface``, ``pop("update_vc_child")``), so every test passes a fresh
dict literal.
"""

from netbox_manager import main


class TestCreateNetboxTask:
    """``create_netbox_task`` wraps a value in the netbox module envelope."""

    def test_default_state_is_present(self):
        task = main.create_netbox_task("vlan", {"name": "OOB"})
        assert task["netbox.netbox.netbox_vlan"]["state"] == "present"

    def test_explicit_state_popped_and_removed_from_data(self):
        value = {"name": "OOB", "state": "absent"}
        task = main.create_netbox_task("vlan", value)
        module = task["netbox.netbox.netbox_vlan"]
        assert module["state"] == "absent"
        # The state is moved into the envelope, not left inside data...
        assert "state" not in module["data"]
        # ...and the caller's dict is mutated in place (pin this behaviour).
        assert "state" not in value

    def test_module_envelope_fields(self):
        value = {"name": "OOB", "vid": 100}
        task = main.create_netbox_task("vlan", value)
        # Only the task name and the single module key are present.
        assert set(task) == {"name", "netbox.netbox.netbox_vlan"}
        module = task["netbox.netbox.netbox_vlan"]
        assert module["data"] == {"name": "OOB", "vid": 100}
        assert module["state"] == "present"
        assert module["netbox_token"] == "test-token"
        assert module["netbox_url"] == "http://localhost:8000"
        assert module["validate_certs"] is True

    def test_validate_certs_true_when_ssl_errors_not_ignored(self):
        # Baseline conftest stub: IGNORE_SSL_ERRORS is false.
        task = main.create_netbox_task("vlan", {"name": "OOB"})
        assert task["netbox.netbox.netbox_vlan"]["validate_certs"] is True

    def test_validate_certs_false_when_ssl_errors_ignored(self, monkeypatch):
        monkeypatch.setattr(main.settings, "IGNORE_SSL_ERRORS", True, raising=False)
        task = main.create_netbox_task("vlan", {"name": "OOB"})
        assert task["netbox.netbox.netbox_vlan"]["validate_certs"] is False

    def test_envelope_reads_settings_at_call_time(self, monkeypatch):
        monkeypatch.setattr(
            main.settings, "URL", "https://netbox.example.org", raising=False
        )
        monkeypatch.setattr(main.settings, "TOKEN", "other-token", raising=False)
        task = main.create_netbox_task("vlan", {"name": "OOB"})
        module = task["netbox.netbox.netbox_vlan"]
        assert module["netbox_url"] == "https://netbox.example.org"
        assert module["netbox_token"] == "other-token"

    def test_token_is_stringified(self, monkeypatch):
        # A numeric token from settings is coerced to str(settings.TOKEN).
        monkeypatch.setattr(main.settings, "TOKEN", 12345, raising=False)
        task = main.create_netbox_task("vlan", {"name": "OOB"})
        assert task["netbox.netbox.netbox_vlan"]["netbox_token"] == "12345"

    def test_task_name_format(self):
        task = main.create_netbox_task("vlan", {"name": "OOB"})
        assert task["name"] == "Manage NetBox resource OOB of type vlan"

    def test_task_name_collapses_double_space_when_no_name(self):
        # No `name` key -> the empty interpolation leaves a double space that
        # `.replace("  ", " ")` collapses to a single space.
        task = main.create_netbox_task("vlan", {"vid": 100})
        assert task["name"] == "Manage NetBox resource of type vlan"

    def test_update_vc_child_beside_data_for_device_interface(self):
        value = {"device": "node-0", "name": "Eth0", "update_vc_child": True}
        task = main.create_netbox_task("device_interface", value)
        module = task["netbox.netbox.netbox_device_interface"]
        # Lifted out of data and placed beside it at the module level.
        assert module["update_vc_child"] is True
        assert "update_vc_child" not in module["data"]

    def test_update_vc_child_stays_in_data_for_other_keys(self):
        value = {"name": "node-0", "update_vc_child": True}
        task = main.create_netbox_task("device", value)
        module = task["netbox.netbox.netbox_device"]
        # For any non-device_interface key it is left untouched inside data.
        assert module["data"]["update_vc_child"] is True
        assert "update_vc_child" not in module

    def test_register_added_only_when_truthy(self):
        with_register = main.create_netbox_task(
            "vlan", {"name": "OOB"}, register_var="result"
        )
        assert with_register["register"] == "result"

        no_register = main.create_netbox_task("vlan", {"name": "OOB"})
        assert "register" not in no_register

        empty_register = main.create_netbox_task(
            "vlan", {"name": "OOB"}, register_var=""
        )
        assert "register" not in empty_register

    def test_ignore_errors_added_only_when_true(self):
        with_ignore = main.create_netbox_task(
            "vlan", {"name": "OOB"}, ignore_errors=True
        )
        assert with_ignore["ignore_errors"] is True

        no_ignore = main.create_netbox_task("vlan", {"name": "OOB"})
        assert "ignore_errors" not in no_ignore


class TestCreateUriTask:
    """``create_uri_task`` builds an ``ansible.builtin.uri`` API-call task."""

    def test_strips_api_prefix_with_leading_slash(self):
        task = main.create_uri_task({"path": "/api/dcim/devices/"})
        uri = task["ansible.builtin.uri"]
        assert uri["url"] == "http://localhost:8000/api/dcim/devices/"
        # The normalized path (prefix stripped) is what the name reports.
        assert task["name"] == "NetBox API call: GET dcim/devices/"

    def test_strips_api_prefix_without_leading_slash(self):
        task = main.create_uri_task({"path": "api/dcim/"})
        assert task["ansible.builtin.uri"]["url"] == "http://localhost:8000/api/dcim/"

    def test_strips_remaining_leading_slashes(self):
        task = main.create_uri_task({"path": "/dcim/devices"})
        assert (
            task["ansible.builtin.uri"]["url"]
            == "http://localhost:8000/api/dcim/devices"
        )

    def test_trailing_slash_url_does_not_produce_double_slash(self, monkeypatch):
        monkeypatch.setattr(
            main.settings, "URL", "http://localhost:8000/", raising=False
        )
        task = main.create_uri_task({"path": "dcim"})
        assert task["ansible.builtin.uri"]["url"] == "http://localhost:8000/api/dcim"

    def test_empty_path_yields_api_root(self):
        task = main.create_uri_task({})
        assert task["ansible.builtin.uri"]["url"] == "http://localhost:8000/api/"

    def test_method_defaults_to_get(self):
        task = main.create_uri_task({"path": "dcim/devices"})
        assert task["ansible.builtin.uri"]["method"] == "GET"
        # Empty path is not in play here, but the name still leads with GET.
        assert task["name"].startswith("NetBox API call: GET")

    def test_explicit_method_passed_through(self):
        task = main.create_uri_task({"path": "dcim/devices/1/", "method": "PATCH"})
        assert task["ansible.builtin.uri"]["method"] == "PATCH"
        assert "PATCH" in task["name"]

    def test_headers(self):
        headers = main.create_uri_task({"path": "dcim"})["ansible.builtin.uri"][
            "headers"
        ]
        assert headers["Authorization"] == "Token test-token"
        assert headers["Accept"] == "application/json"
        assert headers["Content-Type"] == "application/json"

    def test_validate_certs_true_when_ssl_errors_not_ignored(self):
        task = main.create_uri_task({"path": "dcim"})
        assert task["ansible.builtin.uri"]["validate_certs"] is True

    def test_validate_certs_false_when_ssl_errors_ignored(self, monkeypatch):
        monkeypatch.setattr(main.settings, "IGNORE_SSL_ERRORS", True, raising=False)
        task = main.create_uri_task({"path": "dcim"})
        assert task["ansible.builtin.uri"]["validate_certs"] is False

    def test_empty_body_removes_body_and_body_format(self):
        # Missing body -> both keys are deleted (GET branch).
        uri = main.create_uri_task({"path": "dcim"})["ansible.builtin.uri"]
        assert "body" not in uri
        assert "body_format" not in uri

    def test_explicitly_empty_body_removes_body_and_body_format(self):
        uri = main.create_uri_task({"path": "dcim", "body": {}})["ansible.builtin.uri"]
        assert "body" not in uri
        assert "body_format" not in uri

    def test_nonempty_body_kept_with_json_format(self):
        uri = main.create_uri_task(
            {"path": "dcim", "method": "POST", "body": {"name": "node-0"}}
        )["ansible.builtin.uri"]
        assert uri["body"] == {"name": "node-0"}
        assert uri["body_format"] == "json"

    def test_status_code_is_fixed(self):
        uri = main.create_uri_task({"path": "dcim"})["ansible.builtin.uri"]
        assert uri["status_code"] == [200, 201, 204]

    def test_register_added_only_when_truthy(self):
        with_register = main.create_uri_task({"path": "dcim"}, register_var="result")
        assert with_register["register"] == "result"

        no_register = main.create_uri_task({"path": "dcim"})
        assert "register" not in no_register

        empty_register = main.create_uri_task({"path": "dcim"}, register_var="")
        assert "register" not in empty_register

    def test_ignore_errors_added_only_when_true(self):
        with_ignore = main.create_uri_task({"path": "dcim"}, ignore_errors=True)
        assert with_ignore["ignore_errors"] is True

        no_ignore = main.create_uri_task({"path": "dcim"})
        assert "ignore_errors" not in no_ignore
