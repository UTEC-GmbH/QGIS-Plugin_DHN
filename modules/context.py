"""Module: context.py

This module contains the PluginContext class, which serves as a centralized
access point for shared plugin objects such as the QGIS interface, the
current project, and the plugin directory.
"""

import inspect
from pathlib import Path
from types import FrameType
from typing import Final, NoReturn

from qgis.core import Qgis, QgsMessageLog, QgsProject
from qgis.gui import QgisInterface, QgsMessageBar
from qgis.PyQt.QtCore import QT_VERSION_STR, QCoreApplication

# A value of 128 is used as a threshold for determining if a color is "dark".
# This is based on the color's luminance, where values closer to 0 are darker
# and values closer to 255 are lighter.
DARK_THEME_LUMINANCE_THRESHOLD: Final[int] = 128


def file_line(frame: FrameType | None) -> str:
    """Return the filename and line number of the caller.

    This function inspects the call stack to determine the file and line number
    from which `log_debug` or `log_and_show_error` was called.

    Args:
        frame: The current frame object,
            typically obtained via `inspect.currentframe()`.

    Returns:
        A string formatted as " (filename: line_number)" or an empty string if
        the frame information is not available.
    """
    if frame and frame.f_back:
        filename: str = Path(frame.f_back.f_code.co_filename).name
        lineno: int = frame.f_back.f_lineno
        return f" [{filename}: {lineno}]"
    return ""


class ContextRuntimeError(Exception):
    """Custom exception for runtime errors in the context module."""


def raise_context_runtime_error(error_msg: str) -> NoReturn:
    """Log a critical error, display it, and raise a ContextRuntimeError.

    Args:
        error_msg: The error message to be displayed and raised.

    Raises:
        ContextRuntimeError: The raised exception with the error message.
    """
    file_line_number: str = file_line(inspect.currentframe())
    error_msg = f"{error_msg}{file_line_number}"
    QgsMessageLog.logMessage(
        message=f"💀 {error_msg}",
        tag="UTEC Plugin ERROR",
        level=Qgis.Critical,
        notifyUser=True,
    )

    raise ContextRuntimeError(error_msg)


class PluginContext:
    """Singleton-like storage for plugin-wide context."""

    _iface: QgisInterface | None = None
    _plugin_dir: Path | None = None

    @classmethod
    def init(cls, iface: QgisInterface, plugin_dir: Path) -> None:
        """Initialize with the QGIS interface and plugin directory.

        Args:
            iface: The QGIS interface instance.
            plugin_dir: The root directory of the plugin.
        """
        cls._iface = iface
        cls._plugin_dir = plugin_dir

    @classmethod
    def iface(cls) -> QgisInterface:
        """Get the QGIS interface.

        Returns:
            The QGIS interface instance.

        Raises:
            ContextRuntimeError: If the context has not been initialized.
        """
        if cls._iface is None:
            raise_context_runtime_error("PluginContext not initialized with iface.")
        return cls._iface

    @classmethod
    def project(cls) -> QgsProject:
        """Return the current QGIS project instance.

        Returns:
            The current QGIS project.

        Raises:
            ContextRuntimeError: If no QGIS project is currently open.
        """
        project: QgsProject | None = QgsProject.instance()
        if project is None:
            raise_context_runtime_error("No QGIS project is currently open.")
        return project

    @classmethod
    def message_bar(cls) -> QgsMessageBar | None:
        """Get the QGIS message bar.

        Returns:
            The QGIS message bar or None if not available.
        """
        return cls._iface.messageBar() if cls._iface else None

    @classmethod
    def plugin_dir(cls) -> Path:
        """Get the plugin directory.

        Returns:
            The absolute path to the plugin directory.

        Raises:
            ContextRuntimeError: If the context has not been initialized.
        """
        if cls._plugin_dir is None:
            raise_context_runtime_error(
                "PluginContext not initialized with plugin_dir."
            )
        return cls._plugin_dir

    @classmethod
    def resources_path(cls) -> Path:
        """Get the resources directory path.

        Returns:
            The absolute path to the resources directory.
        """
        return cls.plugin_dir() / "resources"

    @classmethod
    def icons_path(cls) -> Path:
        """Get the icons directory path.

        Returns:
            The absolute path to the icons directory.
        """
        return cls.resources_path() / "icons"

    @classmethod
    def templates_path(cls) -> Path:
        """Get the templates directory path.

        Returns:
            The absolute path to the templates directory.
        """
        return cls.resources_path() / "templates"

    @classmethod
    def project_path(cls) -> Path:
        r"""Get the file path of the current QGIS project.

        Returns:
            The path to the current QGIS project file (e.g.,
            'C:\project\my_project.qgz').

        Raises:
            ContextRuntimeError: If the project has not been saved.
        """
        project: QgsProject = cls.project()
        project_path: str = project.fileName()
        if not project_path:
            msg: str = QCoreApplication.translate(
                "UserError", "Project is not saved. Please save the project first."
            )
            raise_context_runtime_error(msg)

        return Path(project_path)

    @classmethod
    def project_gpkg(cls) -> Path:
        """Return the expected GeoPackage path for the current project.

        Example:
            For a project 'my_project.qgz', returns 'my_project.gpkg'.

        Returns:
             The Path object to the GeoPackage.
        """
        return cls.project_path().with_suffix(".gpkg")

    @classmethod
    def is_dark_theme(cls) -> bool:
        """Check if QGIS is running with a dark theme.

        Returns:
            True if the theme is dark, False otherwise.
        """
        iface: QgisInterface = cls.iface()
        window = iface.mainWindow()
        if not window:
            return False

        bg_color = window.palette().color(window.backgroundRole())

        return bg_color.value() < DARK_THEME_LUMINANCE_THRESHOLD

    @staticmethod
    def is_qgis4() -> bool:
        """Check if running on QGIS 4.

        Returns:
            True if running on QGIS 4 or newer, False otherwise.
        """
        # QGIS_VERSION_INT is structured as MMmmpp (e.g., 31609 for 3.16.9).
        # Integer division by 10000 extracts the major version number.
        return Qgis.QGIS_VERSION_INT // 10000 >= 4  # noqa: PLR2004

    @staticmethod
    def is_qt6() -> bool:
        """Check if running on Qt 6.

        Returns:
            True if running on Qt 6 or newer, False otherwise.
        """
        return int(QT_VERSION_STR.split(".")[0]) >= 6  # noqa: PLR2004
