# netbox-manager

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
└── resources/            # Numbered resource files
    ├── 100-initialise.yml          # Base infrastructure
    ├── 200-rack-1000.yml          # Rack and device definitions
    ├── 300-testbed-manager.yml    # Manager configuration
    ├── 300-testbed-node-*.yml     # Node configurations (0-9)
    ├── 300-testbed-switch-*.yml   # Switch configurations (0-3)
    └── 300-testbed-switch-oob.yml # Out-of-band switch
```

### Execution Order

Files are processed by their numeric prefix:

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
