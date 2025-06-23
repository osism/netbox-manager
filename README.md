# netbox-manager

A Python CLI tool that automates NetBox infrastructure management through
YAML-based configuration files. It streamlines the process of importing
device types, module types, and managing NetBox resources using Ansible
playbooks, making it easy to define and deploy complex network infrastructures
with consistent configuration management.

## Installation

```
$ pipenv shell
$ pip install netbox-manager
$ ansible-galaxy collection install -r requirements.yml
```

## Configuration

```toml
DEVICETYPE_LIBRARY = "example/devicetypes"
IGNORE_SSL_ERRORS = true
MODULETYPE_LIBRARY = "example/moduletypes"
RESOURCES = "example/resources"
TOKEN = ""
URL = "https://XXX.netbox.regio.digital"
VARS = "example/vars"
VERBOSE = true
```

## Usage

```
$ pipenv shell
$ netbox-manager --help

 Usage: netbox-manager [OPTIONS]

╭─ Options ───────────────────────────────────────────────────────────────────────────────────────────╮
│ --limit                      TEXT  Limit files by prefix [default: None]                            │
│ --skipdtl    --no-skipdtl          Skip devicetype library [default: no-skipdtl]                    │
│ --skipmtl    --no-skipmtl          Skip moduletype library [default: no-skipmtl]                    │
│ --skipres    --no-skipres          Skip resources [default: no-skipres]                             │
│ --wait       --no-wait             Wait for NetBox service [default: wait]                          │
│ --help                             Show this message and exit.                                      │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### Limit Functionality

The `--limit` option allows you to process only specific resources by their numeric prefix. The limit is applied at the top level (files and directories in the resources directory) but not to files within directories.

**Examples:**

```bash
# Process only resources starting with "300"
netbox-manager --limit 300

# Process only resources starting with "200"
netbox-manager --limit 200
```

**Behavior:**

- **Files**: `--limit 300` processes `300-devices.yml` but skips `200-racks.yml`
- **Directories**: `--limit 300` processes the entire `300-devices/` directory
- **Directory contents**: ALL files within `300-devices/` are processed, regardless of their names
- **Mixed resources**: Both `300-devices.yml` and `300-devices/` would be processed together

**Directory Structure Example:**
```
resources/
├── 200-racks.yml              # Skipped with --limit 300
├── 300-devices.yml            # Processed with --limit 300
├── 300-networks/              # Processed with --limit 300
│   ├── cables.yml            # Processed (all files in directory)
│   ├── interfaces.yml        # Processed (all files in directory)
│   └── vlans.yml             # Processed (all files in directory)
└── 400-maintenance.yml        # Skipped with --limit 300
```

This approach allows you to:
- Test specific phases of your deployment (`--limit 100` for infrastructure only)
- Rerun specific resource groups after changes
- Organize complex configurations into directories without losing limit functionality

## Variables

netbox-manager supports both local and global variables for flexible configuration management.

### Local Variables

Variables can be defined within individual resource files using the `vars` section:

```yaml
- vars:
    site: Discworld
    location: Ankh-Morpork
    rack: "1000"
    tenant: Testbed

- device:
    name: "{{ tenant }}-switch-1"
    site: "{{ site }}"
    location: "{{ location }}"
    rack: "{{ rack }}"
```

### Global Variables

Global variables are defined in YAML files within the `VARS` directory (configured in settings.toml). These variables are automatically loaded and available in all resource files.

**Global Variable Files (`example/vars/`):**

```yaml
# 000-global.yml
site: Discworld
location: Ankh-Morpork
rack: "1000"
tenant: Testbed

networks:
  oob:
    vlan: 100
    subnet: "172.16.0.0/20"
  management:
    vlan: 200
    subnet: "192.168.16.0/20"
```

```yaml
# 100-overrides.yml - loaded after 000-global.yml
networks:
  oob:
    description: "Out-of-band management network"

environment: production
```

**Variable Precedence:**
1. Global variables are loaded first (sorted by filename)
2. Later global variable files can override earlier ones
3. Local `vars` sections in resource files override global variables
4. Deep merging preserves nested structures

**Usage in Resource Files:**
```yaml
# No local vars needed - uses global variables
- device:
    name: "{{ tenant }}-node-1"
    site: "{{ site }}"
    rack: "{{ rack }}"

# Local vars override global vars
- vars:
    rack: "2000"  # Overrides global rack setting

- device:
    name: "{{ tenant }}-node-2"
    site: "{{ site }}"        # Still uses global site
    rack: "{{ rack }}"        # Uses local override
