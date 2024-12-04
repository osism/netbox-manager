# netbox-manager

## Installation

```
$ pipenv shell
$ pip install netbox-manager
$ ansible-galaxy collection install -r requirements.yml
```

## Configuration

```toml
DEVICETYPE_LIBRARY = "example/devicetype-library"
IGNORE_SSL_ERRORS = true
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
│ --wait       --no-wait             Wait for NetBox service [default: wait]                          │
│ --skipdtl    --no-skipdtl          Skip device type library [default: no-skipdtl]                   │
│ --help                             Show this message and exit.                                      │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## Documentation

* https://docs.ansible.com/ansible/latest/collections/netbox/netbox/index.html
* https://github.com/netbox-community/devicetype-library
