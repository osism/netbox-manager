# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import glob
from itertools import groupby
import os
import pkg_resources
import signal
import sys
import tarfile
import tempfile
import time
from typing import Optional
from typing_extensions import Annotated
import warnings

import ansible_runner
from dynaconf import Dynaconf, Validator, ValidationError
import git
from jinja2 import Template
from loguru import logger
import pynetbox
import typer
import yaml

from .dtl import Repo, NetBox

files_changed: list[str] = []

warnings.filterwarnings("ignore")

settings = Dynaconf(
    envvar_prefix="NETBOX_MANAGER",
    settings_files=["settings.toml", ".secrets.toml"],
    load_dotenv=True,
)

# NOTE: lazy validate configuration
settings.validators.register(
    Validator("DEVICETYPE_LIBRARY", is_type_of=str)
    | Validator("DEVICETYPE_LIBRARY", is_type_of=None, default=None),
    Validator("MODULETYPE_LIBRARY", is_type_of=str)
    | Validator("MODULETYPE_LIBRARY", is_type_of=None, default=None),
    Validator("RESOURCES", is_type_of=str)
    | Validator("RESOURCES", is_type_of=None, default=None),
    Validator("TOKEN", is_type_of=str),
    Validator("URL", is_type_of=str),
    Validator("IGNORE_SSL_ERRORS", is_type_of=bool)
    | Validator(
        "IGNORE_SSL_ERRORS",
        is_type_of=str,
        cast=lambda v: v.lower() in ["true", "yes"],
        default=False,
    ),
    Validator("VERBOSE", is_type_of=bool)
    | Validator(
        "VERBOSE",
        is_type_of=str,
        cast=lambda v: v.lower() in ["true", "yes"],
        default=False,
    ),
)

try:
    settings.validators.validate_all()
except ValidationError as e:
    logger.error(f"Error validating configuration: {e.details}")
    raise typer.Exit()

nb = pynetbox.api(settings.URL, token=settings.TOKEN)

