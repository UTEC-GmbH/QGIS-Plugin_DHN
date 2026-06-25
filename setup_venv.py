"""Automates the creation of a QGIS-compatible virtual environment.

Usage:
    1. Open the OSGeo4W Shell from the Windows Start Menu.
    2. Navigate to the directory containing this script using the 'cd' command.
       cd path_to_folder
    3. Execute the setup script using the 'python' command:
       python setup_venv.py
"""

import contextlib
import logging
import subprocess
import sys
from pathlib import Path
from typing import Final, NoReturn

# Default OSGeo4W path for QGIS LTR
DEFAULT_OSGEO4W: Final[Path] = Path("C:/OSGeo4W")
DEV_DEPENDENCIES: Final[list[str]] = [
    "pytest",
    "pytest-qgis",
    "pytest-qt",
    "pytest-cov",
    "ruff",
]

logger: logging.Logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure the module's logger to print to the console."""
    # Ensure stdout handles non-ASCII characters
    # without raising UnicodeEncodeError on Windows
    if hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(errors="replace")
    handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)
    formatter: logging.Formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _fail(message: str) -> NoReturn:
    """Log a failure message and raise a RuntimeError.

    Args:
        message: The error message to log and raise.

    Raises:
        RuntimeError: Always raised with the provided message.
    """
    logger.error("❌ %s", message)
    raise RuntimeError(message)


def get_qgis_python_path(root_path: Path) -> Path:
    """Locate the Python executable within the OSGeo4W directory.

    Args:
        root_path: The root directory of the OSGeo4W installation.

    Returns:
        Path: The absolute path to the Python executable.

    Raises:
        RuntimeError: If the Python executable cannot be found.
    """
    # Common locations for Python in OSGeo4W (e.g., Python312, Python39)
    apps_dir: Path
    if not (apps_dir := root_path / "apps").exists():
        _fail(f"OSGeo4W apps directory not found at {apps_dir}")

    # Find all directories starting with 'Python' that contain a valid executable
    python_exes: list[Path] = sorted(
        [
            exe
            for directory in apps_dir.glob("Python*")
            if (exe := directory / "python.exe").exists()
        ],
        reverse=True,
    )

    if not python_exes:
        _fail("No Python installation found in OSGeo4W/apps.")

    return python_exes[0]


def setup_environment() -> None:
    """Create the venv and install development dependencies."""
    try:
        qgis_python: Path = get_qgis_python_path(DEFAULT_OSGEO4W)
        project_root: Path = Path(__file__).resolve().parent
        venv_path: Path = project_root / ".venv"

        logger.info("Using QGIS Python: %s", qgis_python)
        logger.info("Creating venv at: %s", venv_path)

        # Create venv with system-site-packages enabled
        subprocess.run(  # noqa: S603
            [str(qgis_python), "-m", "venv", str(venv_path), "--system-site-packages"],
            check=True,
        )

        # Create qgis.pth inside the virtual environment's site-packages to allow tools
        # (IDE, linter, tests) to resolve and import QGIS and its PyQt library.
        site_packages_dir: Path = venv_path / "Lib" / "site-packages"
        if site_packages_dir.exists():
            qgis_path_str: str = str(
                DEFAULT_OSGEO4W / "apps" / "qgis-ltr" / "python"
            ).replace("\\", "/")
            qgis_plugins_path_str: str = str(
                DEFAULT_OSGEO4W / "apps" / "qgis-ltr" / "python" / "plugins"
            ).replace("\\", "/")
            pth_file: Path = site_packages_dir / "qgis.pth"
            logger.info("Configuring QGIS path redirects at: %s", pth_file)
            pth_file.write_text(
                f"{qgis_path_str}\n{qgis_plugins_path_str}\n", encoding="utf-8"
            )

            # Create sitecustomize.py to add OSGeo4W DLL directories
            # to the Python DLL search path
            # and configure QGIS-related environment variables automatically.
            sitecustomize_file: Path = site_packages_dir / "sitecustomize.py"
            logger.info(
                "Configuring OSGeo4W DLL paths and environment variables at: %s",
                sitecustomize_file,
            )
            sitecustomize_content = f"""
import os
import sys

# Configure environment variables and DLL directories for OSGeo4W QGIS
OSGEO4W_ROOT = r"{DEFAULT_OSGEO4W}"

if os.path.exists(OSGEO4W_ROOT):
    # Define paths
    bin_path = os.path.join(OSGEO4W_ROOT, "bin")
    qgis_bin = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "bin")
    qt5_bin = os.path.join(OSGEO4W_ROOT, "apps", "qt5", "bin")
    
    # 1. Add DLL directories for Python 3.8+ on Windows
    for path in [bin_path, qgis_bin, qt5_bin]:
        if os.path.exists(path):
            os.add_dll_directory(path)
            
    # 2. Update system PATH to ensure subprocesses and legacy DLL loaders find them
    path_env = os.environ.get("PATH", "")
    osgeo_paths = ";".join([bin_path, qgis_bin, qt5_bin])
    if osgeo_paths not in path_env:
        os.environ["PATH"] = osgeo_paths + ";" + path_env

    # 3. Set OSGeo4W / QGIS specific environment variables
    os.environ["QGIS_PREFIX_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr").replace("\\\\", "/")
    os.environ["GDAL_FILENAME_IS_UTF8"] = "YES"
    os.environ["VSI_CACHE"] = "TRUE"
    os.environ["VSI_CACHE_SIZE"] = "1000000"
    os.environ["QT_PLUGIN_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "qtplugins") + ";" + os.path.join(OSGEO4W_ROOT, "apps", "qt5", "plugins")
"""  # noqa: E501, S608
            sitecustomize_file.write_text(sitecustomize_content, encoding="utf-8")

        # Install testing and linting tools using the new venv's pip
        pip_exe: Path
        if not (pip_exe := venv_path / "Scripts" / "pip.exe").exists():
            _fail(f"Failed to create venv: {pip_exe} missing.")

        logger.info("Installing development dependencies...")
        subprocess.run(  # noqa: S603
            [str(pip_exe), "install", *DEV_DEPENDENCIES],
            check=True,
        )

        logger.info("\n✅ Virtual environment setup complete!")
        logger.info("Activate it with: %s\\Scripts\\activate", venv_path)

    except (RuntimeError, subprocess.CalledProcessError):
        logger.exception("❌ A critical error occurred during environment setup")
        raise


def main() -> int:
    """CLI entry point.

    Returns:
        int: 0 for success, 1 for failure.
    """
    setup_logging()
    try:
        setup_environment()
    except (RuntimeError, subprocess.CalledProcessError):
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main())
