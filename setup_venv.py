r"""Automates the creation of a QGIS-compatible virtual environment.

Usage:
    1. Open the OSGeo4W Shell from the Windows Start Menu.
    2. Navigate to the directory containing this script using the 'cd' command.
       cd path_to_folder
    3. Execute the setup script using the 'python' command:
       python setup_venv.py
    4. Before using the venv from the OSGeo4W Shell, clear its Python overrides:
       set PYTHONHOME=
       set PYTHONPATH=
       set PYTHONEXECUTABLE=
       .venv\\Scripts\\activate

Set OSGEO4W_ROOT or QGIS_PREFIX_PATH to use a non-default installation.
In VS Code, select the project's .venv interpreter if it is not selected
automatically.
"""

import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final, NoReturn

# Default OSGeo4W path for QGIS LTR
DEFAULT_OSGEO4W: Final[Path] = Path(os.environ.get("OSGEO4W_ROOT") or "C:/OSGeo4W")
DEV_DEPENDENCIES: Final[list[str]] = [
    "pytest",
    "pytest-qgis",
    "pytest-qt",
    "pytest-cov",
    "pyrefly",
    "ruff",
    "PyQt5-stubs",
    "PyQt6-stubs",
    "qgis-stubs",
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
    python_exes = [
        exe
        for directory in apps_dir.glob("Python*")
        if (exe := directory / "python.exe").exists()
    ]

    if not python_exes:
        _fail("No Python installation found in OSGeo4W/apps.")

    def version_key(executable: Path) -> tuple[int, int]:
        match = re.fullmatch(r"Python(\d)(\d+)", executable.parent.name)
        if match is None:
            return (0, 0)
        return int(match.group(1)), int(match.group(2))

    return max(python_exes, key=version_key)


def get_qgis_prefix_path(root_path: Path) -> Path:
    """Locate the QGIS installation prefix within OSGeo4W."""
    configured_prefix = os.environ.get("QGIS_PREFIX_PATH")
    if configured_prefix:
        prefix_path = Path(configured_prefix)
        if not (prefix_path / "python" / "qgis").is_dir():
            _fail(f"QGIS Python package not found at {prefix_path / 'python' / 'qgis'}")
        return prefix_path

    qgis_name = os.environ.get("QGIS_ENVNAME", "qgis-ltr")
    prefix_path = root_path / "apps" / qgis_name
    if not (prefix_path / "python" / "qgis").is_dir():
        _fail(f"QGIS Python package not found at {prefix_path / 'python' / 'qgis'}")

    return prefix_path


def get_venv_subprocess_environment() -> dict[str, str]:
    """Return an environment that does not override virtual-environment Python."""
    env = os.environ.copy()
    for variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE"):
        env.pop(variable, None)
    return env


def configure_venv_python_home(qgis_python: Path, venv_path: Path) -> None:
    """Point the venv at the OSGeo4W Python directory
    containing the standard library.
    """
    config_file = venv_path / "pyvenv.cfg"
    if not config_file.is_file():
        _fail(f"Virtual environment configuration not found at {config_file}")

    python_home = qgis_python.parent
    if not (python_home / "Lib" / "encodings").is_dir():
        _fail(f"Python standard library not found under {python_home}")

    config_lines = config_file.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(config_lines):
        if line.partition("=")[0].strip().lower() == "home":
            config_lines[index] = f"home = {python_home}"
            break
    else:
        _fail(f"Python home setting not found in {config_file}")

    config_file.write_text("\n".join(config_lines) + "\n", encoding="utf-8")


def create_virtual_environment(qgis_python: Path, venv_path: Path) -> None:
    """Create the virtual environment with a venv fallback to virtualenv."""
    logger.info("Using QGIS Python: %s", qgis_python)
    logger.info("Creating venv at: %s", venv_path)
    subprocess_env = get_venv_subprocess_environment()

    attempts: list[list[str]] = [
        [str(qgis_python), "-m", "venv", str(venv_path), "--system-site-packages"],
        [
            str(qgis_python),
            "-m",
            "virtualenv",
            str(venv_path),
            "--system-site-packages",
        ],
    ]

    if virtualenv_exe := shutil.which("virtualenv"):
        attempts.append(
            [
                virtualenv_exe,
                "--python",
                str(qgis_python),
                str(venv_path),
                "--system-site-packages",
            ]
        )

    errors: list[str] = []
    for command in attempts:
        try:
            subprocess.run(command, check=True, env=subprocess_env)  # noqa: S603
            configure_venv_python_home(qgis_python, venv_path)
            logger.info(
                "✅ Virtual environment created successfully with: %s",
                " ".join(command),
            )
            return  # noqa: TRY300
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            errors.append(f"{command[0]} {' '.join(command[1:])}: {exc}")
            logger.warning("⚠️ Virtual environment creation failed using %s", command)

    raise RuntimeError(
        """❌ Unable to create the virtual environment with venv or virtualenv. 
        Use OSGeo4W setup to install python3-venv or python3-virtualenv. 
        Errors encountered: """
        + " | ".join(errors)
    )


def setup_environment() -> None:
    """Create the venv and install development dependencies."""
    try:
        osgeo4w_root = DEFAULT_OSGEO4W
        qgis_prefix = get_qgis_prefix_path(osgeo4w_root)
        qgis_python: Path = get_qgis_python_path(osgeo4w_root)
        project_root: Path = Path(__file__).resolve().parent
        venv_path: Path = project_root / ".venv"

        create_virtual_environment(qgis_python, venv_path)

        # Create qgis.pth inside the virtual environment's site-packages to allow tools
        # (IDE, linter, tests) to resolve and import QGIS and its PyQt library.
        site_packages_dir: Path = venv_path / "Lib" / "site-packages"
        if not site_packages_dir.is_dir():
            _fail(f"Virtual environment site-packages not found at {site_packages_dir}")

        qgis_path_str = (qgis_prefix / "python").as_posix()
        qgis_plugins_path_str = (qgis_prefix / "python" / "plugins").as_posix()
        pth_file: Path = site_packages_dir / "qgis.pth"
        logger.info("Configuring QGIS path redirects at: %s", pth_file)
        pth_file.write_text(
            f"{qgis_path_str}\n{qgis_plugins_path_str}\n", encoding="utf-8"
        )

        # Create sitecustomize.py to configure OSGeo4W DLL and QGIS paths.
        sitecustomize_file: Path = site_packages_dir / "sitecustomize.py"
        logger.info(
            "Configuring OSGeo4W DLL paths and environment variables at: %s",
            sitecustomize_file,
        )
        sitecustomize_content = f"""
import os

OSGEO4W_ROOT = {str(osgeo4w_root)!r}
QGIS_PREFIX = {str(qgis_prefix)!r}
QT_PREFIX = os.path.join(OSGEO4W_ROOT, "apps", "qt5")

if os.path.exists(OSGEO4W_ROOT):
    bin_path = os.path.join(OSGEO4W_ROOT, "bin")
    qgis_bin = os.path.join(QGIS_PREFIX, "bin")
    qt5_bin = os.path.join(QT_PREFIX, "bin")
    dll_directories = [bin_path, qgis_bin, qt5_bin]
    # Keep handles alive so these DLL search directories remain active.
    _dll_directory_handles = [
        os.add_dll_directory(path) for path in dll_directories if os.path.isdir(path)
    ]

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        [path for path in dll_directories if os.path.isdir(path)] + path_entries
    )
    os.environ["QGIS_PREFIX_PATH"] = QGIS_PREFIX.replace("\\\\", "/")
    os.environ["GDAL_FILENAME_IS_UTF8"] = "YES"
    os.environ["VSI_CACHE"] = "TRUE"
    os.environ["VSI_CACHE_SIZE"] = "1000000"
    os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(
        [
            os.path.join(QGIS_PREFIX, "qtplugins"),
            os.path.join(QT_PREFIX, "plugins"),
        ]
    )
"""
        sitecustomize_file.write_text(sitecustomize_content, encoding="utf-8")

        venv_python = venv_path / "Scripts" / "python.exe"
        if not venv_python.is_file():
            _fail(f"Failed to create venv: {venv_python} missing.")

        logger.info("Verifying that the virtual environment can import QGIS...")
        subprocess.run(  # noqa: S603
            [
                str(venv_python),
                "-c",
                (
                    "from qgis.PyQt.QtGui import QIcon; "
                    "from qgis.core import Qgis; "
                    "print(QIcon, Qgis.MessageLevel.Warning)"
                ),
            ],
            check=True,
            env=get_venv_subprocess_environment(),
        )

        logger.info("Installing development dependencies...")
        subprocess.run(  # noqa: S603
            [str(venv_python), "-m", "pip", "install", *DEV_DEPENDENCIES],
            check=True,
            env=get_venv_subprocess_environment(),
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
