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
from typing import Any, Optional, Tuple, Dict, List
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
    """Extract the leading number from a filename for grouping purposes."""
    basename = os.path.basename(path)
    return basename.split("-")[0]


def find_device_names_in_structure(data: Dict[str, Any]) -> List[str]:
    """Recursively search for device names in a nested data structure."""
    device_names = []

    def _recursive_search(obj: Any) -> None:
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


def deep_merge(dict1: Dict[str, Any], dict2: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries, with dict2 values taking precedence."""
    result = deepcopy(dict1)

    for key, value in dict2.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)

    return result


def find_yaml_files(directory: str) -> List[str]:
    """Find all YAML files in a directory and return sorted list."""
    yaml_files = []
    for ext in ["*.yml", "*.yaml"]:
        yaml_files.extend(glob.glob(os.path.join(directory, ext)))
    return sorted(yaml_files)


def create_netbox_api() -> pynetbox.api:
    """Create and configure NetBox API connection."""
    api = pynetbox.api(settings.URL, token=settings.TOKEN)
    if settings.IGNORE_SSL_ERRORS:
        api.http_session.verify = False
    return api


def get_device_role_slug(device: Any) -> str:
    """Extract device role slug from a device object."""
    if not device.role:
        return ""

    if hasattr(device.role, "slug"):
        return device.role.slug.lower()
    elif hasattr(device.role, "name"):
        return device.role.name.lower()
    return ""


def get_resource_name(resource: Any) -> str:
    """Extract a displayable name from a resource object."""
    return getattr(
        resource,
        "name",
        getattr(
            resource,
            "address",
            getattr(resource, "id", "unknown"),
        ),
    )


def load_global_vars() -> Dict[str, Any]:
    """Load and merge global variables from the VARS directory."""
    global_vars: Dict[str, Any] = {}

    vars_dir = getattr(settings, "VARS", None)
    if not vars_dir:
        return global_vars
    if not os.path.exists(vars_dir):
        logger.debug(f"VARS directory {vars_dir} does not exist, skipping global vars")
        return global_vars

    yaml_files = find_yaml_files(vars_dir)
    logger.debug(f"Loading global vars from {len(yaml_files)} files in {vars_dir}")

    for yaml_file in yaml_files:
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                file_vars = yaml.safe_load(f)
                if file_vars:
                    logger.debug(f"Loading vars from {os.path.basename(yaml_file)}")
                    global_vars = deep_merge(global_vars, file_vars)
        except yaml.YAMLError as e:
            # Extract line and column information if available
            error_msg = f"Invalid YAML syntax in vars file '{yaml_file}'"
            if hasattr(e, "problem_mark"):
                mark = e.problem_mark
                error_msg += f" at line {mark.line + 1}, column {mark.column + 1}"
            if hasattr(e, "problem"):
                error_msg += f": {e.problem}"
            if hasattr(e, "context"):
                error_msg += f" ({e.context})"
            logger.error(error_msg)
        except FileNotFoundError:
            logger.error(f"Vars file not found: {yaml_file}")
        except Exception as e:
            logger.error(f"Error loading vars from {yaml_file}: {e}")

    return global_vars


def should_skip_task_by_filter(key: str, task_filter: str) -> bool:
    """Check if task should be skipped based on task filter."""
    normalized_filter = task_filter.replace("-", "_")
    normalized_key = key.replace("-", "_")
    return normalized_key != normalized_filter


def extract_device_names_from_task(key: str, value: Dict[str, Any]) -> List[str]:
    """Extract all device names referenced in a task."""
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

    return device_names


def should_skip_task_by_device_filter(
    device_names: List[str], device_filters: List[str]
) -> bool:
    """Check if task should be skipped based on device filters."""
    if not device_names:
        return True  # Skip if no device names found

    return not any(
        filter_device in device_name
        for device_name in device_names
        for filter_device in device_filters
    )


def create_netbox_task(
    key: str, value: Dict[str, Any], register_var: Optional[str] = None
) -> Dict[str, Any]:
    """Create a NetBox Ansible task from resource data."""
    state = value.pop("state", "present")

    # Extract update_vc_child parameter for device_interface tasks
    update_vc_child = None
    if key == "device_interface" and "update_vc_child" in value:
        update_vc_child = value.pop("update_vc_child")

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

    # Add update_vc_child at the same level as data for device_interface tasks
    if update_vc_child is not None:
        netbox_module_key = f"netbox.netbox.netbox_{key}"
        netbox_module_config = task[netbox_module_key]
        assert isinstance(netbox_module_config, dict)  # Type narrowing for mypy
        netbox_module_config["update_vc_child"] = update_vc_child

    # Add register field if specified
    if register_var:
        task["register"] = register_var

    return task


def create_uri_task(
    value: Dict[str, Any], register_var: Optional[str] = None
) -> Dict[str, Any]:
    """Create an ansible.builtin.uri task for direct NetBox API calls.

    Args:
        value: Dictionary containing 'body', 'method', and 'path' parameters
        register_var: Optional variable name to register the result

    Returns:
        Dict containing the Ansible task configuration
    """
    # Extract parameters from value
    body = value.get("body", {})
    method = value.get("method", "GET")  # Default to GET if not specified
    path = value.get("path", "")

    # Ensure path doesn't start with /api/ as it will be added automatically
    if path.startswith("/api/"):
        path = path[5:]  # Remove /api/ prefix
    elif path.startswith("api/"):
        path = path[4:]  # Remove api/ prefix without leading slash

    # Clean up the path - remove leading slashes to avoid double slashes
    path = path.lstrip("/")

    # Construct the full URL
    netbox_url = settings.URL.rstrip("/")
    full_url = f"{netbox_url}/api/{path}"

    # Create the task
    task = {
        "name": f"NetBox API call: {method} {path}",
        "ansible.builtin.uri": {
            "url": full_url,
            "method": method,
            "headers": {
                "Authorization": f"Token {settings.TOKEN}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            "body_format": "json",
            "body": body if body else None,
            "validate_certs": not settings.IGNORE_SSL_ERRORS,
            "status_code": [200, 201, 204],
        },
    }

    # Remove body if it's empty (for GET requests)
    if not body:
        uri_config = task["ansible.builtin.uri"]
        assert isinstance(uri_config, dict)  # Type narrowing for mypy
        del uri_config["body"]
        del uri_config["body_format"]

    # Add register field if specified
    if register_var:
        task["register"] = register_var

    return task


def create_ansible_playbook(
    file: str, template_vars: Dict[str, Any], template_tasks: List[Dict[str, Any]]
) -> str:
    """Create Ansible playbook from template variables and tasks."""
    template = Template(playbook_template)
    return template.render(
        {
            "name": os.path.basename(file),
            "vars": yaml.dump(template_vars, indent=2, default_flow_style=False),
            "tasks": yaml.dump(template_tasks, indent=2, default_flow_style=False),
        }
    )


def handle_file(
    file: str,
    dryrun: bool,
    task_filter: Optional[str] = None,
    device_filters: Optional[List[str]] = None,
    fail_fast: bool = False,
    show_playbooks: bool = False,
    verbose: bool = False,
) -> None:
    """Process a single YAML resource file and execute corresponding Ansible playbook."""
    # Load global vars first
    template_vars = load_global_vars()
    template_tasks = []

    logger.info(f"Handle file {file}")
    try:
        with open(file) as fp:
            data = yaml.safe_load(fp)
    except yaml.YAMLError as e:
        # Extract line and column information if available
        error_msg = f"Invalid YAML syntax in file '{file}'"
        if hasattr(e, "problem_mark"):
            mark = e.problem_mark
            error_msg += f" at line {mark.line + 1}, column {mark.column + 1}"
        if hasattr(e, "problem"):
            error_msg += f": {e.problem}"
        if hasattr(e, "context"):
            error_msg += f" ({e.context})"
        logger.error(error_msg)
        if fail_fast:
            raise typer.Exit(1)
        return
    except FileNotFoundError:
        logger.error(f"File not found: {file}")
        if fail_fast:
            raise typer.Exit(1)
        return
    except Exception as e:
        logger.error(f"Error reading file '{file}': {e}")
        if fail_fast:
            raise typer.Exit(1)
        return

    # Check if data is None (empty file)
    if data is None:
        logger.warning(f"File '{file}' is empty or contains only comments")
        return

    # Check if data is a list (expected format)
    if not isinstance(data, list):
        logger.error(
            f"Invalid YAML structure in file '{file}': Expected a list of tasks, got {type(data).__name__}"
        )
        if fail_fast:
            raise typer.Exit(1)
        return

    try:
        for idx, rtask in enumerate(data):
            # Validate task structure
            if not isinstance(rtask, dict):
                logger.error(
                    f"Invalid task structure in file '{file}' at index {idx}: Expected a dictionary, got {type(rtask).__name__}"
                )
                if fail_fast:
                    raise typer.Exit(1)
                continue

            if not rtask:
                logger.warning(f"Empty task in file '{file}' at index {idx}, skipping")
                continue

            # Check if task has a register field
            register_var = rtask.pop("register", None)

            # Get the first remaining key-value pair (the actual task)
            try:
                key, value = next(iter(rtask.items()))
            except StopIteration:
                logger.warning(
                    f"Task in file '{file}' at index {idx} has no content after removing 'register' field, skipping"
                )
                continue
            if key == "vars":
                # Merge local vars with global vars, local vars take precedence
                template_vars = deep_merge(template_vars, value)
            elif key == "debug":
                task = {"ansible.builtin.debug": value}
                # Add register field if specified for debug tasks
                if register_var:
                    task["register"] = register_var
                template_tasks.append(task)
            elif key == "uri":
                # Handle direct NetBox API calls via ansible.builtin.uri
                task = create_uri_task(value, register_var)
                template_tasks.append(task)
            else:
                # Apply task filter if specified
                if task_filter and should_skip_task_by_filter(key, task_filter):
                    logger.debug(
                        f"Skipping task of type '{key}' (filter: {task_filter})"
                    )
                    continue

                # Apply device filter if specified
                if device_filters:
                    device_names = extract_device_names_from_task(key, value)
                    if should_skip_task_by_device_filter(device_names, device_filters):
                        if device_names:
                            logger.debug(
                                f"Skipping task with devices '{device_names}' (device filters: {device_filters})"
                            )
                        else:
                            logger.debug(
                                f"Skipping task of type '{key}' with no device reference (device filters active)"
                            )
                        continue

                task = create_netbox_task(key, value, register_var)
                template_tasks.append(task)
    except Exception as e:
        logger.error(f"Error processing tasks in file '{file}': {e}")
        if fail_fast:
            raise typer.Exit(1)
        return

    # Skip file if no tasks remain after filtering
    if not template_tasks:
        logger.info(f"No tasks to execute in {file} after filtering")
        return

    playbook_resources = create_ansible_playbook(file, template_vars, template_tasks)

    if show_playbooks:
        # Output the playbook to stdout
        print(f"# Playbook for {file}")
        print(playbook_resources)
        print()  # Add blank line between playbooks
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".yml", delete=False
        ) as temp_file:
            temp_file.write(playbook_resources)

        if dryrun:
            logger.info(f"Skip the execution of {file} as only one dry run")
        else:
            # Prepare verbosity parameters - ansible-runner expects an integer
            verbosity = 3 if verbose else None

            result = ansible_runner.run(
                playbook=temp_file.name,
                private_data_dir=temp_dir,
                inventory=inventory,
                cancel_callback=lambda: None,
                verbosity=verbosity,
                envvars={
                    "ANSIBLE_STDOUT_CALLBACK": "ansible.builtin.default",
                    "ANSIBLE_CALLBACKS_ENABLED": "ansible.builtin.default",
                    "ANSIBLE_STDOUT_CALLBACK_RESULT_FORMAT": "yaml",
                },
            )
            if fail_fast and result.status == "failed":
                logger.error(
                    f"Ansible playbook failed for {file}. Exiting due to --fail option."
                )
                raise typer.Exit(1)


def signal_handler_sigint(sig: int, frame: Any) -> None:
    """Handle SIGINT signal gracefully."""
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


def process_device_and_module_types(
    settings_attr: str, skip_flag: bool, type_name: str
) -> None:
    """Process device types or module types with common logic."""
    library_path = getattr(settings, settings_attr, None)
    if not library_path or skip_flag:
        return

    logger.info(f"Manage {type_name}")
    dtl_repo = Repo(library_path)
    dtl_netbox = NetBox(settings)

    try:
        files, vendors = dtl_repo.get_devices()
        types_data = dtl_repo.parse_files(files)

        dtl_netbox.create_manufacturers(vendors)

        if type_name == "devicetypes":
            dtl_netbox.create_device_types(types_data)
        else:  # moduletypes
            dtl_netbox.create_module_types(types_data)

    except FileNotFoundError:
        logger.error(f"Could not load {type_name} in {library_path}")


def discover_resource_files(
    resources_dir: str, limit: Optional[str] = None
) -> List[str]:
    """Discover and return sorted list of resource files."""
    files = []

    # Find files directly in resources directory
    for extension in ["yml", "yaml"]:
        try:
            top_level_files = glob.glob(os.path.join(resources_dir, f"*.{extension}"))
            # Apply limit filter at file level
            if limit:
                top_level_files = [
                    f for f in top_level_files if os.path.basename(f).startswith(limit)
                ]
            files.extend(top_level_files)
        except FileNotFoundError:
            logger.error(f"Could not load resources in {resources_dir}")

    # Find files in numbered subdirectories (excluding vars directory)
    vars_dirname = None
    vars_dir = getattr(settings, "VARS", None)
    if vars_dir:
        vars_dirname = os.path.basename(vars_dir)

    try:
        for item in os.listdir(resources_dir):
            item_path = os.path.join(resources_dir, item)
            if os.path.isdir(item_path) and (not vars_dirname or item != vars_dirname):
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

    return files


def callback_version(value: bool) -> None:
    """Show version and exit if requested."""
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
    fail_fast: bool = False,
    show_playbooks: bool = False,
    verbose: bool = False,
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
                envvars={
                    "ANSIBLE_STDOUT_CALLBACK": "ansible.builtin.default",
                    "ANSIBLE_CALLBACKS_ENABLED": "ansible.builtin.default",
                    "ANSIBLE_STDOUT_CALLBACK_RESULT_FORMAT": "yaml",
                },
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
                            handle_file,
                            file,
                            dryrun,
                            filter_task,
                            filter_device,
                            fail_fast,
                            show_playbooks,
                            verbose,
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
        Optional[List[str]],
        typer.Option(help="Filter tasks by device name (can be used multiple times)"),
    ] = None,
    fail_fast: Annotated[
        bool, typer.Option("--fail-fast", help="Exit on first Ansible playbook failure")
    ] = False,
    show_playbooks: Annotated[
        bool,
        typer.Option(
            "--show-playbooks",
            help="Output generated playbooks to stdout without executing them",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose", help="Run ansible-playbook with -vvv for detailed output"
        ),
    ] = False,
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
        fail_fast,
        show_playbooks,
        verbose,
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


def has_sonic_hwsku_parameter(device: Any) -> bool:
    """Check if device has sonic_parameters.hwsku custom field."""
    if not (hasattr(device, "custom_fields") and device.custom_fields):
        return False

    sonic_params = device.custom_fields.get("sonic_parameters")
    return bool(
        sonic_params and isinstance(sonic_params, dict) and sonic_params.get("hwsku")
    )


def should_have_loopback_interface(device: Any) -> bool:
    """Determine if a device should have a Loopback0 interface."""
    device_role_slug = get_device_role_slug(device)

    # Node roles always get Loopback0 interfaces
    if device_role_slug in NETBOX_NODE_ROLES:
        return True

    # Switch roles and switch device types only get Loopback0 if they have sonic_parameters.hwsku
    is_switch_role = device_role_slug in NETBOX_SWITCH_ROLES
    is_switch_type = (
        device.device_type
        and hasattr(device.device_type, "model")
        and "switch" in device.device_type.model.lower()
    )

    if is_switch_role or is_switch_type:
        if has_sonic_hwsku_parameter(device):
            sonic_params = device.custom_fields.get("sonic_parameters")
            context = (
                f"role: {device_role_slug}"
                if is_switch_role
                else f"type: {device.device_type.model.lower()}"
            )
            logger.debug(
                f"Switch {device.name} ({context}) has sonic_parameters.hwsku: {sonic_params.get('hwsku')}"
            )
            return True
        else:
            context = (
                f"role: {device_role_slug}"
                if is_switch_role
                else f"type: {device.device_type.model.lower()}"
            )
            logger.debug(
                f"Switch {device.name} ({context}) does not have sonic_parameters.hwsku, skipping Loopback0"
            )

    return False


def _generate_loopback_interfaces() -> List[Dict[str, Any]]:
    """Generate Loopback0 interfaces for eligible devices that don't have them."""
    tasks = []
    netbox_api = create_netbox_api()

    logger.info("Analyzing devices for Loopback0 interface creation...")
    all_devices = netbox_api.dcim.devices.all()

    for device in all_devices:
        if should_have_loopback_interface(device):
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
) -> Dict[str, Any]:
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
            if "_segment_loopback_network_ipv4" in segment_context.data:
                ipv4_net = segment_context.data.get("_segment_loopback_network_ipv4")
                ipv6_net = segment_context.data.get("_segment_loopback_network_ipv6")
                logger.debug(
                    f"Found loopback config in {segment_context.name}: IPv4={ipv4_net}, IPv6={ipv6_net}"
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


def group_devices_by_cluster(
    devices_with_clusters: List[Any],
) -> Dict[int, Dict[str, Any]]:
    """Group devices by their assigned cluster."""
    clusters_dict = {}
    for device in devices_with_clusters:
        cluster_id = device.cluster.id
        if cluster_id not in clusters_dict:
            clusters_dict[cluster_id] = {"cluster": device.cluster, "devices": []}
        clusters_dict[cluster_id]["devices"].append(device)
    return clusters_dict


def calculate_loopback_ips(
    device: Any, ipv4_network: Any, ipv6_network: Optional[Any], offset: int
) -> Tuple[Optional[str], Optional[str]]:
    """Calculate IPv4 and IPv6 loopback addresses for a device."""
    position = getattr(device, "position", None)
    if position is None:
        logger.warning(
            f"Device '{device.name}' has no rack position, skipping loopback generation"
        )
        return None, None

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
            return None, None

    # Calculate IPv4 address: byte_4 = device_position * 2 - 1 + offset
    byte_4 = position * 2 - 1 + offset

    try:
        # Convert network to list of octets and modify the last octet
        network_int = int(ipv4_network.network_address)
        device_ipv4_int = network_int + byte_4
        device_ipv4 = ipaddress.IPv4Address(device_ipv4_int)
        device_ipv4_with_mask = f"{device_ipv4}/32"

        ipv6_addr = None
        if ipv6_network:
            try:
                # Convert IPv4 to IPv6: fd93:363d:dab8:0:10:10:128:3/128
                ipv4_octets = str(device_ipv4).split(".")
                ipv6_suffix = f"{ipv4_octets[0]}:{ipv4_octets[1]}:{ipv4_octets[2]}:{ipv4_octets[3]}"

                # Create IPv6 address using the network prefix and IPv4-based suffix
                network_prefix = str(ipv6_network.network_address).rstrip("::")
                if network_prefix.endswith(":"):
                    network_prefix = network_prefix.rstrip(":")
                ipv6_addr = f"{network_prefix}:0:{ipv6_suffix}/128"
            except Exception as e:
                logger.error(
                    f"Error generating IPv6 address for device '{device.name}': {e}"
                )

        return device_ipv4_with_mask, ipv6_addr

    except Exception as e:
        logger.error(f"Error generating IPv4 address for device '{device.name}': {e}")
        return None, None


def _generate_cluster_loopback_tasks() -> Dict[str, List[Dict[str, Any]]]:
    """Generate loopback IP address assignments for devices with assigned clusters."""
    tasks_by_type: Dict[str, List[Dict[str, Any]]] = {"ip_address": []}
    netbox_api = create_netbox_api()

    logger.info("Analyzing devices with clusters for loopback IP generation...")

    # Get all devices with clusters assigned
    all_devices = netbox_api.dcim.devices.all()
    devices_with_clusters = [device for device in all_devices if device.cluster]

    for device in devices_with_clusters:
        logger.debug(f"Found device {device.name} with cluster {device.cluster.name}")

    logger.info(f"Found {len(devices_with_clusters)} devices with assigned clusters")

    # Group devices by cluster
    clusters_dict = group_devices_by_cluster(devices_with_clusters)

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
            loopback_ipv4_network = config_context.get("_segment_loopback_network_ipv4")
            loopback_ipv6_network = config_context.get("_segment_loopback_network_ipv6")
            loopback_offset_ipv4 = config_context.get(
                "_segment_loopback_offset_ipv4", 0
            )

            if not loopback_ipv4_network:
                logger.info(
                    f"Cluster '{cluster.name}' has no _segment_loopback_network_ipv4 in config context, skipping"
                )
                continue

            logger.debug(
                f"Cluster '{cluster.name}' config: IPv4={loopback_ipv4_network}, IPv6={loopback_ipv6_network}, offset={loopback_offset_ipv4}"
            )

            # Parse networks
            try:
                ipv4_network = ipaddress.IPv4Network(
                    loopback_ipv4_network, strict=False
                )
            except ValueError as e:
                logger.error(
                    f"Invalid IPv4 network '{loopback_ipv4_network}' for cluster '{cluster.name}': {e}"
                )
                continue

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
                ipv4_addr, ipv6_addr = calculate_loopback_ips(
                    device, ipv4_network, ipv6_network, loopback_offset_ipv4
                )

                if ipv4_addr:
                    tasks_by_type["ip_address"].append(
                        {
                            "ip_address": {
                                "address": ipv4_addr,
                                "assigned_object": {
                                    "name": "Loopback0",
                                    "device": device.name,
                                },
                            }
                        }
                    )
                    logger.info(
                        f"Generated IPv4 loopback: {device.name} -> {ipv4_addr}"
                    )

                if ipv6_addr:
                    tasks_by_type["ip_address"].append(
                        {
                            "ip_address": {
                                "address": ipv6_addr,
                                "assigned_object": {
                                    "name": "Loopback0",
                                    "device": device.name,
                                },
                            }
                        }
                    )
                    logger.info(
                        f"Generated IPv6 loopback: {device.name} -> {ipv6_addr}"
                    )

        except Exception as e:
            logger.error(f"Error processing cluster '{cluster.name}': {e}")
            continue

    total_tasks = sum(len(tasks) for tasks in tasks_by_type.values())
    logger.info(f"Generated {total_tasks} cluster-based loopback IP assignment tasks")
    return tasks_by_type


def _generate_device_interface_labels() -> List[Dict[str, Any]]:
    """Generate device interface label tasks based on switch, router, and firewall custom fields."""
    tasks = []
    netbox_api = create_netbox_api()

    logger.info(
        "Analyzing switch, router, and firewall devices for device interface labeling..."
    )

    # Get all devices and filter for switches, routers, and firewalls with device_interface_label custom field
    all_devices = netbox_api.dcim.devices.all()
    devices_with_labels = []

    for device in all_devices:
        device_role_slug = get_device_role_slug(device)

        # Check if device is a switch, router, or firewall with device_interface_label custom field
        if device_role_slug in NETBOX_SWITCH_ROLES or device_role_slug in [
            "router",
            "firewall",
        ]:
            if hasattr(device, "custom_fields") and device.custom_fields:
                device_interface_label = device.custom_fields.get(
                    "device_interface_label"
                )
                if device_interface_label:
                    devices_with_labels.append((device, device_interface_label))
                    device_type_name = (
                        "switch"
                        if device_role_slug in NETBOX_SWITCH_ROLES
                        else device_role_slug
                    )
                    logger.debug(
                        f"Found {device_type_name} {device.name} with device_interface_label: {device_interface_label}"
                    )

    logger.info(
        f"Found {len(devices_with_labels)} devices (switches/routers/firewalls) with device_interface_label custom field"
    )

    # Process each device with device_interface_label
    for source_device, label_value in devices_with_labels:
        source_device_role = get_device_role_slug(source_device)
        device_type_name = (
            "switch"
            if source_device_role in NETBOX_SWITCH_ROLES
            else source_device_role
        )
        logger.debug(
            f"Processing {device_type_name} {source_device.name} with label '{label_value}'"
        )

        # Get all interfaces for this device
        source_interfaces = netbox_api.dcim.interfaces.filter(
            device_id=source_device.id
        )

        for interface in source_interfaces:
            # Check if interface has connected endpoints
            if not (
                hasattr(interface, "connected_endpoints")
                and interface.connected_endpoints
            ):
                continue

            # Process each connected endpoint
            for endpoint in interface.connected_endpoints:
                # Check if endpoint has a device
                if not (hasattr(endpoint, "device") and endpoint.device):
                    continue

                connected_device = endpoint.device
                connected_role_slug = get_device_role_slug(connected_device)

                # Check if connected device is a node
                if connected_role_slug in NETBOX_NODE_ROLES:
                    interface_name = getattr(endpoint, "name", None)
                    if interface_name:
                        tasks.append(
                            {
                                "device_interface": {
                                    "device": connected_device.name,
                                    "name": interface_name,
                                    "label": label_value,
                                    "tags": ["managed-by-osism"],
                                }
                            }
                        )
                        logger.info(
                            f"Will set label on {connected_device.name}:{interface_name} -> '{label_value}' (from {device_type_name} {source_device.name}:{interface.name})"
                        )
                    else:
                        logger.warning(
                            f"Could not determine interface name for connection to {connected_device.name}"
                        )

    logger.info(f"Generated {len(tasks)} device interface label tasks")
    return tasks


def _split_tasks_by_type(
    all_tasks: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Split a list of tasks into separate lists by resource type."""
    tasks_by_type: Dict[str, List[Dict[str, Any]]] = {}

    for task in all_tasks:
        # Get the first key from the task dict (the resource type)
        resource_type = next(iter(task.keys()))
        if resource_type not in tasks_by_type:
            tasks_by_type[resource_type] = []
        tasks_by_type[resource_type].append(task)

    return tasks_by_type


def _write_autoconf_files(
    tasks_by_type: Dict[str, List[Dict[str, Any]]],
    file_prefix: str,
    resources_dir: Optional[str] = None,
) -> int:
    """Write autoconf tasks to separate files by resource type."""
    if not resources_dir and settings.RESOURCES:
        resources_dir = settings.RESOURCES

    files_written = 0

    for resource_type, tasks in tasks_by_type.items():
        if not tasks:
            continue

        filename = f"{file_prefix}-{resource_type.replace('_', '-')}.yml"

        if resources_dir:
            filepath = os.path.join(resources_dir, filename)
            # Ensure directory exists
            os.makedirs(resources_dir, exist_ok=True)
        else:
            filepath = filename

        with open(filepath, "w") as f:
            yaml.dump(tasks, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Generated {len(tasks)} {resource_type} tasks in {filepath}")
        files_written += 1

    return files_written


def is_virtual_interface(interface: Any) -> bool:
    """Check if an interface is virtual based on its type."""
    if interface.type:
        if (
            hasattr(interface.type, "value")
            and "virtual" in interface.type.value.lower()
        ):
            return True
        if (
            hasattr(interface.type, "label")
            and "virtual" in interface.type.label.lower()
        ):
            return True
    return False


def collect_interface_assignments(
    netbox_api: pynetbox.api, non_switch_devices: Dict[int, Any]
) -> List[Dict[str, Any]]:
    """Collect MAC address assignments for interfaces."""
    tasks = []
    logger.info("Checking interfaces for MAC address assignments...")

    for device_id, device in non_switch_devices.items():
        device_interfaces = netbox_api.dcim.interfaces.filter(device_id=device_id)

        for interface in device_interfaces:
            if is_virtual_interface(interface):
                continue

            # Check if interface has a MAC address that should be set as primary
            mac_to_assign = None
            if interface.mac_address:
                mac_to_assign = interface.mac_address
            elif interface.mac_addresses and not interface.mac_address:
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

    return tasks


def collect_ip_assignments_by_interface(
    netbox_api: pynetbox.api,
    non_switch_devices: Dict[int, Any],
    interface_name: str,
    assignment_type: str,
) -> Dict[str, Dict[str, Any]]:
    """Collect IP assignments from a specific interface type."""
    device_assignments = {}
    logger.info(
        f"Checking {interface_name} interfaces for {assignment_type} IP assignments..."
    )

    for device_id, device in non_switch_devices.items():
        interfaces = netbox_api.dcim.interfaces.filter(
            device_id=device_id, name=interface_name
        )

        for interface in interfaces:
            ip_addresses = netbox_api.ipam.ip_addresses.filter(
                assigned_object_id=interface.id
            )

            for ip_addr in ip_addresses:
                if device.name not in device_assignments:
                    device_assignments[device.name] = {"name": device.name}

                if assignment_type == "OOB":
                    device_assignments[device.name]["oob_ip"] = ip_addr.address
                    logger.info(
                        f"Found OOB IP assignment: {device.name} -> {ip_addr.address}"
                    )
                else:  # Primary IP assignments
                    if ":" not in ip_addr.address:  # IPv4
                        device_assignments[device.name]["primary_ip4"] = ip_addr.address
                        logger.info(
                            f"Found primary IPv4 assignment: {device.name} -> {ip_addr.address}"
                        )
                    else:  # IPv6
                        device_assignments[device.name]["primary_ip6"] = ip_addr.address
                        logger.info(
                            f"Found primary IPv6 assignment: {device.name} -> {ip_addr.address}"
                        )

    return device_assignments


def _generate_autoconf_tasks() -> Dict[str, List[Dict[str, Any]]]:
    """Generate automatic configuration tasks based on NetBox API data."""
    tasks_by_type: Dict[str, List[Dict[str, Any]]] = {
        "device": [],
        "device_interface": [],
        "ip_address": [],
    }

    netbox_api = create_netbox_api()
    logger.info("Analyzing NetBox data for automatic configuration...")

    # Get all devices first and filter out switches
    logger.info("Filtering out switch devices...")
    all_devices = netbox_api.dcim.devices.all()
    non_switch_devices = {}

    for device in all_devices:
        device_role_slug = get_device_role_slug(device)
        if device_role_slug not in NETBOX_SWITCH_ROLES:
            non_switch_devices[device.id] = device

    logger.info(
        f"Found {len(non_switch_devices)} non-switch devices out of {len(all_devices)} total devices"
    )

    # 1. MAC address assignment for interfaces
    interface_tasks = collect_interface_assignments(netbox_api, non_switch_devices)
    tasks_by_type["device_interface"].extend(interface_tasks)

    # 2. Consolidated device IP assignments (OOB, primary IPv4, primary IPv6)
    logger.info("Checking for device IP assignments...")

    # Collect OOB IP assignments from eth0 interfaces
    oob_assignments = collect_ip_assignments_by_interface(
        netbox_api, non_switch_devices, "eth0", "OOB"
    )

    # Collect primary IPv4 and IPv6 assignments from Loopback0 interfaces
    primary_assignments = collect_ip_assignments_by_interface(
        netbox_api, non_switch_devices, "Loopback0", "Primary"
    )

    # Merge assignments
    all_device_assignments = {}
    for assignments_dict in [oob_assignments, primary_assignments]:
        for device_name, assignment in assignments_dict.items():
            if device_name not in all_device_assignments:
                all_device_assignments[device_name] = assignment
            else:
                all_device_assignments[device_name].update(assignment)

    # Create consolidated device tasks from collected assignments
    for device_assignment in all_device_assignments.values():
        tasks_by_type["device"].append({"device": device_assignment})

    total_tasks = sum(len(tasks) for tasks in tasks_by_type.values())
    logger.info(f"Generated {total_tasks} automatic configuration tasks")
    return tasks_by_type


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
    3. Set interface labels on connected node devices based on switch device_interface_label custom field
    4. Assign primary MAC addresses to interfaces that have exactly one MAC
    5. Assign OOB IP addresses from eth0 interfaces to devices
    6. Assign primary IPv4 addresses from Loopback0 interfaces to devices
    7. Assign primary IPv6 addresses from Loopback0 interfaces to devices

    The loopback interface tasks are written to 299-autoconf.yml, cluster-based
    loopback IP tasks are written to 399-autoconf.yml, and other tasks (including
    interface labels) are written to 999-autoconf.yml in the standard netbox-manager
    resource format.
    """
    # Initialize logger
    init_logger(debug)

    # Validate NetBox connection settings
    validate_netbox_connection()

    try:
        # Generate loopback interface tasks
        loopback_tasks_list = _generate_loopback_interfaces()
        loopback_tasks = _split_tasks_by_type(loopback_tasks_list)

        # Generate cluster-based loopback IP tasks
        cluster_loopback_tasks = _generate_cluster_loopback_tasks()

        # Generate device interface label tasks
        interface_label_tasks_list = _generate_device_interface_labels()
        interface_label_tasks = _split_tasks_by_type(interface_label_tasks_list)

        # Generate other autoconf tasks
        other_autoconf_tasks = _generate_autoconf_tasks()

        # Merge interface label tasks with other autoconf tasks
        # We need to merge device_interface tasks to avoid duplicates
        merged_tasks: dict[str, list[dict]] = {}
        interface_task_map = {}

        # First, collect all interface label tasks by device:interface key
        for task in interface_label_tasks.get("device_interface", []):
            if "device_interface" in task:
                device_name = task["device_interface"]["device"]
                interface_name = task["device_interface"]["name"]
                key = f"{device_name}:{interface_name}"
                interface_task_map[key] = task["device_interface"]

        # Initialize merged_tasks with all resource types
        for resource_type in ["device", "device_interface", "ip_address"]:
            merged_tasks[resource_type] = []

        # Merge device_interface tasks from other_autoconf_tasks
        for task in other_autoconf_tasks.get("device_interface", []):
            if "device_interface" in task:
                device_name = task["device_interface"]["device"]
                interface_name = task["device_interface"]["name"]
                key = f"{device_name}:{interface_name}"

                if key in interface_task_map:
                    # Merge the tasks - combine all fields
                    merged_interface = {
                        **task["device_interface"],
                        **interface_task_map[key],
                    }
                    merged_tasks["device_interface"].append(
                        {"device_interface": merged_interface}
                    )
                    # Remove from interface_task_map so we don't add it twice
                    del interface_task_map[key]
                else:
                    merged_tasks["device_interface"].append(task)
            else:
                # This shouldn't happen but handle it gracefully
                merged_tasks["device_interface"].append(task)

        # Add any remaining interface label tasks that weren't merged
        for interface_data in interface_task_map.values():
            merged_tasks["device_interface"].append(
                {"device_interface": interface_data}
            )

        # Add other resource types from other_autoconf_tasks
        for resource_type in ["device", "ip_address"]:
            merged_tasks[resource_type].extend(
                other_autoconf_tasks.get(resource_type, [])
            )

        # Replace other_tasks with merged tasks
        other_tasks = merged_tasks

        if dryrun:
            if any(tasks for tasks in loopback_tasks.values()):
                logger.info(
                    "Dry run - would generate the following loopback interface tasks:"
                )
                for resource_type, tasks in loopback_tasks.items():
                    if tasks:
                        logger.info(f"  {resource_type}:")
                        for task in tasks:
                            logger.info(
                                f"    {yaml.dump(task, default_flow_style=False).strip()}"
                            )

            if any(tasks for tasks in cluster_loopback_tasks.values()):
                logger.info(
                    "Dry run - would generate the following cluster-based loopback IP tasks:"
                )
                for resource_type, tasks in cluster_loopback_tasks.items():
                    if tasks:
                        logger.info(f"  {resource_type}:")
                        for task in tasks:
                            logger.info(
                                f"    {yaml.dump(task, default_flow_style=False).strip()}"
                            )

            if any(tasks for tasks in other_tasks.values()):
                logger.info(
                    "Dry run - would generate the following other autoconf tasks:"
                )
                for resource_type, tasks in other_tasks.items():
                    if tasks:
                        logger.info(f"  {resource_type}:")
                        for task in tasks:
                            logger.info(
                                f"    {yaml.dump(task, default_flow_style=False).strip()}"
                            )
            return

        files_written = 0

        # Handle loopback interfaces files (split by type)
        if any(tasks for tasks in loopback_tasks.values()):
            loopback_prefix = os.path.splitext(os.path.basename(loopback_output))[0]
            loopback_dir = (
                os.path.dirname(loopback_output)
                if os.path.dirname(loopback_output)
                else settings.RESOURCES
            )
            files_written += _write_autoconf_files(
                loopback_tasks, loopback_prefix, loopback_dir
            )

        # Handle cluster-based loopback IP tasks files (split by type)
        if any(tasks for tasks in cluster_loopback_tasks.values()):
            cluster_loopback_prefix = os.path.splitext(
                os.path.basename(cluster_loopback_output)
            )[0]
            cluster_loopback_dir = (
                os.path.dirname(cluster_loopback_output)
                if os.path.dirname(cluster_loopback_output)
                else settings.RESOURCES
            )
            files_written += _write_autoconf_files(
                cluster_loopback_tasks, cluster_loopback_prefix, cluster_loopback_dir
            )

        # Handle other autoconf tasks files (split by type)
        if any(tasks for tasks in other_tasks.values()):
            other_prefix = os.path.splitext(os.path.basename(output))[0]
            other_dir = (
                os.path.dirname(output)
                if os.path.dirname(output)
                else settings.RESOURCES
            )
            files_written += _write_autoconf_files(other_tasks, other_prefix, other_dir)

        if files_written == 0:
            logger.info("No automatic configuration tasks found")

    except pynetbox.RequestError as e:
        logger.error(f"NetBox API error: {e}")
        raise typer.Exit(1)
    except Exception as e:
        logger.error(f"Error generating autoconf: {e}")
        raise typer.Exit(1)


@app.command(name="purge", help="Delete all managed resources from NetBox")
def purge_command(
    debug: Annotated[bool, typer.Option(help="Debug")] = False,
    dryrun: Annotated[
        bool, typer.Option(help="Dry run - show what would be deleted")
    ] = False,
    limit: Annotated[
        Optional[str], typer.Option(help="Limit deletion to specific resource type")
    ] = None,
    exclude_core: Annotated[
        bool, typer.Option(help="Exclude core resources (tenants, sites, locations)")
    ] = False,
    force: Annotated[
        bool, typer.Option(help="Force deletion without confirmation", prompt=False)
    ] = False,
    verbose: Annotated[
        bool, typer.Option(help="Show detailed information about what is being deleted")
    ] = False,
    parallel: Annotated[
        Optional[int],
        typer.Option(help="Delete up to n resources of same type in parallel"),
    ] = 1,
) -> None:
    """Delete all managed resources from NetBox.

    This command removes all resources created by netbox-manager while preserving:
    - Users and user accounts
    - API tokens
    - Permissions and roles
    - Custom fields

    Resources are deleted in reverse dependency order to avoid conflicts.

    Use --verbose to see detailed information about each resource being deleted,
    including the name/identifier of each individual resource as it's processed.
    """
    # Initialize logger
    init_logger(debug)

    # Validate NetBox connection settings
    validate_netbox_connection()

    # Confirm deletion unless force flag is set
    if not force and not dryrun:
        confirm = typer.confirm(
            "⚠️  This will DELETE all managed resources from NetBox. Are you sure?",
            default=False,
        )
        if not confirm:
            logger.info("Purge cancelled by user")
            raise typer.Exit()

    try:
        # Initialize NetBox API connection
        netbox_api = pynetbox.api(settings.URL, token=settings.TOKEN)
        if settings.IGNORE_SSL_ERRORS:
            netbox_api.http_session.verify = False

        logger.info("Starting NetBox purge operation...")

        # Define deletion order (reverse of creation order)
        # Later items depend on earlier items, so delete in reverse
        deletion_order = [
            # Network connections and assignments (most dependent)
            ("ipam.ip_addresses", "IP addresses"),
            ("ipam.fhrp_group_assignments", "FHRP group assignments"),
            ("dcim.cables", "cables"),
            ("dcim.mac_addresses", "MAC addresses"),
            # Device components
            ("dcim.interfaces", "interfaces"),
            ("dcim.console_server_ports", "console server ports"),
            ("dcim.console_ports", "console ports"),
            ("dcim.power_outlets", "power outlets"),
            ("dcim.power_ports", "power ports"),
            ("dcim.device_bays", "device bays"),
            ("dcim.inventory_items", "inventory items"),
            # Devices and infrastructure
            ("dcim.devices", "devices"),
            ("dcim.virtual_chassis", "virtual chassis"),
            ("dcim.device_types", "device types"),
            ("dcim.module_types", "module types"),
            # Virtualization resources (clusters depend on cluster types)
            ("virtualization.clusters", "clusters"),
            ("virtualization.cluster_types", "cluster types"),
            # Network resources
            ("ipam.fhrp_groups", "FHRP groups"),
            ("ipam.prefixes", "prefixes"),
            ("ipam.vlans", "VLANs"),
            ("ipam.vlan_groups", "VLAN groups"),
            ("ipam.vrfs", "VRFs"),
            # Infrastructure (if not excluded)
            ("dcim.racks", "racks"),
            ("dcim.locations", "locations"),
            ("dcim.sites", "sites"),
            ("organization.tenants", "tenants"),
            # Config contexts
            ("extras.config_contexts", "config contexts"),
            # Manufacturers (should be last of the device-related items)
            ("dcim.manufacturers", "manufacturers"),
        ]

        # Filter deletion order if limit is specified
        if limit:
            normalized_limit = limit.replace("-", "_").replace(".", "_")
            deletion_order = [
                (api_path, name)
                for api_path, name in deletion_order
                if normalized_limit in api_path.replace(".", "_")
            ]
            if not deletion_order:
                logger.error(f"No resource type matching '{limit}' found")
                raise typer.Exit(1)

        # Apply exclude_core filter
        if exclude_core:
            core_resources = ["sites", "locations", "tenants", "racks"]
            deletion_order = [
                (api_path, name)
                for api_path, name in deletion_order
                if not any(core in name for core in core_resources)
            ]

        total_deleted = 0
        errors = []

        for api_path, resource_name in deletion_order:
            try:
                # Navigate to the API endpoint
                api_parts = api_path.split(".")
                endpoint = netbox_api
                for part in api_parts:
                    endpoint = getattr(endpoint, part)

                resources = list(endpoint.all())

                if not resources:
                    if verbose:
                        logger.info(f"No {resource_name} found to delete")
                    else:
                        logger.debug(f"No {resource_name} found to delete")
                    continue

                if dryrun:
                    logger.info(f"Would delete {len(resources)} {resource_name}")
                    # Show detailed list when verbose flag is used
                    if verbose:
                        for resource in resources:
                            name_attr = get_resource_name(resource)
                            logger.info(f"  Would delete {resource_name}: {name_attr}")
                    else:
                        # Show only first 5 items in non-verbose mode
                        for resource in resources[:5]:
                            name_attr = get_resource_name(resource)
                            logger.debug(f"  - {name_attr}")
                        if len(resources) > 5:
                            logger.debug(f"  ... and {len(resources) - 5} more")
                    continue

                # Show start of deletion process for this resource type
                if verbose:
                    logger.info(f"Deleting {len(resources)} {resource_name}...")

                # Delete resources
                deleted_count = 0

                # Skip deletion of users and tokens
                if api_path in ["users.users", "users.tokens", "auth.tokens"]:
                    continue

                # Function to delete a single resource
                def delete_resource(resource):
                    try:
                        # Get resource identifier for verbose output
                        name_attr = get_resource_name(resource)

                        if verbose:
                            logger.info(f"  Deleting {resource_name}: {name_attr}")

                        resource.delete()
                        return True, None
                    except Exception as e:
                        name_attr = get_resource_name(resource)
                        error_msg = (
                            f"Failed to delete {resource_name} '{name_attr}': {e}"
                        )
                        if verbose:
                            logger.warning(error_msg)
                        else:
                            logger.debug(error_msg)
                        return False, f"{resource_name} '{name_attr}': {e}"

                # Use ThreadPoolExecutor for parallel deletion
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=parallel
                ) as executor:
                    futures = [
                        executor.submit(delete_resource, resource)
                        for resource in resources
                    ]

                    for future in concurrent.futures.as_completed(futures):
                        success, error = future.result()
                        if success:
                            deleted_count += 1
                        elif error:
                            errors.append(error)

                if deleted_count > 0:
                    logger.info(f"Deleted {deleted_count} {resource_name}")
                    total_deleted += deleted_count

            except AttributeError:
                logger.debug(f"API endpoint {api_path} not found, skipping")
            except Exception as e:
                logger.error(f"Error processing {resource_name}: {e}")
                errors.append(f"{resource_name}: {e}")

        # Summary
        if dryrun:
            logger.info("Dry run complete - no resources were deleted")
        else:
            logger.info(f"Purge complete - deleted {total_deleted} resources")

        if errors:
            logger.warning(f"Encountered {len(errors)} errors during deletion:")
            for error in errors[:10]:  # Show first 10 errors
                logger.warning(f"  - {error}")
            if len(errors) > 10:
                logger.warning(f"  ... and {len(errors) - 10} more errors")

    except pynetbox.RequestError as e:
        logger.error(f"NetBox API error: {e}")
        raise typer.Exit(1)
    except Exception as e:
        logger.error(f"Error during purge: {e}")
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