```

## Example Configuration

The `example/` directory contains a complete example configuration for a testbed setup with multiple network switches and compute nodes. This configuration demonstrates all key features of netbox-manager.

### Directory Structure

```
example/
├── devicetypes/           # Device type definitions
│   ├── Edgecore/         # Edgecore switch models
│   │   ├── 5835-54X-O-AC-F.yaml
│   │   └── 7726-32X-O-AC-F.yaml
│   └── Other/            # Generic device types
│       ├── baremetal-device.yml
│       ├── baremetal-housing.yml
│       ├── manager.yml
│       └── node.yml
├── moduletypes/          # Module definitions (empty)
├── vars/                 # Global variable files
│   ├── 000-global.yml   # Base global variables
│   └── 100-overrides.yml # Environment-specific overrides
└── resources/            # Numbered resource files and directories
    ├── 100-initialise.yml          # Base infrastructure
    ├── 200-rack-1000.yml          # Rack and device definitions
    ├── 300-testbed-manager.yml    # Manager configuration
    ├── 300-testbed-node-*.yml     # Node configurations (0-9)
    ├── 300-testbed-switch-*.yml   # Switch configurations (0-3)
    ├── 300-testbed-switch-oob.yml # Out-of-band switch
    └── 400-networks/               # Directory with network configurations
        ├── cables.yml
        ├── interfaces.yml
        └── vlans.yml
```

### Execution Order

Files and directories are processed by their numeric prefix. Files within numbered directories are processed together in the same execution group:

1. **100-initialise.yml**: Creates base infrastructure
   - Tenant: `Testbed`
   - Site: `Discworld`
   - Location: `Ankh-Morpork`
   - VLANs: OOB Testbed (VLAN 100)
   - IP ranges: OOB (172.16.0.0/20), Management (192.168.16.0/20), External (192.168.112.0/20)
   - IPv6 range: fda6:f659:8c2b::/48

2. **200-rack-1000.yml**: Defines physical infrastructure
   - Rack "1000" with 47 rack units
   - 11 devices (1 Manager, 10 Nodes, 5 Switches) with exact rack positions

3. **300-*.yml**: Detailed configuration for individual devices
   - Network interfaces and VLAN assignments
   - IP addresses and MAC addresses
   - Cable connections between devices

4. **400-networks/**: Directory containing all network configuration files
   - All YAML files within this directory are processed together
   - Files are sorted alphabetically within the directory (cables.yml, interfaces.yml, vlans.yml)

### Resource Organization

netbox-manager supports two organizational approaches:

**File-based (traditional):**
```
resources/
├── 100-sites.yml
├── 200-racks.yml
├── 300-devices.yml
└── 400-networks.yml
```

**Directory-based (new):**
```
resources/
├── 100-infrastructure/
│   ├── sites.yml
│   └── tenants.yml
├── 200-physical/
│   ├── locations.yml
│   └── racks.yml
└── 300-devices/
    ├── managers.yml
    ├── nodes.yml
    └── switches.yml
```

**Mixed approach:**
```
resources/
├── 100-sites.yml          # Single file
├── 200-physical/          # Directory with multiple files
│   ├── locations.yml
│   └── racks.yml
└── 300-devices.yml        # Back to single file
```

### Device Types

**Manager (manager.yml)**
- 1U server with management function
- 1x 1000Base-T (management), 2x 10GBase-T, 2x 100G QSFP28

**Node (node.yml)**
- 1U server for Control/Compute/Storage roles
- Identical configuration to Manager

**Edgecore Switches**
- 7726-32X: 32-port 100G switch (Leaf/Spine)
- 5835-54X: 54-port switch for out-of-band management

### Network Architecture

The example implements a typical leaf-spine architecture:

- **2x Leaf Switches** (testbed-switch-0/1): Connection to compute nodes
- **2x Spine Switches** (testbed-switch-2/3): Interconnect between leafs
- **1x OOB Switch** (testbed-switch-oob): Out-of-band management
- **1x Manager**: Orchestration and deployment
- **10x Nodes**: OpenStack Control/Compute/Storage nodes

### IP Addressing

- **OOB Network**: 172.16.0.0/20 (Out-of-band management)
- **Management**: 192.168.16.0/20 (OpenStack management)
- **External**: 192.168.112.0/20 (Public API access)
- **IPv6**: fda6:f659:8c2b::/48 (Management IPv6)

## Documentation

* https://docs.ansible.com/ansible/latest/collections/netbox/netbox/index.html
* https://github.com/netbox-community/devicetype-library/tree/master/device-types
* https://github.com/netbox-community/devicetype-library/tree/master/module-types
