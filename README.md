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

## Documentation

* https://docs.ansible.com/ansible/latest/collections/netbox/netbox/index.html
* https://github.com/netbox-community/devicetype-library/tree/master/device-types
* https://github.com/netbox-community/devicetype-library/tree/master/module-types
