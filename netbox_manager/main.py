import sys
import time
import warnings

from dynaconf import Dynaconf
from loguru import logger
import pynetbox
import typer

from dtl import DTLRepo, DTLNetBox

warnings.filterwarnings("ignore")

log_fmt = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<level>{message}</level>"
)

logger.remove()
logger.add(sys.stderr, format=log_fmt, level="INFO", colorize=True)

settings = Dynaconf(
    envvar_prefix="NETBOX_MANAGER",
    settings_files=["settings.toml"],
    load_dotenv=True,
)

assert type(settings.DEVICETYPE_LIBRARY) is str
assert type(settings.TOKEN) is str
assert type(settings.URL) is str

nb = pynetbox.api(settings.URL, token=settings.TOKEN)


def run() -> None:
    start = time.time()

    # handle devicetype library

    dtl_repo = DTLRepo(settings.DEVICETYPE_LIBRARY)
    files, vendors = dtl_repo.get_devices()
    device_types = dtl_repo.parse_files(files)

    dtl_netbox = DTLNetBox(settings)
    dtl_netbox.create_manufacturers(vendors)
    dtl_netbox.create_device_types(device_types)

    end = time.time()
    logger.info(f"Runtime: {(end-start):.4f}s")


def main() -> None:
    typer.run(run)


if __name__ == "__main__":
    main()
