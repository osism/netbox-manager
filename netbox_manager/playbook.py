# SPDX-License-Identifier: Apache-2.0

import tempfile

import ansible_runner
from loguru import logger
import typer

from .config import settings, inventory

playbook_template = """
- name: Manage NetBox resources defined in {{ name }}
  connection: local
  hosts: localhost
  gather_facts: false

  vars:
    {{ vars | indent(4) }}

  tasks:
    {{ tasks | indent(4) }}
"""

playbook_wait = f"""
- name: Wait for NetBox service
  hosts: localhost
  gather_facts: false

  tasks:
    - name: Wait for NetBox service REST API
      ansible.builtin.uri:
        url: "{settings.URL.rstrip('/')}/api/"
        headers:
          Authorization: "Token {settings.TOKEN}"
          Accept: application/json
        status_code: [200]
        validate_certs: {not settings.IGNORE_SSL_ERRORS}
      register: result
      retries: 60
      delay: 5
      until: result.status == 200 or result.status == 403
"""


def wait_for_netbox() -> None:
    """Wait for NetBox service to be ready."""
    logger.info("Wait for NetBox service")

    with tempfile.TemporaryDirectory() as temp_dir:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".yml", delete=False
        ) as temp_file:
            temp_file.write(playbook_wait)

        ansible_result = ansible_runner.run(
            playbook=temp_file.name,
            private_data_dir=temp_dir,
            inventory=inventory,
            cancel_callback=lambda: None,
        )
        if (
            "localhost" in ansible_result.stats["failures"]
            and ansible_result.stats["failures"]["localhost"] > 0
        ):
            logger.error("Failed to establish connection to netbox")
            raise typer.Exit()


def run_playbook(playbook_content: str, dryrun: bool = False) -> None:
    """Run an Ansible playbook."""
    with tempfile.TemporaryDirectory() as temp_dir:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".yml", delete=False
        ) as temp_file:
            temp_file.write(playbook_content)

        if not dryrun:
            ansible_runner.run(
                playbook=temp_file.name,
                private_data_dir=temp_dir,
                inventory=inventory,
                cancel_callback=lambda: None,
            )
