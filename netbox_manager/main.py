# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import glob
from importlib import metadata
from itertools import groupby
import os
import platform
import signal
import subprocess
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

# NOTE: Register validators for common settings
settings.validators.register(
    Validator("DEVICETYPE_LIBRARY", is_type_of=str)
    | Validator("DEVICETYPE_LIBRARY", is_type_of=None, default=None),
    Validator("MODULETYPE_LIBRARY", is_type_of=str)
    | Validator("MODULETYPE_LIBRARY", is_type_of=None, default=None),
    Validator("RESOURCES", is_type_of=str)
    | Validator("RESOURCES", is_type_of=None, default=None),
    Validator("IGNORED_FILES", is_type_of=list)
    | Validator(
        "IGNORED_FILES",
        is_type_of=None,
        default=["000-external.yml", "000-external.yaml"],
    ),
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


def validate_netbox_connection():
    """Validate NetBox connection settings."""
    settings.validators.register(
        Validator("TOKEN", is_type_of=str),
        Validator("URL", is_type_of=str),
    )
    try:
        settings.validators.validate_all()
    except ValidationError as e:
        logger.error(f"Error validating NetBox connection settings: {e.details}")
        raise typer.Exit()


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


def get_leading_number(file: str) -> str:
    return file.split("-")[0]


def find_device_names_in_structure(data: dict) -> list[str]:
    """Recursively search for device names in a nested data structure."""
    device_names = []

    def _recursive_search(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "device" and isinstance(value, str):
                    device_names.append(value)
                elif isinstance(value, (dict, list)):
                    _recursive_search(value)
        elif isinstance(obj, list):
            for item in obj:
                _recursive_search(item)

    _recursive_search(data)
    return device_names


def handle_file(
    file: str,
    dryrun: bool,
    task_filter: Optional[str] = None,
    device_filters: Optional[list[str]] = None,
) -> None:
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
                # Apply task filter if specified
                if task_filter and key != task_filter:
                    logger.debug(
                        f"Skipping task of type '{key}' (filter: {task_filter})"
                    )
                    continue

                # Apply device filter if specified
                if device_filters:
                    device_names = []

                    # Check if task has a 'device' field (for tasks that reference a device)
                    if "device" in value:
                        device_names.append(value["device"])
                    # Check if task has a 'name' field and this is a device creation task
                    elif key == "device" and "name" in value:
                        device_names.append(value["name"])

                    # Search for device names in nested structures
                    nested_device_names = find_device_names_in_structure(value)
                    device_names.extend(nested_device_names)

                    # If we found device names, check if any matches the filters
                    if device_names:
                        task_matches_filter = False
                        for device_name in device_names:
                            if any(
                                filter_device in device_name
                                for filter_device in device_filters
                            ):
                                task_matches_filter = True
                                break

                        if not task_matches_filter:
                            logger.debug(
                                f"Skipping task with devices '{device_names}' (device filters: {device_filters})"
                            )
                            continue
                    else:
                        # If no device name found and device filters are active, skip this task
                        logger.debug(
                            f"Skipping task of type '{key}' with no device reference (device filters active)"
                        )
                        continue

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

    # Skip file if no tasks remain after filtering
    if not template_tasks:
        logger.info(f"No tasks to execute in {file} after filtering")
        return

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


def init_logger(debug: bool = False) -> None:
    """Initialize logger with consistent format and level."""
    log_fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<level>{message}</level>"
    )

    log_level = "DEBUG" if debug else "INFO"

    logger.remove()
    logger.add(sys.stderr, format=log_fmt, level=log_level, colorize=True)


def callback_version(value: bool):
    if value:
        print(f"Version {metadata.version('netbox-manager')}")
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
    task_filter: Optional[str] = None,
    include_ignored_files: bool = False,
    filter_device: Optional[list[str]] = None,
) -> None:
    start = time.time()

    # Initialize logger
    init_logger(debug)

    # Validate NetBox connection settings for run command
    validate_netbox_connection()

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

        # Create playbook_wait with validated settings
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

        # Filter out ignored files unless include_ignored_files is True
        if not include_ignored_files:
            ignored_files = getattr(
                settings, "IGNORED_FILES", ["000-external.yml", "000-external.yaml"]
            )
            files_filtered = [
                f
                for f in files_filtered
                if not any(
                    os.path.basename(f) == ignored_file
                    for ignored_file in ignored_files
                )
            ]
            if debug and len(files) != len(files_filtered):
                logger.debug(
                    f"Filtered out {len(files) - len(files_filtered)} ignored files"
                )

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
                        executor.submit(
                            handle_file, file, dryrun, task_filter, filter_device
                        )
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
    task_filter: Annotated[
        Optional[str], typer.Option(help="Filter tasks by type (e.g., 'device')")
    ] = None,
    include_ignored_files: Annotated[
        bool, typer.Option(help="Include files that are normally ignored")
    ] = False,
    filter_device: Annotated[
        Optional[list[str]],
        typer.Option(help="Filter tasks by device name (can be used multiple times)"),
    ] = None,
) -> None:
    """Process NetBox resources, device types, and module types."""
    _run_main(
        always,
        debug,
        dryrun,
        limit,
        parallel,
        version,
        skipdtl,
        skipmtl,
        skipres,
        wait,
        task_filter,
        include_ignored_files,
        filter_device,
    )


