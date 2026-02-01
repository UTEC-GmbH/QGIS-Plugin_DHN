"""Module: context.py

This module contains the PluginContext class, which serves as a centralized
access point for shared plugin objects such as the QGIS interface, the
current project, and the plugin directory.
"""
# pylint: disable=import-outside-toplevel
# ruff: noqa: PLC0415, PLR2004

from pathlib import Path

from qgis.core import Qgis, QgsProject
from qgis.gui import QgisInterface, QgsMessageBar
from qgis.PyQt.QtCore import QT_VERSION_STR, QCoreApplication


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
            QgisInterface: The QGIS interface instance.

        Raises:
            CustomRuntimeError: If the context has not been initialized.
        """
        from .logs_and_errors import raise_runtime_error

        if cls._iface is None:
            raise_runtime_error("PluginContext not initialized with iface.")
        return cls._iface

    @classmethod
    def project(cls) -> QgsProject:
        """Return the current QGIS project instance.

        Returns:
            QgsProject: The current QGIS project.

        Raises:
            CustomRuntimeError: If no QGIS project is currently open.
        """
        from .logs_and_errors import raise_runtime_error

        project: QgsProject | None = QgsProject.instance()
        if project is None:
            raise_runtime_error("No QGIS project is currently open.")
        return project

    @classmethod
    def message_bar(cls) -> QgsMessageBar | None:
        """Get the QGIS message bar.

        Returns:
            QgsMessageBar | None: The QGIS message bar or None if not available.
        """
        return cls._iface.messageBar() if cls._iface else None

    @classmethod
    def plugin_dir(cls) -> Path:
        """Get the plugin directory.

        Returns:
            Path: The absolute path to the plugin directory.

        Raises:
            CustomRuntimeError: If the context has not been initialized.
        """
        from .logs_and_errors import raise_runtime_error

        if cls._plugin_dir is None:
            raise_runtime_error("PluginContext not initialized with plugin_dir.")
        return cls._plugin_dir

    @classmethod
    def resources_path(cls) -> Path:
        """Get the resources directory path.

        Returns:
            Path: The absolute path to the resources directory.
        """
        return cls.plugin_dir() / "resources"

    @classmethod
    def icons_path(cls) -> Path:
        """Get the icons directory path.

        Returns:
            Path: The absolute path to the icons directory.
        """
        return cls.resources_path() / "icons"

    @classmethod
    def templates_path(cls) -> Path:
        """Get the templates directory path.

        Returns:
            Path: The absolute path to the templates directory.
        """
        return cls.resources_path() / "templates"

    @classmethod
    def project_path(cls) -> Path:
        r"""Get the file path of the current QGIS project.

        Returns:
            Path: The path to the current QGIS project file
                (e.g., 'C:\project\my_project.qgz').
        """
        from .logs_and_errors import raise_user_error

        project: QgsProject = cls.project()
        project_path: str = project.fileName()
        if not project_path:
            # fmt: off
            msg: str = QCoreApplication.translate("UserError", "Project is not saved. Please save the project first.")  # noqa: E501
            # fmt: on
            raise_user_error(msg)

        return Path(project_path)

    @classmethod
    def project_gpkg(cls) -> Path:
        """Return the expected GeoPackage path for the current project.

        Example: for a project 'my_project.qgz', returns 'my_project.gpkg'.

        Returns:
             Path: The Path object to the GeoPackage.
        """
        return cls.project_path().with_suffix(".gpkg")

    @classmethod
    def is_dark_theme(cls) -> bool:
        """Check if QGIS is running with a dark theme.

        Returns:
            bool: True if the theme is dark, False otherwise.
        """
        iface: QgisInterface = cls.iface()
        window = iface.mainWindow()
        bg_color = window.palette().color(window.backgroundRole())  # pyright: ignore[reportOptionalMemberAccess]

        return bg_color.value() < 128

    @staticmethod
    def is_qgis4() -> bool:
        """Check if running on QGIS 4.

        Returns:
            bool: True if running on QGIS 4 or newer, False otherwise.
        """
        return Qgis.QGIS_VERSION_INT // 10000 >= 4

    @staticmethod
    def is_qt6() -> bool:
        """Check if running on Qt 6.

        Returns:
            bool: True if running on Qt 6 or newer, False otherwise.
        """
        return int(QT_VERSION_STR.split(".")[0]) >= 6
