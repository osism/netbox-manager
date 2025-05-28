# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

netbox-manager is a Python CLI tool that automates NetBox infrastructure management through YAML-based configuration files. It imports device types, module types, and manages NetBox resources using Ansible playbooks.

## Key Commands

```bash
# Development setup
pipenv shell
pip install -e .
ansible-galaxy collection install -r requirements.yml

# Run the application
netbox-manager --help
netbox-manager  # Process all resources
netbox-manager --limit 300  # Process only files starting with 300
netbox-manager --skipdtl  # Skip device type library
netbox-manager export --output netbox-config.tar.gz  # Export configuration

# Linting and type checking
flake8  # Uses .flake8 config (max line length: 200)
mypy netbox_manager/  # Type checking (excludes doc/)

# Update example files from upstream
gilt overlay  # Updates example/ from osism/testbed repository
```

## Architecture

### Core Components
- `netbox_manager/main.py`: CLI application using Typer framework
- `netbox_manager/dtl.py`: Device Type Library import logic (adapted from netbox-community)
- Configuration: Uses dynaconf with `settings.toml` or environment variables

### Key Dependencies
- `pynetbox`: NetBox API client
- `ansible-runner`: Executes Ansible playbooks for resource management
- `GitPython`: Git integration for processing changed files
- `dynaconf`: Configuration management

### Resource Processing Flow
1. Device/Module Types: Imports manufacturer and type definitions from YAML
2. Resources: Processes numbered YAML files in order (e.g., 100-*, 200-*, 300-*)
3. Parallel execution: Groups files by leading number for dependency management

### Configuration Structure
- `settings.toml`: NetBox URL, token, and directory paths
- `example/devicetypes/`: Device type YAML definitions
- `example/moduletypes/`: Module type YAML definitions  
- `example/resources/`: Numbered YAML files with NetBox resource definitions

## Important Patterns

### Resource File Naming
Files in the resources directory must be numbered for ordered execution:
- `100-initialise.yml`: Base infrastructure (sites, tenants)
- `200-*.yml`: Racks and locations
- `300-*.yml`: Devices and connections

### YAML Resource Format

Resources use Ansible's netbox.netbox collection format. Each resource type corresponds to a NetBox object:

#### Basic Structure
```yaml
# Variables definition
- vars:
    site: Discworld
    location: Ankh-Morpork
    rack: "1000"
    tenant: Testbed

# Resource definition
- resource_type:
    name: resource_name
    parameter: value
    parameter_with_var: "{{ variable_name }}"
```

#### Common Resource Types

**Infrastructure Resources:**
```yaml
- tenant:
    name: Testbed
    tags:
      - managed-by-osism

- site:
    name: Discworld
    tenant: Testbed

- location:
    name: Ankh-Morpork
    site: Discworld

- rack:
    name: "1000"
    site: Discworld
    location: Ankh-Morpork
```

**Network Resources:**
```yaml
- vlan:
    name: OOB Testbed
    tenant: Testbed
    vid: 100
    site: Discworld
    vlan_role: OOB

- prefix:
    tenant: Testbed
    prefix: 192.168.16.0/20
    vlan:
      name: Management Testbed
```

**Device Resources:**
```yaml
- device:
    name: testbed-node-0
    tenant: "{{ tenant }}"
    site: "{{ site }}"
    location: "{{ location }}"
    rack: "{{ rack }}"
    device_type: Node  # Must match imported device type
    device_role: Control
    face: front
    position: 10
    oob_ip: 172.16.0.10/20  # Sets out-of-band IP
    primary_ip4: 192.168.16.10/32
    primary_ip6: "fda6:f659:8c2b::192:168:16:10/128"
    tags:
      - managed-by-osism
```

**Interface Configuration:**
```yaml
- device_interface:
    device: testbed-manager
    name: Ethernet0
    type: 1000base-t
    label: oob-switch
    mode: access
    untagged_vlan:
      name: OOB Testbed

- device_interface:
    device: testbed-switch-0
    name: Ethernet1
    mode: tagged
    tagged_vlans:
      - name: Management Testbed
      - name: External Testbed
```

**Cable Connections:**
```yaml
- cable:
    type: cat6a  # or mmf-om4 for fiber
    termination_a:
      device: testbed-switch-oob
      name: Ethernet1
    termination_b:
      device: testbed-node-0
      name: Ethernet0
```

**IP Address Assignment:**
```yaml
- ip_address:
    tenant: Testbed
    address: 192.168.16.10/32
    assigned_object:
      name: Loopback0  # Interface name
      device: testbed-node-0

- mac_address:
    address: "18:C0:86:D4:E2:F7"
    assigned_object:
      name: Ethernet0
      device: testbed-node-0
```

### Device Type Format
Follows NetBox community device type library standards with manufacturer, model, and component templates.

## Development Notes

- Python 3.8+ required
- Uses setuptools with git-based versioning
- Entry point: `netbox-manager` command
- No test suite currently implemented
- Gilt pulls example files from osism/testbed repository
