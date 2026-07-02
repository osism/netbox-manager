# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``handle_file`` orchestration (issue #252, Tier 4 of #232).

``handle_file`` (``netbox_manager.main``) is the glue that loads a YAML resource
file, validates its structure, dispatches each task to the right builder,
applies the task / device filters, renders a playbook and -- unless dry-run or
show-playbooks -- invokes ``ansible_runner.run``. These tests exercise the
branch logic *inside* ``handle_file``: they drive the **real** Tier 2 builders
(``create_netbox_task`` / ``create_uri_task`` / ``create_ansible_playbook``,
tested in #249) and the **real** ``load_global_vars`` (Tier 3), and mock only
``ansible_runner`` (via the shared ``mock_ansible_runner`` fixture) plus the
filesystem (via ``tmp_path``). The builders read ``settings.URL`` /
``settings.TOKEN`` / ``settings.IGNORE_SSL_ERRORS``; the baseline comes from the
conftest dynaconf env stub (#233).

Log assertions use the shared loguru->``caplog`` bridge from ``conftest.py``.
Where a PyYAML error message is asserted, only stable substrings are pinned --
the exact ``problem`` / ``context`` wording varies across PyYAML versions.
"""

import logging

import pytest
import typer
import yaml

from netbox_manager import main


@pytest.fixture(autouse=True)
def _no_global_vars(monkeypatch):
    """Keep ``load_global_vars`` hermetic across every test in this module.

    A developer environment exporting ``NETBOX_MANAGER_VARS`` would make the
    real ``load_global_vars`` read files from disk; forcing ``settings.VARS`` to
    ``None`` makes it return ``{}`` (the real function still runs), so the
    rendered ``vars`` block reflects only what each test's ``vars`` tasks merge.
    """
    monkeypatch.setattr(main.settings, "VARS", None, raising=False)


def write_resource_file(tmp_path, text, name="300-test.yml"):
    """Write ``text`` to a resource file under ``tmp_path`` and return its path."""
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def render_playbook(tmp_path, capsys, text, name="300-test.yml", **kwargs):
    """Render ``text`` through ``handle_file`` in show-playbooks mode.

    Writes ``text`` to a resource file, calls ``handle_file`` with
    ``show_playbooks=True`` (which prints the rendered playbook to stdout and
    returns before touching ``ansible_runner``), parses the captured stdout back
    with ``yaml.safe_load`` (the leading ``# Playbook for ...`` line is a YAML
    comment) and returns the single play dict.
    """
    file = write_resource_file(tmp_path, text, name=name)
    main.handle_file(file, dryrun=False, show_playbooks=True, **kwargs)
    out = capsys.readouterr().out
    assert out.startswith(f"# Playbook for {file}")
    plays = yaml.safe_load(out)
    assert isinstance(plays, list) and len(plays) == 1
    return plays[0]


class TestHandleFileLoadErrors:
    """Group 1 -- file load / IO error branches."""

    def test_missing_file_logs_and_returns(self, tmp_path, caplog, mock_ansible_runner):
        missing = str(tmp_path / "no-such-file.yml")
        result = main.handle_file(missing, dryrun=False, fail_fast=False)
        assert result is None
        assert "File not found" in caplog.text
        assert mock_ansible_runner.calls == []

    def test_missing_file_fail_fast_exits(self, tmp_path):
        missing = str(tmp_path / "no-such-file.yml")
        with pytest.raises(typer.Exit) as exc_info:
            main.handle_file(missing, dryrun=False, fail_fast=True)
        assert exc_info.value.exit_code == 1

    def test_malformed_yaml_logs_position_and_returns(
        self, tmp_path, caplog, mock_ansible_runner
    ):
        # An unterminated flow sequence raises a MarkedYAMLError carrying a
        # problem_mark, so the message is enriched with line/column.
        file = write_resource_file(tmp_path, "foo: [1, 2")
        result = main.handle_file(file, dryrun=False, fail_fast=False)
        assert result is None
        assert "Invalid YAML syntax in file" in caplog.text
        assert "at line" in caplog.text
        assert "column" in caplog.text
        assert mock_ansible_runner.calls == []

    def test_malformed_yaml_fail_fast_exits(self, tmp_path):
        file = write_resource_file(tmp_path, "foo: [1, 2")
        with pytest.raises(typer.Exit) as exc_info:
            main.handle_file(file, dryrun=False, fail_fast=True)
        assert exc_info.value.exit_code == 1

    def test_unreadable_path_logs_generic_error(
        self, tmp_path, caplog, mock_ansible_runner
    ):
        # Opening a directory raises IsADirectoryError (an OSError, not a
        # FileNotFoundError), so it falls through to the generic handler.
        result = main.handle_file(str(tmp_path), dryrun=False, fail_fast=False)
        assert result is None
        assert "Error reading file" in caplog.text
        assert mock_ansible_runner.calls == []

    def test_unreadable_path_fail_fast_exits(self, tmp_path):
        with pytest.raises(typer.Exit) as exc_info:
            main.handle_file(str(tmp_path), dryrun=False, fail_fast=True)
        assert exc_info.value.exit_code == 1

    def test_empty_file_warns_and_returns_even_with_fail_fast(
        self, tmp_path, caplog, mock_ansible_runner
    ):
        # The empty-file branch has no fail_fast exit -- it always returns.
        # Characterization, not endorsement: an empty resource file passing
        # silently under --fail may itself be a bug (see the PR #266 review
        # note); a future fix that makes this exit is not a regression.
        file = write_resource_file(tmp_path, "# only a comment\n")
        result = main.handle_file(file, dryrun=False, fail_fast=True)
        assert result is None
        assert "empty or contains only comments" in caplog.text
        assert mock_ansible_runner.calls == []

    @pytest.mark.parametrize("text,type_name", [("key: value", "dict"), ("42", "int")])
    def test_non_list_root_logs_actual_type(self, tmp_path, caplog, text, type_name):
        file = write_resource_file(tmp_path, text)
        result = main.handle_file(file, dryrun=False, fail_fast=False)
        assert result is None
        assert f"Expected a list of tasks, got {type_name}" in caplog.text

    def test_non_list_root_fail_fast_exits(self, tmp_path):
        file = write_resource_file(tmp_path, "key: value")
        with pytest.raises(typer.Exit) as exc_info:
            main.handle_file(file, dryrun=False, fail_fast=True)
        assert exc_info.value.exit_code == 1


class TestHandleFileTaskValidation:
    """Group 2 -- per-task validation branches."""

    def test_non_dict_task_skipped_and_rest_processed(self, tmp_path, capsys, caplog):
        text = "- [not, a, dict]\n- debug:\n    msg: kept\n"
        play = render_playbook(tmp_path, capsys, text)
        assert "Invalid task structure" in caplog.text
        assert "at index 0" in caplog.text
        assert "got list" in caplog.text
        # The `continue` means the following debug task is still processed.
        tasks = play["tasks"]
        assert len(tasks) == 1
        assert tasks[0] == {"ansible.builtin.debug": {"msg": "kept"}}

    def test_non_dict_task_fail_fast_exits(self, tmp_path):
        # The inner `raise typer.Exit(1)` is caught by handle_file's outer
        # `except Exception` (typer.Exit subclasses Exception) and re-raised,
        # emitting an extra "Error processing tasks" log line. The observable
        # behaviour is still exit code 1, which is what we pin -- we do not
        # assert the absence of that extra log line.
        file = write_resource_file(tmp_path, "- [not, a, dict]\n")
        with pytest.raises(typer.Exit) as exc_info:
            main.handle_file(file, dryrun=False, fail_fast=True)
        assert exc_info.value.exit_code == 1

    def test_empty_task_dict_warns_and_never_exits(self, tmp_path, capsys, caplog):
        # An empty task warns and skips regardless of fail_fast (no exit branch).
        # Characterization, not endorsement -- same asymmetry as the empty-file
        # branch above; see the PR #266 review note.
        text = "- {}\n- debug:\n    msg: kept\n"
        play = render_playbook(tmp_path, capsys, text, fail_fast=True)
        assert "Empty task in file" in caplog.text
        assert "index 0" in caplog.text
        tasks = play["tasks"]
        assert len(tasks) == 1
        assert tasks[0] == {"ansible.builtin.debug": {"msg": "kept"}}

    def test_register_only_task_warns_and_skips(
        self, tmp_path, caplog, mock_ansible_runner
    ):
        # After popping `register` the task is empty, so next(iter(...)) raises
        # StopIteration -> warn + skip, then the no-tasks gate returns.
        file = write_resource_file(tmp_path, "- register: out\n")
        result = main.handle_file(file, dryrun=False)
        assert result is None
        assert "no content after removing 'register' field" in caplog.text
        assert mock_ansible_runner.calls == []

    def test_register_popped_before_first_key_is_read(self, tmp_path, capsys):
        # `register` is listed first, yet dispatch keys on `vlan` -- proving the
        # pop (main.py:526) runs before the first remaining key is read
        # (main.py:530) -- and the popped value flows into the built task.
        text = "- register: vlan_result\n  vlan:\n    name: OOB\n    vid: 100\n"
        play = render_playbook(tmp_path, capsys, text)
        tasks = play["tasks"]
        assert len(tasks) == 1
        assert "netbox.netbox.netbox_vlan" in tasks[0]
        assert tasks[0]["register"] == "vlan_result"


class TestHandleFileDispatch:
    """Group 3 -- per-key dispatch branches."""

    def test_vars_blocks_merge_and_later_wins(self, tmp_path, capsys):
        text = (
            "- vars:\n    a: 1\n    nested:\n      x: 1\n"
            "- vars:\n    nested:\n      x: 2\n"
            "- debug:\n    msg: hi\n"
        )
        play = render_playbook(tmp_path, capsys, text)
        # deep_merge: later vars win at the leaf, siblings are preserved, and
        # the merged vars reach the rendered play. No task is emitted for `vars`.
        assert play["vars"] == {"a": 1, "nested": {"x": 2}}
        assert len(play["tasks"]) == 1

    def test_debug_task_with_register_and_ignore_errors(self, tmp_path, capsys):
        text = "- register: r\n  debug:\n    msg: hello\n"
        play = render_playbook(tmp_path, capsys, text, ignore_errors=True)
        tasks = play["tasks"]
        assert len(tasks) == 1
        assert tasks[0] == {
            "ansible.builtin.debug": {"msg": "hello"},
            "register": "r",
            "ignore_errors": True,
        }

    def test_debug_task_bare(self, tmp_path, capsys):
        # No register, default ignore_errors=False: neither key is added.
        text = "- debug:\n    msg: hello\n"
        play = render_playbook(tmp_path, capsys, text)
        tasks = play["tasks"]
        assert len(tasks) == 1
        assert set(tasks[0]) == {"ansible.builtin.debug"}

    def test_uri_task_wiring(self, tmp_path, capsys):
        text = "- register: resp\n  uri:\n    path: /api/status/\n    method: GET\n"
        play = render_playbook(tmp_path, capsys, text)
        tasks = play["tasks"]
        assert len(tasks) == 1
        uri = tasks[0]["ansible.builtin.uri"]
        # The value reached the real create_uri_task (URL built from the conftest
        # baseline); only the wiring is pinned, not the builder internals (#249).
        assert uri["url"] == "http://localhost:8000/api/status/"
        assert uri["method"] == "GET"
        assert tasks[0]["register"] == "resp"

    def test_default_key_builds_netbox_task(self, tmp_path, capsys):
        text = "- vlan:\n    name: OOB\n    vid: 100\n"
        play = render_playbook(tmp_path, capsys, text)
        tasks = play["tasks"]
        assert len(tasks) == 1
        module = tasks[0]["netbox.netbox.netbox_vlan"]
        assert module["data"] == {"name": "OOB", "vid": 100}

    def test_task_filter_keeps_only_matching_type(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        text = (
            "- vlan:\n    name: OOB\n    vid: 100\n"
            "- device_interface:\n    device: node-1\n    name: eth0\n"
        )
        # The filter normalises '-' to '_', so 'device-interface' matches.
        play = render_playbook(tmp_path, capsys, text, task_filter="device-interface")
        tasks = play["tasks"]
        assert len(tasks) == 1
        assert "netbox.netbox.netbox_device_interface" in tasks[0]
        assert "Skipping task of type 'vlan'" in caplog.text

    def test_device_filter_skips_and_keeps(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        text = (
            "- device_interface:\n    device: node-1\n    name: eth0\n"
            "- device_interface:\n    device: other-2\n    name: eth0\n"
            "- vlan:\n    name: OOB\n    vid: 100\n"
        )
        play = render_playbook(tmp_path, capsys, text, device_filters=["node"])
        tasks = play["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["netbox.netbox.netbox_device_interface"]["data"]["device"] == (
            "node-1"
        )
        # Both device-filter skip branches fire: one task references a
        # non-matching device, the other references no device at all.
        assert "Skipping task with devices" in caplog.text
        assert "no device reference" in caplog.text

    def test_ignore_errors_reaches_all_task_types(self, tmp_path, capsys):
        text = (
            "- debug:\n    msg: hi\n"
            "- uri:\n    path: /status/\n"
            "- vlan:\n    name: OOB\n"
        )
        play = render_playbook(tmp_path, capsys, text, ignore_errors=True)
        tasks = play["tasks"]
        assert len(tasks) == 3
        # Propagates into the debug dict and through both builder calls.
        assert all(task.get("ignore_errors") is True for task in tasks)


class TestHandleFileExecution:
    """Group 4 -- post-loop / execution branches."""

    def test_task_processing_error_logs_and_returns(
        self, tmp_path, caplog, mock_ansible_runner
    ):
        # A string `uri` value makes create_uri_task's `value.get(...)` raise
        # AttributeError inside the loop, hitting the outer except.
        file = write_resource_file(tmp_path, "- uri: not-a-dict\n")
        result = main.handle_file(file, dryrun=False, fail_fast=False)
        assert result is None
        assert "Error processing tasks in file" in caplog.text
        assert mock_ansible_runner.calls == []

    def test_task_processing_error_fail_fast_exits(self, tmp_path):
        file = write_resource_file(tmp_path, "- uri: not-a-dict\n")
        with pytest.raises(typer.Exit) as exc_info:
            main.handle_file(file, dryrun=False, fail_fast=True)
        assert exc_info.value.exit_code == 1

    def test_no_tasks_after_filter_skips_playbook(
        self, tmp_path, caplog, mock_ansible_runner
    ):
        caplog.set_level(logging.INFO)
        # The only task's type does not match the filter -> no tasks remain, so
        # handle_file returns before rendering or executing anything.
        file = write_resource_file(tmp_path, "- vlan:\n    name: OOB\n")
        result = main.handle_file(file, dryrun=False, task_filter="device")
        assert result is None
        assert "No tasks to execute" in caplog.text
        assert "after filtering" in caplog.text
        assert mock_ansible_runner.calls == []

    def test_show_playbooks_prints_and_never_runs(
        self, tmp_path, capsys, mock_ansible_runner
    ):
        file = write_resource_file(tmp_path, "- debug:\n    msg: hi\n")
        main.handle_file(file, dryrun=False, show_playbooks=True)
        out = capsys.readouterr().out
        assert out.startswith(f"# Playbook for {file}")
        plays = yaml.safe_load(out)
        assert len(plays) == 1
        assert plays[0]["name"] == ("Manage NetBox resources defined in 300-test.yml")
        assert mock_ansible_runner.calls == []

    def test_dryrun_skips_execution(self, tmp_path, caplog, mock_ansible_runner):
        caplog.set_level(logging.INFO)
        file = write_resource_file(tmp_path, "- debug:\n    msg: hi\n")
        main.handle_file(file, dryrun=True)
        assert "Skip the execution" in caplog.text
        assert "only one dry run" in caplog.text
        assert mock_ansible_runner.calls == []

    def test_run_invocation_kwargs(self, tmp_path, mock_ansible_runner):
        file = write_resource_file(tmp_path, "- vlan:\n    name: OOB\n    vid: 100\n")
        main.handle_file(file, dryrun=False, verbose=False)
        assert len(mock_ansible_runner.calls) == 1
        kw = mock_ansible_runner.calls[0]
        assert kw["verbosity"] is None
        assert kw["envvars"] == {
            "ANSIBLE_STDOUT_CALLBACK": "ansible.builtin.default",
            "ANSIBLE_CALLBACKS_ENABLED": "ansible.builtin.default",
            "ANSIBLE_STDOUT_CALLBACK_RESULT_FORMAT": "yaml",
        }
        assert callable(kw["cancel_callback"])
        assert isinstance(kw["private_data_dir"], str) and kw["private_data_dir"]
        # inventory is the module-level localhost dict (a kwarg the issue omits).
        assert kw["inventory"] is main.inventory
        assert kw["playbook"].endswith(".yml")
        # The recorder snapshots the playbook file at run() time, when it is
        # guaranteed to exist -- reading it back here instead would depend on
        # the delete=False temp-file leak that #267 removes.
        written = mock_ansible_runner.playbooks[0]
        assert "netbox.netbox.netbox_vlan" in written[0]["tasks"][0]

    def test_run_verbose_sets_verbosity_3(self, tmp_path, mock_ansible_runner):
        file = write_resource_file(tmp_path, "- vlan:\n    name: OOB\n")
        main.handle_file(file, dryrun=False, verbose=True)
        assert len(mock_ansible_runner.calls) == 1
        assert mock_ansible_runner.calls[0]["verbosity"] == 3

    def test_failed_status_with_fail_fast_exits(
        self, tmp_path, caplog, mock_ansible_runner
    ):
        mock_ansible_runner.status = "failed"
        file = write_resource_file(tmp_path, "- vlan:\n    name: OOB\n")
        with pytest.raises(typer.Exit) as exc_info:
            main.handle_file(file, dryrun=False, fail_fast=True)
        assert exc_info.value.exit_code == 1
        assert "Ansible playbook failed" in caplog.text

    def test_failed_status_without_fail_fast_returns(
        self, tmp_path, mock_ansible_runner
    ):
        mock_ansible_runner.status = "failed"
        file = write_resource_file(tmp_path, "- vlan:\n    name: OOB\n")
        result = main.handle_file(file, dryrun=False, fail_fast=False)
        assert result is None
        assert len(mock_ansible_runner.calls) == 1

    def test_successful_status_with_fail_fast_returns(
        self, tmp_path, mock_ansible_runner
    ):
        file = write_resource_file(tmp_path, "- vlan:\n    name: OOB\n")
        result = main.handle_file(file, dryrun=False, fail_fast=True)
        assert result is None
        assert len(mock_ansible_runner.calls) == 1
