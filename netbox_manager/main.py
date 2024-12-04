import glob
import os
import sys
import tempfile
import time
import warnings

import ansible_runner
from dynaconf import Dynaconf
from jinja2 import Template
from loguru import logger
import pynetbox
import typer
import yaml

from dtl import DTLRepo, DTLNetBox

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
- name: Manage resources in {{ name }}
  connection: local
  hosts: localhost
  gather_facts: false

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


def run() -> None:
    start = time.time()

    # install netbox.netbox collection
    # ansible-galaxy collection install netbox.netbox

    # wait for NetBox service
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
    for file in files:
        tasks = []
        with open(file) as fp:
            data = yaml.safe_load(fp)
            for task in data:
                for key in task.keys():
                    if key.startswith("netbox.netbox"):
                        task[key]["netbox_token"] = settings.TOKEN
                        task[key]["netbox_url"] = settings.URL
                        task[key]["validate_certs"] = settings.IGNORE_SSL_ERRORS
                tasks.append(task)

        playbook_resources = template.render(
            {
                "name": os.path.basename(file),
                "tasks": yaml.dump(tasks, indent=2, default_flow_style=False),
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