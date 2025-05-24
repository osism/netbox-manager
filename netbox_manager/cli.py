# SPDX-License-Identifier: Apache-2.0

import pkg_resources

import typer


def signal_handler_sigint(sig, frame):
    print("SIGINT received. Exit.")
    raise typer.Exit()


def callback_version(value: bool):
    if value:
        print(f"Version {pkg_resources.get_distribution('netbox-manager').version}")
        raise typer.Exit()
