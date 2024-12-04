# SPDX-License-Identifier: Apache-2.0

import glob
from natsort import natsorted
import os
import sys
import tempfile
import time
from typing import Optional
from typing_extensions import Annotated
import warnings

import ansible_runner
from dynaconf import Dynaconf
from jinja2 import Template
from loguru import logger
import pynetbox
import typer
import yaml

from .dtl import DTLRepo, DTLNetBox

warnings.filterwarnings("ignore")

log_fmt = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<level>{message}</level>"
)

logger.remove()
logger.add(sys.stderr, format=log_fmt, level="INFO", colorize=True)

settings = Dynaconf(
    envvar_prefix="NETBOX_MANAGER",
    settings_files=["settings.toml"],
    load_dotenv=True,
)

assert type(settings.DEVICETYPE_LIBRARY) is str
assert type(settings.TOKEN) is str
assert type(settings.URL) is str

nb = pynetbox.api(settings.URL, token=settings.TOKEN)

inventory = {
    "all": {
        "hosts": {
            "localhost": {
                "ansible_connection": "local",
                "netbox_url": settings.URL,
                "netbox_token": settings.TOKEN,
                "ansible_python_interpreter": sys.executable,
            }
        }
    }
}

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

playbook_wait = """
- name: Wait for NetBox service
  hosts: localhost
  gather_facts: false

  tasks:
    - name: Wait for NetBox service
      ansible.builtin.uri:
        url: "{{ netbox_url }}"
        return_content: true
        status_code: [200]
        validate_certs: false
      register: result
      failed_when: "'NetBox Community' not in result.content"
      retries: 60
      delay: 5
"""


def run(
    limit: Annotated[Optional[str], typer.Option(help="Limit files by prefix")] = None,
    wait: Annotated[bool, typer.Option(help="Wait for NetBox service")] = True,
    skipdtl: Annotated[bool, typer.Option(help="Skip device type library")] = False,
) -> None:
    start = time.time()

    # install netbox.netbox collection
    # ansible-galaxy collection install netbox.netbox

    # wait for NetBox service
    if wait:
        logger.info("Wait for NetBox service")

        with tempfile.TemporaryDirectory() as temp_dir:
            with tempfile.NamedTemporaryFile(
                mode="w+", suffix=".yml", delete=False
            ) as temp_file:
                temp_file.write(playbook_wait)

            ansible_runner.run(
                playbook=temp_file.name, private_data_dir=temp_dir, inventory=inventory
            )

    # manage devicetype library
    if not skipdtl:
        logger.info("Manage devicetype library")
        dtl_repo = DTLRepo(settings.DEVICETYPE_LIBRARY)
        files, vendors = dtl_repo.get_devices()
        device_types = dtl_repo.parse_files(files)

        dtl_netbox = DTLNetBox(settings)
        dtl_netbox.create_manufacturers(vendors)
        dtl_netbox.create_device_types(device_types)

    files = []
    for extension in ["yml", "yaml"]:
        files.extend(glob.glob(os.path.join(settings.RESOURCES, f"*.{extension}")))

    template = Template(playbook_template)
    for file in natsorted(files):
        if limit and not os.path.basename(file).startswith(limit):
            logger.info(f"Skipping {os.path.basename(file)}")
            continue

        template_vars = {}
        template_tasks = []
        with open(file) as fp:
            data = yaml.safe_load(fp)
            for rtask in data:
                key, value = next(iter(rtask.items()))
                if key == "vars":
                    template_vars = value
                else:
                    task = {
                        "name": f"Manage NetBox resource {value.get('name', '')} of type {key}".replace(
                            "  ", " "
                        ),
                        f"netbox.netbox.netbox_{key}": {
                            "data": value,
                            "netbox_token": settings.TOKEN,
                            "netbox_url": settings.URL,
                            "validate_certs": settings.IGNORE_SSL_ERRORS,
                        },
                    }
                    template_tasks.append(task)

        playbook_resources = template.render(
            {
                "name": os.path.basename(file),
                "vars": yaml.dump(template_vars, indent=2, default_flow_style=False),
                "tasks": yaml.dump(template_tasks, indent=2, default_flow_style=False),
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with tempfile.NamedTemporaryFile(
                mode="w+", suffix=".yml", delete=False
            ) as temp_file:
                temp_file.write(playbook_resources)

            ansible_runner.run(
                playbook=temp_file.name, private_data_dir=temp_dir, inventory=inventory
            )

    end = time.time()
    logger.info(f"Runtime: {(end-start):.4f}s")


def main() -> None:
    typer.run(run)


if __name__ == "__main__":
    main()
