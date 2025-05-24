# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import glob
from itertools import groupby
import os
from typing import Optional

from jinja2 import Template
from loguru import logger
import yaml

from .config import settings
from .playbook import playbook_template, run_playbook


def get_leading_number(file: str) -> str:
    return file.split("-")[0]


def handle_file(file: str, dryrun: bool) -> None:
    template = Template(playbook_template)

    template_vars = {}
    template_tasks = []

    logger.info(f"Handle file {file}")
    with open(file) as fp:
        data = yaml.safe_load(fp)
        for rtask in data:
            key, value = next(iter(rtask.items()))
            if key == "vars":
                template_vars = value
            elif key == "debug":
                task = {"ansible.builtin.debug": value}
                template_tasks.append(task)
            else:
                state = "present"
                if "state" in value:
                    state = value["state"]
                    del value["state"]

                task = {
                    "name": f"Manage NetBox resource {value.get('name', '')} of type {key}".replace(
                        "  ", " "
                    ),
                    f"netbox.netbox.netbox_{key}": {
                        "data": value,
                        "state": state,
                        "netbox_token": settings.TOKEN,
                        "netbox_url": settings.URL,
                        "validate_certs": not settings.IGNORE_SSL_ERRORS,
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

    if dryrun:
        logger.info(f"Skip the execution of {file} as only one dry run")
    else:
        run_playbook(playbook_resources, dryrun)


def process_resources(
    files_changed: list[str],
    always: bool,
    limit: Optional[str],
    parallel: int,
    dryrun: bool,
) -> None:
    """Process NetBox resources."""
    logger.info("Manage resources")

    files = []
    for extension in ["yml", "yaml"]:
        try:
            files.extend(glob.glob(os.path.join(settings.RESOURCES, f"*.{extension}")))
        except FileNotFoundError:
            logger.error(f"Could not load resources in {settings.RESOURCES}")

    if not always:
        files_filtered = [f for f in files if f in files_changed]
    else:
        files_filtered = files

    files_filtered.sort(key=get_leading_number)
    files_grouped = []
    for _, group in groupby(files_filtered, key=get_leading_number):
        files_grouped.append(list(group))

    for group in files_grouped:  # type: ignore[assignment]
        files_process = []
        for file in group:
            if limit and not os.path.basename(file).startswith(limit):
                logger.info(f"Skipping {os.path.basename(file)}")
                continue

            files_process.append(file)

        if files_process:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=parallel
            ) as executor:
                futures = [
                    executor.submit(handle_file, file, dryrun) for file in files_process
                ]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