@app.command(
    name="export-archive",
    help="Export devicetypes, moduletypes, and resources to netbox-export.tar.gz",
)
def export_archive(
    image: bool = typer.Option(
        False,
        "--image",
        "-i",
        help="Create an ext4 image file containing the tarball",
    ),
    image_size: int = typer.Option(
        100,
        "--image-size",
        help="Size of the ext4 image in MB (default: 100)",
    ),
) -> None:
    """Export devicetypes, moduletypes, and resources to netbox-export.tar.gz."""
    # Initialize logger
    init_logger()

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
    image_file = "netbox-export.img"
    mount_point = "/tmp/netbox-export-mount"

    try:
        with tarfile.open(output_file, "w:gz") as tar:
            for directory in directories:
                logger.info(f"Adding {directory} to archive")
                tar.add(directory, arcname=os.path.basename(directory))

        logger.info(f"Export completed: {output_file}")

        if image:
            # Check if running on Linux
            if platform.system() != "Linux":
                logger.error("Creating ext4 images is only supported on Linux systems")
                raise typer.Exit(1)

            # Create image file with specified size
            logger.info(f"Creating {image_size}MB ext4 image: {image_file}")
            os.system(
                f"dd if=/dev/zero of={image_file} bs=1M count={image_size} 2>/dev/null"
            )

            # Create ext4 filesystem
            logger.info("Creating ext4 filesystem")
            os.system(f"mkfs.ext4 -q {image_file}")

            # Create mount point
            os.makedirs(mount_point, exist_ok=True)

            # Mount the image
            logger.info(f"Mounting image to {mount_point}")
            mount_result = os.system(f"sudo mount -o loop {image_file} {mount_point}")

            if mount_result != 0:
                logger.error("Failed to mount image (requires sudo)")
                raise typer.Exit(1)

            try:
                # Copy tarball to mounted image
                logger.info("Copying tarball to image")
                os.system(f"sudo cp {output_file} {mount_point}/")

                # Sync and unmount
                os.system("sync")
                logger.info("Unmounting image")
                os.system(f"sudo umount {mount_point}")

            except Exception as e:
                logger.error(f"Error during copy: {e}")
                os.system(f"sudo umount {mount_point}")
                raise

            # Clean up
            os.rmdir(mount_point)
            os.remove(output_file)

            logger.info(
                f"Export completed: {image_file} ({image_size}MB ext4 image containing {output_file})"
            )

    except Exception as e:
        logger.error(f"Failed to create export: {e}")
        raise typer.Exit(1)


@app.command(
    name="import-archive",
    help="Import and sync content from a netbox-export.tar.gz file",
)
def import_archive(
    input_file: str = typer.Option(
        "netbox-export.tar.gz",
        "--input",
        "-i",
        help="Input tarball file to import (default: netbox-export.tar.gz)",
    ),
    destination: str = typer.Option(
        "/opt/configuration/netbox",
        "--destination",
        "-d",
        help="Destination directory for imported content (default: /opt/configuration/netbox)",
    ),
) -> None:
    """Import and sync content from a netbox-export.tar.gz file to local directories."""
    # Initialize logger
    init_logger()

    if not os.path.exists(input_file):
        logger.error(f"Input file not found: {input_file}")
        raise typer.Exit(1)

    # Create temporary directory for extraction
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            logger.info(f"Extracting {input_file} to temporary directory")
            with tarfile.open(input_file, "r:gz") as tar:
                tar.extractall(temp_dir)

            # Process each extracted directory
            for item in os.listdir(temp_dir):
                source_path = os.path.join(temp_dir, item)
                if not os.path.isdir(source_path):
                    continue

                # Target path is the item name under the destination directory
                target_path = os.path.join(destination, item)
                logger.info(f"Syncing {item} to {target_path}")

                # Ensure target directory exists
                os.makedirs(target_path, exist_ok=True)

                # Use rsync to sync directories
                rsync_cmd = [
                    "rsync",
                    "-av",
                    "--delete",
                    f"{source_path}/",
                    f"{target_path}/",
                ]

                result = subprocess.run(rsync_cmd, capture_output=True, text=True)

                if result.returncode != 0:
                    logger.error(f"rsync failed: {result.stderr}")
                    raise typer.Exit(1)

                logger.info(f"Successfully synced {item}")

            logger.info("Import completed successfully")

        except Exception as e:
            logger.error(f"Failed to import: {e}")
            raise typer.Exit(1)


@app.command(name="version", help="Show version information")
def version_command() -> None:
    """Display version information for netbox-manager."""
    print(f"netbox-manager {metadata.version('netbox-manager')}")


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
