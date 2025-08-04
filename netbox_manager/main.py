# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import glob
from importlib import metadata
import ipaddress
from itertools import groupby
import os
import platform
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Optional
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
from copy import deepcopy

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
    Validator("VARS", is_type_of=str)
    | Validator("VARS", is_type_of=None, default=None),
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

# Define device roles that should get Loopback0 interfaces
NETBOX_NODE_ROLES = [
    "compute",
    "storage",
    "resource",
    "control",
    "manager",
    "network",
    "metalbox",
    "dpu",
    "loadbalancer",
    "router",
    "firewall",
]

# Define switch roles that should also get Loopback0 interfaces
NETBOX_SWITCH_ROLES = [
    "accessleaf",
    "borderleaf",
    "computeleaf",
    "dataleaf",
    "leaf",
    "serviceleaf",
    "spine",
    "storageleaf",
    "superspine",
    "switch",
    "transferleaf",
]


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


def get_leading_number(path: str) -> str:
    basename = os.path.basename(path)
    return basename.split("-")[0]


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


def deep_merge(dict1: dict, dict2: dict) -> dict:
    """Deep merge two dictionaries, with dict2 values taking precedence."""
    result = deepcopy(dict1)

    for key, value in dict2.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)

    return result


def load_global_vars() -> dict:
    """Load and merge global variables from the VARS directory."""
    global_vars: dict[str, Any] = {}

    vars_dir = getattr(settings, "VARS", None)
    if not vars_dir:
        return global_vars
    if not os.path.exists(vars_dir):
        logger.debug(f"VARS directory {vars_dir} does not exist, skipping global vars")
        return global_vars

    # Find all YAML files in the vars directory
    yaml_files = []
    for ext in ["*.yml", "*.yaml"]:
        yaml_files.extend(glob.glob(os.path.join(vars_dir, ext)))

    # Sort files by filename for consistent order
    yaml_files.sort()

    logger.debug(f"Loading global vars from {len(yaml_files)} files in {vars_dir}")

    for yaml_file in yaml_files:
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                file_vars = yaml.safe_load(f)
                if file_vars:
                    logger.debug(f"Loading vars from {os.path.basename(yaml_file)}")
                    global_vars = deep_merge(global_vars, file_vars)
        except Exception as e:
            logger.error(f"Error loading vars from {yaml_file}: {e}")

    return global_vars


