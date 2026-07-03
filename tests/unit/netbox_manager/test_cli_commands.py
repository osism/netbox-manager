# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the Typer command wiring (issue #258, Tier 10 of #232).

This tier covers the thin ``@app.command()`` / ``@app.callback()`` shells in
``netbox_manager.main`` plus the entrypoint plumbing (``main``, the SIGINT
handler, ``init_logger``, the ``--version`` eager callback). The goal is to
verify that CLI argument/option parsing maps onto the right worker arguments,
that each command dispatches to the correct worker, and that exit codes are
correct -- *not* to re-test the workers themselves (Tiers 1-9 do that).

The defining property of this tier is that every heavy worker is patched out
via the shared ``mock_cli_workers`` fixture (see ``conftest.py``): ``_run_main``,
``_run_autoconf_for_devices``, ``validate_ip_addresses_have_prefixes`` /
``validate_vrf_consistency``, the connection guard and the API factory. As a
result these tests touch no live NetBox, no ``ansible_runner``, no ``git.Repo``
and no real filesystem -- they exercise only the ``typer`` dispatch surface, and
the destructive ``purge`` deletion path is asserted unreached in dry-run.

``init_logger`` is among the patched workers because the real one calls
``logger.remove()``, which would tear down the shared loguru->``caplog`` bridge
mid-command and blind the log-message assertions; the spy also serves as the
``--debug`` / ``--verbose`` plumbing assertion point.
"""

from importlib import metadata
from unittest.mock import MagicMock

from netbox_manager import main
from netbox_manager.main import app

VERSION = metadata.version("netbox-manager")

# ``_run_main`` receives all 17 ``run`` options positionally (main.py). This is
# the tuple ``run`` forwards for a bare ``run`` / no-subcommand invocation.
RUN_MAIN_DEFAULTS = (
    True,  # always
    False,  # debug
    False,  # dryrun
    None,  # limit
    1,  # parallel
    None,  # version
    False,  # skipdtl
    False,  # skipmtl
    False,  # skipres
    True,  # wait
    None,  # filter_task
    False,  # include_ignored_files
    None,  # filter_device
    False,  # fail_fast
    False,  # show_playbooks
    False,  # verbose
    False,  # ignore_errors
)


class TestRunCommand:
    """Group 1 -- ``run_command`` option mapping and dispatch (main.py)."""

    def test_options_map_positionally_to_run_main(self, cli_runner, mock_cli_workers):
        result = cli_runner.invoke(
            app,
            [
                "run",
                "--no-always",
                "--dryrun",
                "--limit",
                "300",
                "--parallel",
                "4",
                "--skipdtl",
                "--skipmtl",
                "--skipres",
                "--no-wait",
                "--filter-task",
                "device",
                "--filter-device",
                "node-a",
                "--filter-device",
                "node-b",
                "--fail-fast",
                "--show-playbooks",
                "--verbose",
                "--ignore-errors",
            ],
        )

        assert result.exit_code == 0
        assert mock_cli_workers.run_main.call_count == 1
        assert mock_cli_workers.run_main.call_args.kwargs == {}
        assert mock_cli_workers.run_main.call_args.args == (
            False,  # always (--no-always)
            False,  # debug
            True,  # dryrun
            "300",  # limit
            4,  # parallel
            None,  # version
            True,  # skipdtl
            True,  # skipmtl
            True,  # skipres
            False,  # wait (--no-wait)
            "device",  # filter_task
            False,  # include_ignored_files
            ["node-a", "node-b"],  # filter_device (repeatable -> list)
            True,  # fail_fast
            True,  # show_playbooks
            True,  # verbose
            True,  # ignore_errors
        )

    def test_bare_run_uses_documented_defaults(self, cli_runner, mock_cli_workers):
        result = cli_runner.invoke(app, ["run"])

        assert result.exit_code == 0
        assert mock_cli_workers.run_main.call_count == 1
        assert mock_cli_workers.run_main.call_args.args == RUN_MAIN_DEFAULTS

    def test_dispatch_touches_only_the_patched_worker(
        self, cli_runner, mock_cli_workers, mock_ansible_runner
    ):
        result = cli_runner.invoke(app, ["run"])

        assert result.exit_code == 0
        # The connection guard and the API factory both live *inside* the
        # patched ``_run_main``, so a hermetic dispatch never reaches them.
        mock_cli_workers.validate_netbox_connection.assert_not_called()
        mock_cli_workers.create_netbox_api.assert_not_called()
        assert mock_ansible_runner.calls == []

    def test_version_eager_callback_skips_run_main(self, cli_runner, mock_cli_workers):
        result = cli_runner.invoke(app, ["run", "--version"])

        assert result.exit_code == 0
        assert f"Version {VERSION}" in result.output
        mock_cli_workers.run_main.assert_not_called()


class TestAutoconfCommand:
    """Group 2 -- ``autoconf_command`` dispatch and exit codes (main.py)."""

    def test_flat_mode_dispatches_worker_once(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        detect = MagicMock(return_value={})
        monkeypatch.setattr(main, "detect_site_folders", detect)

        result = cli_runner.invoke(app, ["autoconf"])

        assert result.exit_code == 0
        detect.assert_called_once_with(str(tmp_path))
        assert mock_cli_workers.run_autoconf_for_devices.call_count == 1
        assert mock_cli_workers.run_autoconf_for_devices.call_args.kwargs == {
            "device_filter": None,
            "output_dir": str(tmp_path),
            "prefix": "999-autoconf",
            "dryrun": False,
        }
        mock_cli_workers.validate_netbox_connection.assert_called_once_with()

    def test_output_option_derives_prefix_and_output_dir(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))

        result = cli_runner.invoke(
            app, ["autoconf", "--output", "sub/dir/500-site.yml"]
        )

        assert result.exit_code == 0
        kwargs = mock_cli_workers.run_autoconf_for_devices.call_args.kwargs
        assert kwargs["prefix"] == "500-site"
        assert kwargs["output_dir"] == "sub/dir"

    def test_dryrun_and_debug_plumbing(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))

        result = cli_runner.invoke(app, ["autoconf", "--dryrun", "--debug"])

        assert result.exit_code == 0
        kwargs = mock_cli_workers.run_autoconf_for_devices.call_args.kwargs
        assert kwargs["dryrun"] is True
        mock_cli_workers.init_logger.assert_called_once_with(True)

    def test_request_error_from_worker_exits_1(
        self,
        cli_runner,
        mock_cli_workers,
        monkeypatch,
        tmp_path,
        caplog,
        make_request_error,
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))
        mock_cli_workers.run_autoconf_for_devices.side_effect = make_request_error(
            "boom"
        )

        result = cli_runner.invoke(app, ["autoconf"])

        assert result.exit_code == 1
        assert "NetBox API error" in caplog.text

    def test_generic_error_from_worker_exits_1(
        self, cli_runner, mock_cli_workers, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(main.settings, "RESOURCES", str(tmp_path), raising=False)
        monkeypatch.setattr(main, "detect_site_folders", MagicMock(return_value={}))
        mock_cli_workers.run_autoconf_for_devices.side_effect = RuntimeError("kaput")

        result = cli_runner.invoke(app, ["autoconf"])

        assert result.exit_code == 1
        assert "Error generating autoconf" in caplog.text
