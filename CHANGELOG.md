# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.20260721.0] - 2026-07-21

### Dependencies
- ansible-core 2.19.3 → 2.19.11 (osism/netbox-manager#283)
- gitpython 3.1.50 → 3.1.52 (osism/netbox-manager#282, osism/netbox-manager#284)
- setuptools 82.0.1 → 83.0.0 (osism/netbox-manager#281)
- typer 0.26.8 → 0.27.0 (osism/netbox-manager#285)

## [v0.20260706.0] - 2026-07-06

### Added
- Add Tier 1 unit-test suite for the pure-logic helpers in `netbox_manager/main.py` — settings/role helpers, generic data-transformation helpers, task-filter helpers, autoconf helpers, and loopback-gate helpers — with shared device/interface conftest factories (osism/netbox-manager#248)
- End-to-end test harness (`make e2e`) that provisions NetBox on a local kind cluster via the pinned netbox-chart (8.2.5, NetBox v4.5.10), applies the bundled `example/` data with `netbox-manager run`, and verifies the result through the NetBox REST API; runs as the `netbox-manager-e2e` Zuul job in both the PR check gate and the periodic-daily pipeline (osism/netbox-manager#263)
- Base NetBox resources (IPAM roles, device roles, tags, and custom fields) referenced by the example data (osism/netbox-manager#263)
- Unit tests for the NetBox and URI task builders (osism/netbox-manager#251)
- Unit tests for playbook rendering (`create_ansible_playbook`) and the YAML dumper (`ProperIndentDumper`), plus a hermetic dynaconf environment baseline in the test suite (osism/netbox-manager#251)
- Unit tests for `handle_file` orchestration, with shared loguru/caplog and `ansible_runner` recorder fixtures for the test suite (osism/netbox-manager#266)
- Unit tests for the YAML file discovery, global vars loading, resource/site-folder discovery, and autoconf file writing helpers (osism/netbox-manager#269)
- Add unit tests and shared fixtures for the Device Type Library (DTL) importer, covering Repo file walking, the NetBox API wrapper, and DeviceTypes component creators (osism/netbox-manager#270)
- Add unit test coverage for Loopback0 interface generation, cluster loopback IP calculation, device interface label generation, and PortChannel task generation, including shared pynetbox mock fixtures (osism/netbox-manager#271)
- Extend shared test factories with autoconf pynetbox shapes for upcoming autoconf collector tests (osism/netbox-manager#272)
- Add unit test coverage for the autoconf collectors (MAC and IP assignment collection) and the autoconf task dispatcher (osism/netbox-manager#272)
- Add unit test coverage for the IP-prefix and VRF-consistency NetBox validation helpers, with shared VRF/IP-address test fixtures (osism/netbox-manager#273)
- Add shared CLI runner and worker-spy fixtures for upcoming command-wiring tests (osism/netbox-manager#275)
- Add smoke tests for CLI command wiring covering `run`, `autoconf`, `validate`, `purge`, and entrypoint/utility commands (osism/netbox-manager#275)
- Add unit tests for archive export/import and NetBox connection helpers, plus shared git/subprocess/platform mock fixtures (osism/netbox-manager#274)

### Changed
- Consolidate the portchannel task filter into the generic device-filter helper, removing a byte-identical duplicate function (osism/netbox-manager#248)
- Untagged builds now carry a distinguishing dev version (`{tag}.post{ccount}+git.{sha}`) instead of collapsing to the bare release tag (osism/netbox-manager#263)
- Use the shared `discover_resource_files` helper in `_run_main` to remove duplicated resource-file discovery logic (osism/netbox-manager#269)

### Fixed
- Fix project board automation not running for fork pull requests by switching to `pull_request_target` and scoping the shared secret (osism/netbox-manager#264)
- Setting defaults (e.g. `VERBOSE`, `IGNORE_SSL_ERRORS`) are now applied correctly when netbox-manager is configured purely through environment variables (osism/netbox-manager#263)
- Fix rendered playbook temp files leaking into the system temp directory instead of being cleaned up after each resource file is processed (osism/netbox-manager#267)
- Fix `UnboundLocalError` when creating a module type fails and the entry has component keys, by skipping component dispatch after a failed create (osism/netbox-manager#270)
- Fix file descriptor leak in device type image uploads by closing image handles after the PATCH request completes (osism/netbox-manager#270)
- Fix IP-prefix validation matching against the network base instead of the host IP, which could wrongly pass an orphaned IP address as having a matching prefix (osism/netbox-manager#273)
- Guard tar extraction in `import-archive` against path traversal and decompression-bomb attacks (osism/netbox-manager#274)
- Exit with a non-zero status when NetBox connection settings fail validation, so callers checking the exit code no longer treat a misconfigured URL or token as success (osism/netbox-manager#274)
- Fix PortChannel name collisions when a switch shares multiple pairs deriving the same number, and preserve existing PortChannel numbers across runs so removing one channel no longer renames a surviving one (osism/netbox-manager#276)

### Dependencies
- pynetbox 7.7.0 → 7.8.0 (osism/netbox-manager#260)
- pytest 9.1.0 → 9.1.1 (osism/netbox-manager#261)
- dynaconf 3.2.13 → 3.3.1 (osism/netbox-manager#265)
- ansible_modules devel → v3.23.0 (osism/netbox-manager#263)
- dynaconf 3.3.1 → 3.3.2 (osism/netbox-manager#268)
- sushy 5.11.0 → 5.11.1 (osism/netbox-manager#277)
- typing_extensions 4.15.0 → 4.16.0 (osism/netbox-manager#278)
- typer 0.26.7 → 0.26.8 (osism/netbox-manager#279)

## [v0.20260614.0] - 2026-06-14

### Added
- Add pytest foundation and Zuul unit-test job for incremental per-module unit testing (osism/netbox-manager#234)
- Automatically add opened issues and PRs to project board (osism/netbox-manager#236)
- Add segment-level fallback for device interface labels via the `_segment_device_interface_label` config context key when the per-device custom field is not set (osism/netbox-manager#246)

### Changed
- Reformat code to comply with black 26.3.1 stable style (osism/netbox-manager#226)

### Fixed
- Pin pipenv version in CI via ensure-pipenv role to fix stale Pipfile.lock verification (osism/netbox-manager#235)
- Disambiguate duplicate device interface labels on multi-homed nodes so repeated links to the same switch no longer produce identical labels (osism/netbox-manager#245)

### Dependencies
- gitpython 3.1.46 → 3.1.50 (osism/netbox-manager#227, osism/netbox-manager#231, osism/netbox-manager#239)
- typer 0.24.1 → 0.26.7 (osism/netbox-manager#228, osism/netbox-manager#229, osism/netbox-manager#240, osism/netbox-manager#241, osism/netbox-manager#242, osism/netbox-manager#244)
- sushy 5.10.0 → 5.11.0 (osism/netbox-manager#237)
- pynetbox 7.6.1 → 7.7.0 (osism/netbox-manager#238)
- pytest 9.0.3 → 9.1.0 (osism/netbox-manager#250)

## [v0.20260322.0] - 2026-03-22

### Added
- Configurable `_segment_loopback_network_multiplicator` parameter allowing segments to override the default multiplicator in loopback IP address calculation (osism/netbox-manager#222)

### Changed
- Update CHANGELOG.md (osism/netbox-manager#221)

### Dependencies
- ansible-runner 2.4.2 → 2.4.3 (osism/netbox-manager#218)
- dynaconf 3.2.12 → 3.2.13 (osism/netbox-manager#225)

## [v0.20260310.0] - 2026-03-10

### Added
- Configurable device roles (`NODE_ROLES`, `SWITCH_ROLES`) via `settings.toml` with fallback to upstream defaults (osism/netbox-manager#216)
- Auto-detection of numbered site folders for per-site autoconf output using x99 prefix (osism/netbox-manager#216)

### Changed
- Autoconf output consolidated under a single prefix split by resource type instead of separate output files (osism/netbox-manager#216)

### Dependencies
- typer 0.21.2 → 0.24.1 (osism/netbox-manager#212, osism/netbox-manager#213, osism/netbox-manager#214)
- sushy 5.9.0 → 5.10.0 (osism/netbox-manager#215)
- setuptools 82.0.0 → 82.0.1 (osism/netbox-manager#217)

## [v0.20260211.0] - 2026-02-11

### Added
- Dependency typing_extensions 4.15.0 (osism/netbox-manager#211)

### Dependencies
- python-gilt 2.2.4 → 2.2.5 (osism/netbox-manager#207)
- setuptools 80.10.2 → 82.0.0 (osism/netbox-manager#209)
- typer 0.21.1 → 0.21.2 (osism/netbox-manager#210)

## [v0.20260129.0] - 2026-01-29

### Dependencies
- setuptools 80.10.1 → 80.10.2 (osism/netbox-manager#204)
- pynetbox 7.6.0 → 7.6.1 (osism/netbox-manager#206)

## [v0.20260123.0] - 2026-01-23

### Added
- Export archive now includes COMMIT_INFO.txt with git commit hash, date, branch, and message information (osism/netbox-manager#195)

### Changed
- Use e2 tools instead of loop devices for export-archive image creation, removing the need for sudo and mount operations (osism/netbox-manager#202)

### Dependencies
- gitpython 3.1.45 → 3.1.46 (osism/netbox-manager#200)
- pynetbox 7.5.0 → 7.6.0 (osism/netbox-manager#201)
- setuptools 80.9.0 → 80.10.1 (osism/netbox-manager#203)
- sushy 5.8.0 → 5.9.0 (osism/netbox-manager#197)
- typer 0.20.0 → 0.21.1 (osism/netbox-manager#198, osism/netbox-manager#199)
- yamale 6.0.0 → 6.1.0 (osism/netbox-manager#194)

## [v0.20251120.0] - 2025-11-20

### Added
- frr_local_pref custom field support to autoconf device interface labeling (osism/netbox-manager#193)
- Python 3.14 classifier (osism/netbox-manager#192)

### Dependencies
- sushy 5.7.1 → 5.8.0 (osism/netbox-manager#191)

## [v0.20251029.0] - 2025-10-29

### Added
- `--ignore-errors` option to the `run` command, allowing Ansible tasks to continue execution even when individual tasks fail (osism/netbox-manager#188)
- `validate` command for NetBox configuration consistency checks, including IP-prefix validation and VRF consistency verification (osism/netbox-manager#189)

## [v0.20251028.0] - 2025-10-28

### Added
- YAML document start marker (`---`) to autoconf-generated files (osism/netbox-manager#186)

### Fixed
- Stable task ordering in autoconf generation to prevent unnecessary Git diffs (osism/netbox-manager#185)
- YAML indentation for nested lists in autoconf output to comply with yamllint rules (osism/netbox-manager#187)

## [v0.20251025.3] - 2025-10-25

### Fixed
- PortChannel naming now uses per-switch interface numbers instead of deriving from only the first switch's interfaces (#184)

## [v0.20251025.2] - 2025-10-25

### Added
- PortChannel autoconf generation for switch-to-switch connections with automatic LAG interface creation and member assignment (osism/netbox-manager#183)

## [v0.20251025.1] - 2025-10-25

### Fixed
- MAC address extraction in autoconf now includes switches, making it consistent with OOB IP assignment (osism/netbox-manager#182)

## [v0.20251025.0] - 2025-10-25

### Fixed
- MAC address extraction in autoconf interface assignments now outputs clean strings instead of serialized Python objects (osism/netbox-manager#181)

## [v0.20251024.0] - 2025-10-24

### Changed
- Include switches in autoconf device IP assignments while keeping them excluded from interface MAC address assignments (osism/netbox-manager#180)

## [v0.20251023.0] - 2025-10-23

### Fixed
- Cluster loopback IP addresses are now only generated for devices that meet Loopback0 interface criteria (osism/netbox-manager#179)

### Dependencies
- ansible-core 2.19.2 → 2.19.3 (osism/netbox-manager#175)
- ansible-runner 2.4.1 → 2.4.2 (osism/netbox-manager#177)
- dynaconf 3.2.11 → 3.2.12 (osism/netbox-manager#176)
- pyyaml 6.0.2 → 6.0.3 (osism/netbox-manager#174)
- typer 0.17.4 → 0.20.0 (osism/netbox-manager#172, osism/netbox-manager#173, osism/netbox-manager#178)

## [v0.20250915.0] - 2025-09-15

### Dependencies
- ansible-core 2.19.0 → 2.19.2 (osism/netbox-manager#168, osism/netbox-manager#171)
- typer 0.16.1 → 0.17.4 (osism/netbox-manager#169, osism/netbox-manager#170)

## [v0.20250825.0] - 2025-08-25

### Fixed
- Support both string and integer TOKEN values for NetBox API configuration (osism/netbox-manager#167)

## [v0.20250823.0] - 2025-08-23

### Changed
- Rename segment parameters from `_loopback_network_*` and `_loopback_offset_*` to `_segment_loopback_network_*` and `_segment_loopback_offset_*` (osism/netbox-manager#166)

## [v0.20250822.0] - 2025-08-22

### Added
- Automatic managed-by-osism tag for device interfaces that receive labels through autoconf device interface labeling (osism/netbox-manager#164)

### Changed
- Device interface labeling extended to include routers and firewalls in addition to switches (osism/netbox-manager#165)

### Dependencies
- sushy 5.7.0 → 5.7.1 (osism/netbox-manager#163)
- typer 0.16.0 → 0.16.1 (osism/netbox-manager#162)

## [v0.20250811.2] - 2025-08-11

### Added
- Support for direct NetBox API calls via `uri` task type with `path`, `method`, and `body` parameters (osism/netbox-manager#160)
- YAML syntax validation with meaningful error messages including line/column information (osism/netbox-manager#161)

### Fixed
- `virtual_chassis` module placement in purge command moved from `virtualization` to `dcim` where it belongs in NetBox's API structure (osism/netbox-manager#159)

## [v0.20250811.1] - 2025-08-11

### Added
- `--show-playbooks` option to run command for previewing generated Ansible playbooks without executing them (osism/netbox-manager#151)
- Support for `update_vc_child` parameter in device_interface tasks (osism/netbox-manager#152)
- `--verbose` option to run command for detailed ansible-playbook output with `-vvv` (osism/netbox-manager#153, osism/netbox-manager#154)
- YAML-formatted output for ansible-playbook using `ansible.builtin.default` callback with `result_format=yaml` (osism/netbox-manager#155, osism/netbox-manager#157)
- `--parallel` option to purge command for concurrent resource deletion (osism/netbox-manager#158)

## [v0.20250811.0] - 2025-08-11

### Added
- `--fail-fast` option to run command for early failure exit on first Ansible playbook failure (osism/netbox-manager#147)
- Virtual chassis to purge command resource list (osism/netbox-manager#148)
- Support for `register` field in YAML resource definitions, enabling Ansible's register functionality for storing task results (osism/netbox-manager#149)
- FHRP groups and FHRP group assignments to purge command resource list (osism/netbox-manager#150)

## [v0.20250806.0] - 2025-08-06

### Added
- Purge command to delete all managed NetBox resources with dry-run mode, resource type filtering, core resource exclusion, and confirmation prompt (osism/netbox-manager#136)
- `--verbose` flag to purge command for detailed real-time deletion output (osism/netbox-manager#140)
- Device interface label autoconf based on switch `device_interface_label` custom field (osism/netbox-manager#143)
- Automatic splitting of autoconf YAML files by resource type for better maintainability (osism/netbox-manager#145)

### Changed
- Purge command deletes all resources instead of filtering by `managed-by-osism` tag (osism/netbox-manager#138)
- Purge command also deletes clusters and cluster types (osism/netbox-manager#139)
- Device interface labeling uses `connected_endpoints` instead of cable-based discovery (osism/netbox-manager#144)
- Refactor main.py to reduce code duplication and improve modularity by extracting common functionality into reusable helper functions (osism/netbox-manager#146)

### Fixed
- Use `dcim.interfaces` instead of `dcim.device_interfaces` for correct NetBox API endpoint in purge command (osism/netbox-manager#137)

## [v0.20250804.0] - 2025-08-04

### Fixed
- AttributeError when VARS is not configured in settings by using getattr() with None default (osism/netbox-manager#135)

## [v0.20250803.2] - 2025-08-03

No changes from v0.20250803.0.

## [v0.20250803.0] - 2025-08-03

### Added
- Cluster-based loopback IP generation in autoconf command, supporting IPv4/IPv6 address assignment for devices with assigned clusters (osism/netbox-manager#131)

### Changed
- Always create Loopback0 device interface tasks for eligible devices regardless of whether they already exist in NetBox (osism/netbox-manager#132)
- Use separate API call for cluster config context retrieval instead of direct cluster attribute access (osism/netbox-manager#133)
- Add integer validation for device positions in cluster loopback generation with descriptive warnings for missing or invalid positions (osism/netbox-manager#134)

## [v0.20250802.2] - 2025-08-02

### Added
- Loopback0 interface generation for eligible devices in autoconf command, output to 299-autoconf.yml (osism/netbox-manager#128)
- hwsku validation requiring sonic_parameters custom field for switch Loopback0 interface creation (osism/netbox-manager#130)

### Changed
- Consolidate OOB IP, primary IPv4, and primary IPv6 assignments into single device tasks in autoconf (osism/netbox-manager#127)
- Always generate IP assignment tasks in autoconf regardless of current device state (osism/netbox-manager#125)
- Elevate autoconf assignment logging from debug to info level for better visibility (osism/netbox-manager#126)
- Include switch roles (accessleaf, borderleaf, leaf, spine, etc.) in loopback interface generation (osism/netbox-manager#129)
- Move NETBOX_NODE_ROLES and NETBOX_SWITCH_ROLES constants to global scope to eliminate duplication (osism/netbox-manager#129)

### Fixed
- MAC address handling to use interface.mac_address and interface.mac_addresses fields instead of API filter (osism/netbox-manager#124)
- Device role references to use device.role instead of deprecated device.device_role (osism/netbox-manager#124)

## [v0.20250802.1] - 2025-08-02

### Changed
- Exclude switch devices from autoconf interface analysis, improving performance by avoiding unnecessary switch interface processing (osism/netbox-manager#123)

## [v0.20250802.0] - 2025-08-02

### Added
- Global variables support with `VARS` configuration setting and deep merging, allowing shared variables across resource files with local override precedence (osism/netbox-manager#114)
- Directory support for numbered resource organization, enabling `300-devices/` directories alongside `300-devices.yml` files (osism/netbox-manager#115)
- `--filter-task` option replacing `--task-filter` for consistency, with support for both underscore and hyphen variations (osism/netbox-manager#112)
- `autoconf` command for automated NetBox configuration generation, handling MAC address, OOB IP, primary IPv4/IPv6 assignments (osism/netbox-manager#121)

### Changed
- `--limit` filter is now applied at the top-level file/directory level rather than to individual files within directories (osism/netbox-manager#115)

### Dependencies
- ansible-core 2.18.6 → 2.19.0 (osism/netbox-manager#117, osism/netbox-manager#118)
- GitPython 3.1.44 → 3.1.45 (osism/netbox-manager#119)
- sushy 5.6.0 → 5.7.0 (osism/netbox-manager#116)

## [v0.20250622.1] - 2025-06-22

### Fixed
- Do not fail when `settings.IGNORED_FILES` is not defined (osism/netbox-manager#111)

## [v0.20250622.0] - 2025-06-22

### Added
- Configuration to ignore specific resource files with `IGNORED_FILES` setting and `--include-ignored-files` CLI parameter to override (osism/netbox-manager#109)
- Device filtering with `--filter-device` CLI parameter supporting nested YAML structures like cable terminations (osism/netbox-manager#110)
- Example configuration documentation to README (osism/netbox-manager#108)

### Changed
- Sync example device types and resources: replace Edgecore 5835-54T with 5835-54X, rename 7726-32X-O-AC-B to 7726-32X-O-AC-F, add `managed-by-metalbox` tag and `sonic_parameters` custom fields to switches (osism/netbox-manager#107)

## [v0.20250621.0] - 2025-06-21

### Added
- `version` command to display the application version (osism/netbox-manager#105)

### Fixed
- `.gitkeep` files and other non-directory entries no longer treated as manufacturers in device type library (osism/netbox-manager#106)

## [v0.20250529.1] - 2025-05-29

### Changed
- Rename `import-netbox` to `import-archive` and `export` to `export-archive` for naming consistency (osism/netbox-manager#103)
- Replace deprecated `pkg_resources` with `importlib.metadata` for version retrieval (osism/netbox-manager#104)

## [v0.20250529.0] - 2025-05-29

### Added
- Import command for syncing content from netbox-export.tar.gz archives to a configurable destination directory (osism/netbox-manager#101)
- `--task-filter` option to filter tasks by resource type in the run command (osism/netbox-manager#102)

### Changed
- Refactored logger initialization into reusable `init_logger()` function (osism/netbox-manager#101)

## [v0.20250528.1] - 2025-05-28

### Added
- Configurable `--image-size` parameter to export command for ext4 image creation (osism/netbox-manager#98)
- Platform check for ext4 image export to provide clear error on non-Linux systems (osism/netbox-manager#97)
- Example files from osism/testbed repository with Gilt overlay configuration (osism/netbox-manager#99)
- CLAUDE.md file for AI-assisted development guidance (osism/netbox-manager#100)

### Changed
- Export command now works without NetBox configuration since it only archives local directories (osism/netbox-manager#96)
- Updated example device types: replaced Cisco manufacturer with Other, added Edgecore 5835-54T-O-AC-F, renamed 7726-32X-O to 7726-32X-O-AC-B, added baremetal device types (osism/netbox-manager#99)
- Updated example resources with tenant support, additional VLANs, prefixes, and expanded testbed node definitions (osism/netbox-manager#99)

### Removed
- Base resource file `000-base.yml` with device roles and tags (osism/netbox-manager#99)
- Supermicro module type definitions (osism/netbox-manager#99)
- Edgecore 7326-56X-O-AC-B device type (osism/netbox-manager#99)

### Dependencies
- python-gilt 2.2.4 added as dev dependency (osism/netbox-manager#99)

## [v0.20250528.0] - 2025-05-28

### Added
- Option to create a 100MB ext4 image file containing the export tarball via `--image` flag on `export-archive` command (osism/netbox-manager#95)

### Dependencies
- setuptools 80.8.0 → 80.9.0 (osism/netbox-manager#94)
- typer 0.15.4 → 0.16.0 (osism/netbox-manager#93)

## [v0.20250526.0] - 2025-05-26

### Added
- Export command to archive NetBox configuration data (devicetypes, moduletypes, resources) as tar.gz (osism/netbox-manager#92)

### Changed
- Refactored CLI to use Typer app with subcommands while maintaining backward compatibility (osism/netbox-manager#92)

## [v0.20250525.0] - 2025-05-25

### Added
- sushy as a dependency for future Redfish MAC address retrieval (osism/netbox-manager#90)

### Changed
- Refreshed Zuul CI secrets (osism/netbox-manager#84)

### Dependencies
- setuptools 80.3.1 → 80.8.0 (osism/netbox-manager#83, osism/netbox-manager#86, osism/netbox-manager#89)
- typer 0.15.3 → 0.15.4 (osism/netbox-manager#85)
- ansible-core 2.18.5 → 2.18.6 (osism/netbox-manager#87)
- pynetbox 7.4.1 → 7.5.0 (osism/netbox-manager#88)

## [v0.20250508.0] - 2025-05-08

### Dependencies
- ansible-core 2.18.4 → 2.18.5 (osism/netbox-manager#73)
- dynaconf 3.2.10 → 3.2.11 (osism/netbox-manager#81)
- setuptools 78.1.0 → 80.3.1 (osism/netbox-manager#72, osism/netbox-manager#74, osism/netbox-manager#75, osism/netbox-manager#77, osism/netbox-manager#78, osism/netbox-manager#79, osism/netbox-manager#80)
- typer 0.15.2 → 0.15.3 (osism/netbox-manager#76)

## [v0.20250406.2] - 2025-04-06

### Changed
- Added debug log messages when devicetype library, moduletype library, or resources are skipped due to no file changes (osism/netbox-manager#70)
- Improved error and debug message wording for clarity (osism/netbox-manager#70)

### Fixed
- Swapped devicetype and moduletype library path checks when detecting changed files (osism/netbox-manager#70)
- Exit condition when all processing is skipped now correctly checks `skipdtl and skipmtl and skipres` instead of the inverted logic (osism/netbox-manager#70)

## [v0.20250406.1] - 2025-04-06

### Added
- Debug mode option for running with verbose logging output (osism/netbox-manager#68)
- Logging of changed files list when running in debug mode (osism/netbox-manager#69)

## [v0.20250406.0] - 2025-04-06

### Added
- Support for processing only changed files via `--no-always` flag using Git diff detection (osism/netbox-manager#67)

### Changed
- Fix operator precedence in devicetype/moduletype library condition check (osism/netbox-manager#67)

### Dependencies
- ansible-core 2.18.3 → 2.18.4 (osism/netbox-manager#63)
- ansible-runner 2.4.0 → 2.4.1 (osism/netbox-manager#65)
- gitpython 3.1.44 added as new dependency (osism/netbox-manager#66)
- setuptools 76.0.0 → 78.1.0 (osism/netbox-manager#60, osism/netbox-manager#61, osism/netbox-manager#62, osism/netbox-manager#64)

## [v0.20250314.0] - 2025-03-14

### Dependencies
- jinja2 3.1.5 → 3.1.6 (security) (osism/netbox-manager#56)
- setuptools 75.8.2 → 76.0.0 (osism/netbox-manager#58)

## [v0.20250304.0] - 2025-03-04

### Added
- Log the name of each handled file (osism/netbox-manager#47)
- Log "Manage resources" step during resource processing (osism/netbox-manager#48)

### Changed
- Package `requirements.yml` and `settings.toml.sample` as additional data files in the Python package
- Validate configuration options with dynaconf validators and provide defaults where possible
- Wait for NetBox REST API availability instead of just the service endpoint
- Exit on failures during the NetBox availability wait

### Fixed
- Negate `IGNORE_SSL_ERRORS` before passing it to `validate_certs` to align semantically (osism/netbox-manager#77af0bc)

### Dependencies
- ansible-core 2.18.2 → 2.18.3 (osism/netbox-manager#49)
- setuptools 75.8.0 → 75.8.2 (osism/netbox-manager#51)
- typer 0.15.1 → 0.15.2 (osism/netbox-manager#53)

## [v0.20250219.3] - 2025-02-19

### Fixed
- Default value of `--version` flag (osism/netbox-manager#45)
- Version callback and SIGINT handler (osism/netbox-manager#46)

## [v0.20250219.2] - 2025-02-19

### Added
- setuptools to requirements (osism/netbox-manager#43)

### Fixed
- callback_version only printing version and exiting when flag is actually set (osism/netbox-manager#44)

## [v0.20250219.1] - 2025-02-19

### Added
- `--version` argument to display the current version (osism/netbox-manager#41)
- SIGINT signal handler for graceful exit (osism/netbox-manager#42)

### Dependencies
- setuptools 75.8.0 (osism/netbox-manager#41)

## [v0.20250219.0] - 2025-02-19

### Added
- Parallel execution of resource files with `--parallel` option (osism/netbox-manager#39)
- Dry run mode with `--dryrun` option (osism/netbox-manager#39)

### Changed
- Group resource files by leading number for dependency-aware execution (osism/netbox-manager#39)

### Fixed
- Fix path to images directory in device type library import (osism/netbox-manager#40)

### Removed
- natsort dependency, replaced by built-in sorting and grouping (osism/netbox-manager#39)

### Dependencies
- ansible-core 2.18.1 → 2.18.2 (osism/netbox-manager#35)
- dynaconf 3.2.6 → 3.2.10 (osism/netbox-manager#38)

## [v0.20250109.0] - 2025-01-09

### Added
- Support for debug tasks (osism/netbox-manager#31)

### Dependencies
- jinja2 3.1.4 → 3.1.5 (osism/netbox-manager#28)
- yamale 5.2.1 → 6.0.0 (osism/netbox-manager#30)

## [v0.20241217.0] - 2024-12-17

### Dependencies
- ansible-core 2.17.7 → 2.18.1 (osism/netbox-manager#8)

## [v0.20241216.0] - 2024-12-16

### Added
- Support for `state` field in resources, allowing resource state management (e.g., absent/present) (osism/netbox-manager#26)

## [v0.20241206.1] - 2024-12-06

### Fixed
- Paths for device types and module types directories (osism/netbox-manager#25)

## [v0.20241206.0] - 2024-12-06

### Added
- Module type library support with `--skipmtl` and `--skipres` CLI options (osism/netbox-manager#24)
- Support for `.secrets.toml` in dynaconf configuration (osism/netbox-manager#22)
- Edgecore 7326-56X-O-AC-B device type (osism/netbox-manager#21)
- Manager device and connections to example resources (osism/netbox-manager#20)

### Changed
- Renamed device type library directory from `example/devicetype-library` to `example/devicetypes` (osism/netbox-manager#24)
- Renamed DTL classes to generic names (`DTLRepo` → `Repo`, `DTLNetBox` → `NetBox`, `DTLLogHandler` → `LogHandler`) (osism/netbox-manager#24)
- Extended README with installation, configuration, and usage instructions (osism/netbox-manager#19)

### Dependencies
- loguru 0.7.2 → 0.7.3 (osism/netbox-manager#23)

## [v0.20241204.1] - 2024-12-04

### Changed
- Use scoped PyPI token in Zuul CI configuration (osism/netbox-manager#16)
- Clean up `__init__.py` file by removing legacy version handling via `pkg_resources` (osism/netbox-manager#18)

### Fixed
- Fix relative import of `dtl` module (osism/netbox-manager#17)

## [v0.20241204.0] - 2024-12-04

### Added
- Initial project setup with CLI application using Typer framework and loguru logging (osism/netbox-manager#3)
- Device Type Library (DTL) import functionality based on netbox-community/Device-Type-Library-Import (osism/netbox-manager#4)
- Configuration management using dynaconf with `settings.toml` support (osism/netbox-manager#4)
- Example device type definitions for Edgecore 7726-32X-O (osism/netbox-manager#4)
- Ansible playbook execution for managing NetBox resources (osism/netbox-manager#6, osism/netbox-manager#9)
- Custom YAML-based data format for resource definitions (osism/netbox-manager#10)
- CLI options `--limit`, `--wait`, and `--skipdtl` (osism/netbox-manager#11)
- Natural sorting of resource files (osism/netbox-manager#11)
- Variable support in resource files via `vars` blocks (osism/netbox-manager#11)
- Connection/cable resource examples (osism/netbox-manager#13)
- `pyproject.toml` with setuptools-git-versioning and Apache 2.0 LICENSE (osism/netbox-manager#14)
- PyPI package publishing via Zuul CI (osism/netbox-manager#15)

### Changed
- Renamed project from netbox-connection-manager to netbox-manager (osism/netbox-manager#4)
- Moved device type library examples to `example/` directory (osism/netbox-manager#7)

### Fixed
- Device type library filter parameter names (`devicetype_id` → `device_type_id`, `moduletype_id` → `module_type_id`) (osism/netbox-manager#13)

### Dependencies
- ansible-core 2.17.7 (new) (osism/netbox-manager#6)
- ansible-runner 2.4.0 (new) (osism/netbox-manager#6)
- Jinja2 3.1.4 (new) (osism/netbox-manager#9)
- natsort 8.4.0 (new) (osism/netbox-manager#11)
- typer 0.14.0 → 0.15.1 (osism/netbox-manager#5, osism/netbox-manager#12)