def handle_file(
    file: str,
    dryrun: bool,
    task_filter: Optional[str] = None,
    device_filters: Optional[list[str]] = None,
) -> None:
    template = Template(playbook_template)

    # Load global vars first
    template_vars = load_global_vars()
    template_tasks = []

    logger.info(f"Handle file {file}")
    with open(file) as fp:
        data = yaml.safe_load(fp)
        for rtask in data:
            key, value = next(iter(rtask.items()))
            if key == "vars":
                # Merge local vars with global vars, local vars take precedence
                template_vars = deep_merge(template_vars, value)
            elif key == "debug":
                task = {"ansible.builtin.debug": value}
                template_tasks.append(task)
            else:
                # Apply task filter if specified
                if task_filter:
                    # Normalize filter to handle both underscore and hyphen variations
                    normalized_filter = task_filter.replace("-", "_")
                    normalized_key = key.replace("-", "_")

                    if normalized_key != normalized_filter:
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
    filter_task: Optional[str] = None,
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

        # Find files directly in resources directory
        for extension in ["yml", "yaml"]:
            try:
                top_level_files = glob.glob(
                    os.path.join(settings.RESOURCES, f"*.{extension}")
                )
                # Apply limit filter at file level
                if limit:
                    top_level_files = [
                        f
                        for f in top_level_files
                        if os.path.basename(f).startswith(limit)
                    ]
                files.extend(top_level_files)
            except FileNotFoundError:
                logger.error(f"Could not load resources in {settings.RESOURCES}")

        # Find files in numbered subdirectories (excluding vars directory)
        vars_dirname = None
        vars_dir = getattr(settings, "VARS", None)
        if vars_dir:
            vars_dirname = os.path.basename(vars_dir)

        try:
            for item in os.listdir(settings.RESOURCES):
                item_path = os.path.join(settings.RESOURCES, item)
                if os.path.isdir(item_path) and (
                    not vars_dirname or item != vars_dirname
                ):
                    # Only process directories that start with a number and hyphen
                    if re.match(r"^\d+-.+", item):
                        # Apply limit filter at directory level
                        if limit and not item.startswith(limit):
                            continue

                        dir_files = []
                        for extension in ["yml", "yaml"]:
                            dir_files.extend(
                                glob.glob(os.path.join(item_path, f"*.{extension}"))
                            )
                        # Sort files within the directory by their basename
                        dir_files.sort(key=lambda f: os.path.basename(f))
                        files.extend(dir_files)
        except FileNotFoundError:
            pass

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
            if group:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=parallel
                ) as executor:
                    futures = [
                        executor.submit(
                            handle_file, file, dryrun, filter_task, filter_device
                        )
                        for file in group
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
    filter_task: Annotated[
        Optional[str],
        typer.Option(help="Filter tasks by type (e.g., 'device', 'device_interface')"),
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
        filter_task,
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


def _generate_loopback_interfaces() -> list[dict]:
    """Generate Loopback0 interfaces for eligible devices that don't have them."""
    tasks = []

    # Initialize NetBox API connection
    netbox_api = pynetbox.api(settings.URL, token=settings.TOKEN)
    if settings.IGNORE_SSL_ERRORS:
        netbox_api.http_session.verify = False

    logger.info("Analyzing devices for Loopback0 interface creation...")

    # Get all devices
    all_devices = netbox_api.dcim.devices.all()

    for device in all_devices:
        # Determine if this device should have a Loopback0 interface
        should_have_loopback = False

        # Check if device has role and if it's in the list of roles that need Loopback0
        if device.role:
            device_role_slug = ""
            if hasattr(device.role, "slug"):
                device_role_slug = device.role.slug.lower()
            elif hasattr(device.role, "name"):
                device_role_slug = device.role.name.lower()

            # Node roles always get Loopback0 interfaces
            if device_role_slug in NETBOX_NODE_ROLES:
                should_have_loopback = True
            # Switch roles only get Loopback0 if they have sonic_parameters.hwsku custom field
            elif device_role_slug in NETBOX_SWITCH_ROLES:
                # Check for sonic_parameters custom field with hwsku
                if hasattr(device, "custom_fields") and device.custom_fields:
                    sonic_params = device.custom_fields.get("sonic_parameters")
                    if (
                        sonic_params
                        and isinstance(sonic_params, dict)
                        and sonic_params.get("hwsku")
                    ):
                        should_have_loopback = True
                        logger.debug(
                            f"Switch {device.name} has sonic_parameters.hwsku: {sonic_params.get('hwsku')}"
                        )
                    else:
                        logger.debug(
                            f"Switch {device.name} does not have sonic_parameters.hwsku, skipping Loopback0"
                        )

        # Check if device type name contains "switch" (case insensitive)
        if device.device_type and hasattr(device.device_type, "model"):
            device_type_name = device.device_type.model.lower()
            if "switch" in device_type_name:
                # Also check for sonic_parameters.hwsku for device types containing "switch"
                if hasattr(device, "custom_fields") and device.custom_fields:
                    sonic_params = device.custom_fields.get("sonic_parameters")
                    if (
                        sonic_params
                        and isinstance(sonic_params, dict)
                        and sonic_params.get("hwsku")
                    ):
                        should_have_loopback = True
                        logger.debug(
                            f"Switch device {device.name} (type: {device_type_name}) has sonic_parameters.hwsku: {sonic_params.get('hwsku')}"
                        )
                    else:
                        logger.debug(
                            f"Switch device {device.name} (type: {device_type_name}) does not have sonic_parameters.hwsku, skipping Loopback0"
                        )

        if not should_have_loopback:
            continue

        # Create Loopback0 interface task (always create regardless of existence)
        tasks.append(
            {
                "device_interface": {
                    "device": device.name,
                    "name": "Loopback0",
                    "type": "virtual",
                    "enabled": True,
                    "tags": ["managed-by-osism"],
                }
            }
        )
        logger.info(f"Will create Loopback0 interface for device: {device.name}")

    logger.info(f"Generated {len(tasks)} Loopback0 interface creation tasks")
    return tasks


def _get_cluster_segment_config_context(
    netbox_api: pynetbox.api, cluster_id: int, cluster_name: str = ""
) -> dict:
    """
    Retrieve the specific segment config context for a cluster via separate API call.

    Each cluster has a config context assigned with the same name as the segment.
    This function retrieves the content of that specific config context.

    Args:
        netbox_api: The NetBox API connection
        cluster_id: The cluster ID to retrieve context for
        cluster_name: Optional cluster name for logging

    Returns:
        dict: The configuration context data from the segment-specific config context
    """
    try:
        logger.debug(
            f"Retrieving segment config context for cluster {cluster_name} (ID: {cluster_id}) via separate API call"
        )

        # Get all config contexts that apply to this cluster
        config_contexts = netbox_api.extras.config_contexts.filter(clusters=cluster_id)

        # Look for the config context with the same name as the cluster (segment name)
        segment_context = None
        for ctx in config_contexts:
            if ctx.name == cluster_name:
                logger.debug(
                    f"Found segment config context: '{ctx.name}' for cluster {cluster_name}"
                )
                segment_context = ctx
                break

        if segment_context and segment_context.data:
            logger.info(
                f"Retrieved segment config context '{segment_context.name}' for cluster {cluster_name}"
            )

            # Log the specific loopback configuration found
            if "_loopback_network_ipv4" in segment_context.data:
                logger.debug(
                    f"Found loopback config in {segment_context.name}: IPv4={segment_context.data.get('_loopback_network_ipv4')}, IPv6={segment_context.data.get('_loopback_network_ipv6')}"
                )

            return segment_context.data
        elif segment_context and not segment_context.data:
            logger.warning(
                f"Config context '{segment_context.name}' found for cluster {cluster_name} but contains no data"
            )
            return {}
        else:
            logger.warning(
                f"No segment config context found for cluster {cluster_name} (expected config context with name '{cluster_name}')"
            )
            return {}

    except Exception as e:
        logger.error(
            f"Error retrieving segment config context for cluster {cluster_name} (ID: {cluster_id}): {e}"
        )
        return {}


def _generate_cluster_loopback_tasks() -> list[dict]:
    """Generate loopback IP address assignments for devices with assigned clusters."""
    tasks = []

    # Initialize NetBox API connection
    netbox_api = pynetbox.api(settings.URL, token=settings.TOKEN)
    if settings.IGNORE_SSL_ERRORS:
        netbox_api.http_session.verify = False

    logger.info("Analyzing devices with clusters for loopback IP generation...")

    # Get all devices with clusters assigned
    devices_with_clusters = []
    all_devices = netbox_api.dcim.devices.all()

    for device in all_devices:
        if device.cluster:
            devices_with_clusters.append(device)
            logger.debug(
                f"Found device {device.name} with cluster {device.cluster.name}"
            )

    logger.info(f"Found {len(devices_with_clusters)} devices with assigned clusters")

    # Group devices by cluster
    clusters_dict = {}
    for device in devices_with_clusters:
        cluster_id = device.cluster.id
        if cluster_id not in clusters_dict:
            clusters_dict[cluster_id] = {"cluster": device.cluster, "devices": []}
        clusters_dict[cluster_id]["devices"].append(device)

    # Process each cluster
    for cluster_id, cluster_data in clusters_dict.items():
        cluster = cluster_data["cluster"]
        devices = cluster_data["devices"]

        logger.info(f"Processing cluster '{cluster.name}' with {len(devices)} devices")

        # Get the segment-specific config context via separate API call
        try:
            config_context = _get_cluster_segment_config_context(
                netbox_api, cluster_id, cluster.name
            )
            if not config_context:
                logger.warning(
                    f"Cluster '{cluster.name}' has no config context assigned, skipping loopback generation for {len(devices)} devices"
                )
                continue

            # Extract loopback network configuration
            loopback_ipv4_network = config_context.get("_loopback_network_ipv4")
            loopback_ipv6_network = config_context.get("_loopback_network_ipv6")
            loopback_offset_ipv4 = config_context.get("_loopback_offset_ipv4", 0)

            if not loopback_ipv4_network:
                logger.info(
                    f"Cluster '{cluster.name}' has no _loopback_network_ipv4 in config context, skipping"
                )
                continue

            logger.debug(
                f"Cluster '{cluster.name}' config: IPv4={loopback_ipv4_network}, IPv6={loopback_ipv6_network}, offset={loopback_offset_ipv4}"
            )

            # Parse IPv4 network
            try:
                ipv4_network = ipaddress.IPv4Network(
                    loopback_ipv4_network, strict=False
                )
            except ValueError as e:
                logger.error(
                    f"Invalid IPv4 network '{loopback_ipv4_network}' for cluster '{cluster.name}': {e}"
                )
                continue

            # Parse IPv6 network if provided
            ipv6_network = None
            if loopback_ipv6_network:
                try:
                    ipv6_network = ipaddress.IPv6Network(
                        loopback_ipv6_network, strict=False
                    )
                except ValueError as e:
                    logger.error(
                        f"Invalid IPv6 network '{loopback_ipv6_network}' for cluster '{cluster.name}': {e}"
                    )

            # Generate IP addresses for each device
            for device in devices:
                # Get device position (rack position)
                position = getattr(device, "position", None)
                if position is None:
                    logger.warning(
                        f"Device '{device.name}' has no rack position, skipping loopback generation"
                    )
                    continue

                # Validate position is an integer
                if not isinstance(position, int):
                    try:
                        position = int(position)
                        logger.debug(
                            f"Device '{device.name}' position converted from {type(getattr(device, 'position', None)).__name__} to int: {position}"
                        )
                    except (ValueError, TypeError) as e:
                        logger.warning(
                            f"Device '{device.name}' has invalid position '{getattr(device, 'position', None)}' (not convertible to int), skipping loopback generation: {e}"
                        )
                        continue

                # Calculate IPv4 address: byte_4 = device_position * 2 - 1 + offset
                byte_4 = position * 2 - 1 + loopback_offset_ipv4

                try:
                    # Convert network to list of octets and modify the last octet
                    network_int = int(ipv4_network.network_address)
                    device_ipv4_int = network_int + byte_4
                    device_ipv4 = ipaddress.IPv4Address(device_ipv4_int)
                    device_ipv4_with_mask = f"{device_ipv4}/32"

                    # Generate IPv4 task
                    tasks.append(
                        {
                            "ip_address": {
                                "address": device_ipv4_with_mask,
                                "assigned_object": {
                                    "name": "Loopback0",
                                    "device": device.name,
                                },
                            }
                        }
                    )
                    logger.info(
                        f"Generated IPv4 loopback: {device.name} -> {device_ipv4_with_mask}"
                    )

                    # Generate IPv6 address based on IPv4
                    if ipv6_network:
                        try:
                            # Convert IPv4 to IPv6: fd93:363d:dab8:0:10:10:128:3/128
                            ipv4_octets = str(device_ipv4).split(".")
                            ipv6_suffix = f"{ipv4_octets[0]}:{ipv4_octets[1]}:{ipv4_octets[2]}:{ipv4_octets[3]}"

                            # Create IPv6 address using the network prefix and IPv4-based suffix
                            network_prefix = str(ipv6_network.network_address).rstrip(
                                "::"
                            )
                            if network_prefix.endswith(":"):
                                network_prefix = network_prefix.rstrip(":")
                            device_ipv6 = f"{network_prefix}:0:{ipv6_suffix}/128"

                            tasks.append(
                                {
                                    "ip_address": {
                                        "address": device_ipv6,
                                        "assigned_object": {
                                            "name": "Loopback0",
                                            "device": device.name,
                                        },
                                    }
                                }
                            )
                            logger.info(
                                f"Generated IPv6 loopback: {device.name} -> {device_ipv6}"
                            )

                        except Exception as e:
                            logger.error(
                                f"Error generating IPv6 address for device '{device.name}': {e}"
                            )

                except Exception as e:
                    logger.error(
                        f"Error generating IPv4 address for device '{device.name}': {e}"
                    )

        except Exception as e:
            logger.error(f"Error processing cluster '{cluster.name}': {e}")
            continue

    logger.info(f"Generated {len(tasks)} cluster-based loopback IP assignment tasks")
    return tasks


def _generate_autoconf_tasks() -> list[dict]:
    """Generate automatic configuration tasks based on NetBox API data."""
    tasks = []

    # Initialize NetBox API connection
    netbox_api = pynetbox.api(settings.URL, token=settings.TOKEN)
    if settings.IGNORE_SSL_ERRORS:
        netbox_api.http_session.verify = False

    logger.info("Analyzing NetBox data for automatic configuration...")

    # Get all devices first and filter out switches
    logger.info("Filtering out switch devices...")
    all_devices = netbox_api.dcim.devices.all()
    non_switch_devices = {}

    for device in all_devices:
        if device.role and hasattr(device.role, "slug"):
            device_role_slug = device.role.slug.lower()
        elif device.role and hasattr(device.role, "name"):
            device_role_slug = device.role.name.lower()
        else:
            device_role_slug = ""

        if device_role_slug not in NETBOX_SWITCH_ROLES:
            non_switch_devices[device.id] = device

    logger.info(
        f"Found {len(non_switch_devices)} non-switch devices out of {len(all_devices)} total devices"
    )

    # 1. MAC address assignment for interfaces
    logger.info("Checking interfaces for MAC address assignments...")

    for device_id, device in non_switch_devices.items():
        # Get interfaces for this specific device
        device_interfaces = netbox_api.dcim.interfaces.filter(device_id=device_id)

        for interface in device_interfaces:
            # Skip virtual interfaces
            if (
                interface.type
                and hasattr(interface.type, "value")
                and "virtual" in interface.type.value.lower()
            ):
                continue
            if (
                interface.type
                and hasattr(interface.type, "label")
                and "virtual" in interface.type.label.lower()
            ):
                continue

            # Check if interface has a MAC address that should be set as primary
            mac_to_assign = None
            if interface.mac_address:
                # Use existing mac_address as primary_mac_address
                mac_to_assign = interface.mac_address
            elif interface.mac_addresses and not interface.mac_address:
                # Use first MAC from mac_addresses list as primary
                mac_to_assign = interface.mac_addresses[0]

            if mac_to_assign:
                tasks.append(
                    {
                        "device_interface": {
                            "device": device.name,
                            "name": interface.name,
                            "primary_mac_address": mac_to_assign,
                        }
                    }
                )
                logger.info(
                    f"Found MAC assignment: {device.name}:{interface.name} -> {mac_to_assign}"
                )

    # 2. Consolidated device IP assignments (OOB, primary IPv4, primary IPv6)
    logger.info("Checking for device IP assignments...")

    # Dictionary to collect all device assignments by device name
    device_assignments = {}

    # Collect OOB IP assignments from eth0 interfaces
    logger.info("Checking eth0 interfaces for OOB IP assignments...")
    for device_id, device in non_switch_devices.items():
        # Get eth0 interface for this specific device
        eth0_interfaces = netbox_api.dcim.interfaces.filter(
            device_id=device_id, name="eth0"
        )

        for interface in eth0_interfaces:
            # Get IP addresses assigned to this interface
            ip_addresses = netbox_api.ipam.ip_addresses.filter(
                assigned_object_id=interface.id
            )

            for ip_addr in ip_addresses:
                if device.name not in device_assignments:
                    device_assignments[device.name] = {"name": device.name}

                device_assignments[device.name]["oob_ip"] = ip_addr.address
                logger.info(
                    f"Found OOB IP assignment: {device.name} -> {ip_addr.address}"
                )

    # Collect primary IPv4 and IPv6 assignments from Loopback0 interfaces
    logger.info("Checking Loopback0 interfaces for primary IP assignments...")
    for device_id, device in non_switch_devices.items():
        # Get Loopback0 interface for this specific device
        loopback_interfaces = netbox_api.dcim.interfaces.filter(
            device_id=device_id, name="Loopback0"
        )

        for interface in loopback_interfaces:
            # Get IP addresses assigned to this interface
            ip_addresses = netbox_api.ipam.ip_addresses.filter(
                assigned_object_id=interface.id
            )

            for ip_addr in ip_addresses:
                if device.name not in device_assignments:
                    device_assignments[device.name] = {"name": device.name}

                # Check if this is an IPv4 or IPv6 address
                if ":" not in ip_addr.address:  # Simple IPv4 check
                    device_assignments[device.name]["primary_ip4"] = ip_addr.address
                    logger.info(
                        f"Found primary IPv4 assignment: {device.name} -> {ip_addr.address}"
                    )
                else:  # IPv6 address
                    device_assignments[device.name]["primary_ip6"] = ip_addr.address
                    logger.info(
                        f"Found primary IPv6 assignment: {device.name} -> {ip_addr.address}"
                    )

    # Create consolidated device tasks from collected assignments
    for device_assignment in device_assignments.values():
        tasks.append({"device": device_assignment})

    logger.info(f"Generated {len(tasks)} automatic configuration tasks")
    return tasks


@app.command(
    name="autoconf", help="Generate automatic configuration based on NetBox data"
)
def autoconf_command(
    output: Annotated[str, typer.Option(help="Output file path")] = "999-autoconf.yml",
    loopback_output: Annotated[
        str, typer.Option(help="Loopback interfaces output file path")
    ] = "299-autoconf.yml",
    cluster_loopback_output: Annotated[
        str, typer.Option(help="Cluster-based loopback IPs output file path")
    ] = "399-autoconf.yml",
    debug: Annotated[bool, typer.Option(help="Debug")] = False,
    dryrun: Annotated[
        bool, typer.Option(help="Dry run - show tasks but don't write file")
    ] = False,
) -> None:
    """Generate automatic configuration based on NetBox API data.

    This command analyzes the NetBox database and generates configuration tasks
    for common patterns:

    1. Create Loopback0 interfaces for switches and devices with specific roles
    2. Generate cluster-based loopback IP addresses for devices with assigned clusters
    3. Assign primary MAC addresses to interfaces that have exactly one MAC
    4. Assign OOB IP addresses from eth0 interfaces to devices
    5. Assign primary IPv4 addresses from Loopback0 interfaces to devices
    6. Assign primary IPv6 addresses from Loopback0 interfaces to devices

    The loopback interface tasks are written to 299-autoconf.yml, cluster-based
    loopback IP tasks are written to 399-autoconf.yml, and other tasks are written
    to 999-autoconf.yml in the standard netbox-manager resource format.
    """
    # Initialize logger
    init_logger(debug)

    # Validate NetBox connection settings
    validate_netbox_connection()

    try:
        # Generate loopback interface tasks
        loopback_tasks = _generate_loopback_interfaces()

        # Generate cluster-based loopback IP tasks
        cluster_loopback_tasks = _generate_cluster_loopback_tasks()

        # Generate other autoconf tasks
        other_tasks = _generate_autoconf_tasks()

        if dryrun:
            if loopback_tasks:
                logger.info(
                    "Dry run - would generate the following loopback interface tasks:"
                )
                for task in loopback_tasks:
                    logger.info(
                        f"  {yaml.dump(task, default_flow_style=False).strip()}"
                    )

            if cluster_loopback_tasks:
                logger.info(
                    "Dry run - would generate the following cluster-based loopback IP tasks:"
                )
                for task in cluster_loopback_tasks:
                    logger.info(
                        f"  {yaml.dump(task, default_flow_style=False).strip()}"
                    )

            if other_tasks:
                logger.info(
                    "Dry run - would generate the following other autoconf tasks:"
                )
                for task in other_tasks:
                    logger.info(
                        f"  {yaml.dump(task, default_flow_style=False).strip()}"
                    )
            return

        files_written = 0

        # Handle loopback interfaces file
        if loopback_tasks:
            # Ensure output directory exists for loopback file
            loopback_output_dir = (
                os.path.dirname(loopback_output)
                if os.path.dirname(loopback_output)
                else settings.RESOURCES
            )
            if loopback_output_dir and not os.path.exists(loopback_output_dir):
                os.makedirs(loopback_output_dir, exist_ok=True)

            # If loopback_output is just a filename, put it in the RESOURCES directory
            if not os.path.dirname(loopback_output) and settings.RESOURCES:
                loopback_output = os.path.join(settings.RESOURCES, loopback_output)

            # Write loopback tasks to YAML file
            with open(loopback_output, "w") as f:
                yaml.dump(loopback_tasks, f, default_flow_style=False, sort_keys=False)

            logger.info(
                f"Generated {len(loopback_tasks)} loopback interface tasks in {loopback_output}"
            )
            files_written += 1

        # Handle cluster-based loopback IP tasks file
        if cluster_loopback_tasks:
            # Ensure output directory exists for cluster loopback file
            cluster_loopback_output_dir = (
                os.path.dirname(cluster_loopback_output)
                if os.path.dirname(cluster_loopback_output)
                else settings.RESOURCES
            )
            if cluster_loopback_output_dir and not os.path.exists(
                cluster_loopback_output_dir
            ):
                os.makedirs(cluster_loopback_output_dir, exist_ok=True)

            # If cluster_loopback_output is just a filename, put it in the RESOURCES directory
            if not os.path.dirname(cluster_loopback_output) and settings.RESOURCES:
                cluster_loopback_output = os.path.join(
                    settings.RESOURCES, cluster_loopback_output
                )

            # Write cluster loopback tasks to YAML file
            with open(cluster_loopback_output, "w") as f:
                yaml.dump(
                    cluster_loopback_tasks, f, default_flow_style=False, sort_keys=False
                )

            logger.info(
                f"Generated {len(cluster_loopback_tasks)} cluster-based loopback IP tasks in {cluster_loopback_output}"
            )
            files_written += 1

        # Handle other autoconf tasks file
        if other_tasks:
            # Ensure output directory exists
            output_dir = (
                os.path.dirname(output)
                if os.path.dirname(output)
                else settings.RESOURCES
            )
            if output_dir and not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)

            # If output is just a filename, put it in the RESOURCES directory
            if not os.path.dirname(output) and settings.RESOURCES:
                output = os.path.join(settings.RESOURCES, output)

            # Write other tasks to YAML file
            with open(output, "w") as f:
                yaml.dump(other_tasks, f, default_flow_style=False, sort_keys=False)

            logger.info(
                f"Generated {len(other_tasks)} other autoconf tasks in {output}"
            )
            files_written += 1

        if files_written == 0:
            logger.info("No automatic configuration tasks found")

    except pynetbox.RequestError as e:
        logger.error(f"NetBox API error: {e}")
        raise typer.Exit(1)
    except Exception as e:
        logger.error(f"Error generating autoconf: {e}")
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
