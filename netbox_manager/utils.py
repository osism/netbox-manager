# SPDX-License-Identifier: Apache-2.0

import git
from loguru import logger
import typer


def get_changed_files(debug: bool = False) -> list[str]:
    """Get list of changed files from Git repository."""
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

    return files_changed