inventory = {
    "all": {
        "hosts": {
            "localhost": {
                "ansible_connection": "local",
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
    with tempfile.TemporaryDirectory() as temp_dir:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".yml", delete=False
        ) as temp_file:
            temp_file.write(playbook_resources)

        if dryrun:
            logger.info(f"Skip the execution of {file} as only one dry run")
        else:
            ansible_runner.run(
                playbook=temp_file.name,
                private_data_dir=temp_dir,
                inventory=inventory,
                cancel_callback=lambda: None,
            )


def signal_handler_sigint(sig, frame):
    print("SIGINT received. Exit.")
    raise typer.Exit()


def callback_version(value: bool):
    if value:
        print(f"Version {pkg_resources.get_distribution('netbox-manager').version}")
        raise typer.Exit()


def _run_main(
    always: bool = True,
    debug: bool = False,
    dryrun: bool = False,
    limit: Optional[str] = None,
    parallel: Optional[int] = 1,
    version: Optional[bool] = None,
    skipdtl: bool = False,
    skipmtl: bool = False,
    skipres: bool = False,
    wait: bool = True,
) -> None:
    start = time.time()

    log_fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<level>{message}</level>"
    )

    if debug:
        log_level = "DEBUG"
    else:
        log_level = "INFO"

    logger.remove()
    logger.add(sys.stderr, format=log_fmt, level=log_level, colorize=True)

    # install netbox.netbox collection
    # ansible-galaxy collection install netbox.netbox

    # check for changed files
    if not always:
        try:
            config_repo = git.Repo(".")
        except git.exc.InvalidGitRepositoryError:
            logger.error(
                "If only changed files are to be processed, the netbox-manager must be called in a Git repository."
            )
            raise typer.Exit()

        commit = config_repo.head.commit
        files_changed = [str(item.a_path) for item in commit.diff(commit.parents[0])]

        if debug:
            logger.debug(
                "A list of the changed files follows. Only changed files are processed."
            )
            for f in files_changed:
                logger.debug(f"- {f}")

        # skip devicetype library when no files changed there
        if not skipdtl and not any(
            f.startswith(settings.DEVICETYPE_LIBRARY) for f in files_changed
        ):
            logger.debug(
                "No file changes in the devicetype library. Devicetype library will be skipped."
            )
            skipdtl = True

        # skip moduletype library when no files changed there
        if not skipmtl and not any(
            f.startswith(settings.MODULETYPE_LIBRARY) for f in files_changed
        ):
            logger.debug(
                "No file changes in the moduletype library. Moduletype library will be skipped."
            )
            skipmtl = True

        # skip resources when no files changed there
        if not skipres and not any(
            f.startswith(settings.RESOURCES) for f in files_changed
        ):
            logger.debug("No file changes in the resources. Resources will be skipped.")
            skipres = True

    if skipdtl and skipmtl and skipres:
        raise typer.Exit()

    # wait for NetBox service
    if wait:
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

    # prepare devicetype and moduletype library
    if (settings.DEVICETYPE_LIBRARY and not skipdtl) or (
        settings.MODULETYPE_LIBRARY and not skipmtl
    ):
        dtl_netbox = NetBox(settings)

    # manage devicetypes
    if settings.DEVICETYPE_LIBRARY and not skipdtl:
        logger.info("Manage devicetypes")

        dtl_repo = Repo(settings.DEVICETYPE_LIBRARY)

        try:
            files, vendors = dtl_repo.get_devices()
            device_types = dtl_repo.parse_files(files)

            dtl_netbox.create_manufacturers(vendors)
            dtl_netbox.create_device_types(device_types)
        except FileNotFoundError:
            logger.error(
                f"Could not load device types in {settings.DEVICETYPE_LIBRARY}"
            )

    # manage moduletypes
    if settings.MODULETYPE_LIBRARY and not skipmtl:
        logger.info("Manage moduletypes")

        dtl_repo = Repo(settings.MODULETYPE_LIBRARY)

        try:
            files, vendors = dtl_repo.get_devices()
            module_types = dtl_repo.parse_files(files)

            dtl_netbox.create_manufacturers(vendors)
            dtl_netbox.create_module_types(module_types)
        except FileNotFoundError:
            logger.error(
                f"Could not load module types in {settings.MODULETYPE_LIBRARY}"
            )

    # manage resources
    if not skipres:
        logger.info("Manage resources")

        files = []
        for extension in ["yml", "yaml"]:
            try:
                files.extend(
                    glob.glob(os.path.join(settings.RESOURCES, f"*.{extension}"))
                )
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
                        executor.submit(handle_file, file, dryrun)
                        for file in files_process
                    ]
                    for future in concurrent.futures.as_completed(futures):
                        future.result()

    end = time.time()
    logger.info(f"Runtime: {(end-start):.4f}s")


app = typer.Typer()


@app.command(
    name="run", help="Process NetBox resources, device types, and module types"
)
def run_command(
    always: Annotated[bool, typer.Option(help="Always run")] = True,
    debug: Annotated[bool, typer.Option(help="Debug")] = False,
    dryrun: Annotated[bool, typer.Option(help="Dry run")] = False,
    limit: Annotated[Optional[str], typer.Option(help="Limit files by prefix")] = None,
    parallel: Annotated[
        Optional[int], typer.Option(help="Process up to n files in parallel")
    ] = 1,
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version",
            help="Show version and exit",
            callback=callback_version,
            is_eager=True,
        ),
    ] = None,
    skipdtl: Annotated[bool, typer.Option(help="Skip devicetype library")] = False,
    skipmtl: Annotated[bool, typer.Option(help="Skip moduletype library")] = False,
    skipres: Annotated[bool, typer.Option(help="Skip resources")] = False,
    wait: Annotated[bool, typer.Option(help="Wait for NetBox service")] = True,
) -> None:
    """Process NetBox resources, device types, and module types."""
    _run_main(
        always, debug, dryrun, limit, parallel, version, skipdtl, skipmtl, skipres, wait
    )


@app.command(
    help="Export devicetypes, moduletypes, and resources to netbox-export.tar.gz"
)
def export() -> None:
    """Export devicetypes, moduletypes, and resources to netbox-export.tar.gz."""
    log_fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<level>{message}</level>"
    )
    logger.remove()
    logger.add(sys.stderr, format=log_fmt, level="INFO", colorize=True)

    directories = []
    if settings.DEVICETYPE_LIBRARY and os.path.exists(settings.DEVICETYPE_LIBRARY):
        directories.append(settings.DEVICETYPE_LIBRARY)
    if settings.MODULETYPE_LIBRARY and os.path.exists(settings.MODULETYPE_LIBRARY):
        directories.append(settings.MODULETYPE_LIBRARY)
    if settings.RESOURCES and os.path.exists(settings.RESOURCES):
        directories.append(settings.RESOURCES)

    if not directories:
        logger.error("No directories found to export")
        raise typer.Exit(1)

    output_file = "netbox-export.tar.gz"

    try:
        with tarfile.open(output_file, "w:gz") as tar:
            for directory in directories:
                logger.info(f"Adding {directory} to archive")
                tar.add(directory, arcname=os.path.basename(directory))

        logger.info(f"Export completed: {output_file}")
    except Exception as e:
        logger.error(f"Failed to create archive: {e}")
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """Handle default behavior when no command is specified."""
    if ctx.invoked_subcommand is None:
        # Default to run command when no subcommand is specified
        run_command()


def main() -> None:
    signal.signal(signal.SIGINT, signal_handler_sigint)
    app()


if __name__ == "__main__":
    main()
