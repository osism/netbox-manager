# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``Repo`` file walker (issue #257, Tier 9 of #232).

``netbox_manager.dtl.Repo`` is the pure-filesystem half of the Device Type
Library importer: it lists vendor directories, globs their YAML files and parses
each into a device-type dict. It never touches NetBox, so these tests run
entirely against ``tmp_path`` trees built by the shared ``make_dtl_tree``
fixture (``conftest.py``) -- no pynetbox mock is involved.

``Repo.get_devices_path`` joins ``os.getcwd()`` with the configured base path;
``os.path.join`` returns the second operand unchanged when it is absolute, so
passing the absolute ``tmp_path`` library directory keeps the walk hermetic.
"""

import pytest

from netbox_manager import dtl


class TestSlugFormat:
    """``slug_format`` (dtl.py:89) -- ``re_sub(r"\\W+", "-", name.lower())``."""

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("Arista Networks", "arista-networks"),
            ("Cisco", "cisco"),
            ("A B  C", "a-b-c"),  # runs of whitespace collapse to a single dash
            ("Juniper (JNPR)", "juniper-jnpr-"),  # trailing punctuation -> dash
            ("Foo_Bar", "foo_bar"),  # underscore is a word char, kept as-is
        ],
    )
    def test_collapses_non_word_runs_and_lowercases(self, name, expected):
        assert dtl.Repo("unused").slug_format(name) == expected


class TestGetDevices:
    """``get_devices`` (dtl.py:92) -- vendor discovery and YAML globbing."""

    def test_ignores_plain_files_and_globs_both_extensions(self, make_dtl_tree):
        base = make_dtl_tree(
            vendors={
                "Arista": {
                    "dcs-7050.yaml": {"manufacturer": "Arista", "slug": "dcs-7050"},
                    "dcs-7280.yml": {"manufacturer": "Arista", "slug": "dcs-7280"},
                },
                "Cisco": {
                    "nexus.yaml": {"manufacturer": "Cisco", "slug": "nexus"},
                },
            },
            loose_files=[".gitkeep"],
        )

        files, vendors = dtl.Repo(base).get_devices()

        # The loose .gitkeep is not a directory, so it is never a vendor.
        assert {v["name"] for v in vendors} == {"Arista", "Cisco"}
        assert {v["slug"] for v in vendors} == {"arista", "cisco"}
        # Both *.yaml and *.yml under each vendor are collected.
        assert {file.rsplit("/", 1)[-1] for file in files} == {
            "dcs-7050.yaml",
            "dcs-7280.yml",
            "nexus.yaml",
        }

    def test_vendor_filter_casefolds_only_the_folder_name(self, make_dtl_tree):
        base = make_dtl_tree(
            vendors={
                "Arista": {"a.yaml": {"manufacturer": "Arista", "slug": "a"}},
                "Cisco": {"c.yaml": {"manufacturer": "Cisco", "slug": "c"}},
            },
        )

        # The folder name is casefolded before membership testing, so a
        # lowercase filter matches the mixed-case folder.
        files, vendors = dtl.Repo(base).get_devices(vendors=["arista"])
        assert [v["name"] for v in vendors] == ["Arista"]
        assert {file.rsplit("/", 1)[-1] for file in files} == {"a.yaml"}

        # The filter is one-sided: only the folder is casefolded, so a
        # mixed-case *filter* value matches nothing.
        files, vendors = dtl.Repo(base).get_devices(vendors=["Arista"])
        assert vendors == []
        assert files == []

    @pytest.mark.parametrize("empty_filter", [None, []])
    def test_none_or_empty_filter_returns_all_vendors(
        self, make_dtl_tree, empty_filter
    ):
        base = make_dtl_tree(
            vendors={
                "Arista": {"a.yaml": {"manufacturer": "Arista", "slug": "a"}},
                "Cisco": {"c.yaml": {"manufacturer": "Cisco", "slug": "c"}},
            },
        )

        _, vendors = dtl.Repo(base).get_devices(vendors=empty_filter)
        assert {v["name"] for v in vendors} == {"Arista", "Cisco"}


class TestParseFiles:
    """``parse_files`` (dtl.py:121) -- YAML round-trip, rewrite and filter."""

    def test_rewrites_manufacturer_and_injects_src(self, make_dtl_tree):
        base = make_dtl_tree(
            vendors={
                "Arista": {
                    "dcs-7050.yaml": {
                        "manufacturer": "Arista Networks",
                        "model": "DCS-7050",
                        "slug": "dcs-7050",
                    }
                }
            }
        )
        repo = dtl.Repo(base)
        files, _ = repo.get_devices()

        parsed = repo.parse_files(files)

        assert len(parsed) == 1
        assert parsed[0]["manufacturer"] == {
            "name": "Arista Networks",
            "slug": "arista-networks",
        }
        # src is the file the device type was parsed from.
        assert parsed[0]["src"] == files[0]

    def test_slug_filter_is_substring_and_case_insensitive(self, make_dtl_tree):
        base = make_dtl_tree(
            vendors={
                "Arista": {
                    # Upper-cased slug proves the data side is casefolded too.
                    "dcs-7050.yaml": {"manufacturer": "Arista", "slug": "DCS-7050"},
                    "mx480.yaml": {"manufacturer": "Arista", "slug": "mx480"},
                }
            }
        )
        repo = dtl.Repo(base)
        files, _ = repo.get_devices()

        parsed = repo.parse_files(files, slugs=["7050"])

        assert [dt["slug"] for dt in parsed] == ["DCS-7050"]

    def test_skips_unparseable_yaml(self, make_dtl_tree):
        base = make_dtl_tree(
            vendors={
                "Arista": {
                    "broken.yaml": "manufacturer: [1, 2",  # unterminated flow seq
                    "valid.yaml": {"manufacturer": "Arista", "slug": "valid"},
                }
            }
        )
        repo = dtl.Repo(base)
        files, _ = repo.get_devices()

        parsed = repo.parse_files(files)

        # The broken file is skipped via ``continue``; the valid sibling parses.
        assert [dt["slug"] for dt in parsed] == ["valid"]
