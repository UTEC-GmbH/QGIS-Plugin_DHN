"""Main module for the UTEC Massenermittlung plugin."""

import configparser
import contextlib
import traceback
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import Qgis, QgsApplication, QgsLayerTreeNode, QgsProject, QgsVectorLayer
from qgis.gui import QgisInterface, QgsLayerTreeView
from qgis.PyQt.QtCore import QCoreApplication, QObject, QSettings, Qt, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMenu, QProgressBar, QToolButton

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:
    from qgis.PyQt.QtGui import QAction

from .modules.constants import ICONS, Names
from .modules.context import PluginContext
from .modules.duplicate_filter import DuplicateFilter
from .modules.excel_exporter import ExcelExporter
from .modules.layer_manager import LayerManager
from .modules.logs_and_errors import (
    CustomRuntimeError,
    CustomUserError,
    create_summary_message,
    log_debug,
    raise_runtime_error,
    raise_user_error,
    show_message,
)
from .modules.poi_classifier import PointOfInterestClassifier

if TYPE_CHECKING:
    from qgis.core import QgsLayerMetadata
    from qgis.gui import QgsMessageBar, QgsMessageBarItem


class DHN(QObject):
    """QGIS Plugin for renaming and moving layers to a GeoPackage."""

    def __init__(self, iface: QgisInterface) -> None:
        """Initialize the plugin.

        :param iface: An interface instance that allows interaction with QGIS.
        """
        super().__init__()
        self.plugin_dir: Path = Path(__file__).parent
        PluginContext.init(iface, self.plugin_dir)

        self.project: QgsProject = PluginContext.project()
        self.iface: QgisInterface = iface
        self.msg_bar: QgsMessageBar | None = iface.messageBar()
        self.actions: list = []
        self.plugin_menu: QMenu | None = None
        self.plugin_icon: QIcon = ICONS.main_icon
        self.translator: QTranslator | None = None
        self.layer_manager: LayerManager | None = None

        # Read metadata to get the plugin name for UI elements
        self.plugin_name: str = "UTEC Massenermittlung (dev)"  # Default
        metadata_path: Path = self.plugin_dir / "metadata.txt"
        if metadata_path.exists():
            config = configparser.ConfigParser()
            config.read(metadata_path)
            try:
                self.plugin_name = config.get("general", "name")
            except (configparser.NoSectionError, configparser.NoOptionError):
                log_debug("Could not read name from metadata.txt", Qgis.Warning)

        self.menu: str = self.plugin_name

        # initialize translation
        locale = QSettings().value("locale/userLocale", "en")[:2]
        translator_path: Path = self.plugin_dir / "i18n" / f"{locale}.qm"

        if not translator_path.exists():
            log_debug(f"Translator not found in: {translator_path}", Qgis.Warning)
        else:
            self.translator = QTranslator()
            if self.translator is not None and self.translator.load(
                str(translator_path)
            ):
                QCoreApplication.installTranslator(self.translator)
            else:
                log_debug("Translator could not be installed.", Qgis.Warning)

    def add_action(  # noqa: PLR0913 # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        icon: QIcon,
        button_text: str,
        callback: Callable,
        enabled_flag: bool = True,  # noqa: FBT001, FBT002
        add_to_menu: bool = True,  # noqa: FBT001, FBT002
        add_to_toolbar: bool = True,  # noqa: FBT001, FBT002
        tool_tip: str | None = None,
        parent=None,  # noqa: ANN001
    ) -> QAction:  # pyright: ignore[reportInvalidTypeForm]
        """Create and configure a QAction for the plugin.

        This helper method creates a QAction, connects it to a callback, and
        optionally adds it to the QGIS toolbar and the plugin's menu.

        Args:
            icon: Path to the icon or QIcon object.
            button_text: Text to be displayed for the action in menus.
            callback: The function to execute when the action is triggered.
            enabled_flag: Whether the action should be enabled by default.
            add_to_menu: If True, adds the action to the plugin's menu.
            add_to_toolbar: If True, adds the action to a QGIS toolbar.
            tool_tip: Optional tooltip text for the action.
            parent: The parent widget for the action, typically the QGIS main window.

        Returns:
            The configured QAction instance.
        """

        if isinstance(icon, str):
            icon = QIcon(icon)
        action = QAction(icon, button_text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if tool_tip is not None:
            action.setToolTip(tool_tip)

        if add_to_toolbar:
            self.iface.addToolBarIcon(action)

        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)

        return action

    def initGui(self) -> None:  # noqa: N802
        """Create the menu entries and toolbar icons for the plugin."""

        # Create a menu for the plugin in the "Plugins" menu
        self.plugin_menu = QMenu(self.menu, self.iface.pluginMenu())
        if self.plugin_menu is None:
            raise_runtime_error("Failed to create the plugin menu.")

        self.plugin_menu.setToolTipsVisible(True)
        self.plugin_menu.setIcon(QIcon(self.plugin_icon))
        self._modify_svg_path(add=True)

        self.layer_manager = LayerManager(self.project, self.iface)
        # Add an action for running the Material Take-off
        # fmt: off
        # ruff: noqa: E501
        button: str = QCoreApplication.translate("Menu_Button", "Run Material Take-off")
        tool_tip_text: str = QCoreApplication.translate("Menu_ToolTip", "<p><b>Material Take-off for the selected layer</b></p><p><span style='font-weight:normal; font-style:normal;'>The Material Take-off will be calculated for the selected layer. The selected layer needs to be a line layer.</span></p>")
        # fmt: on
        mto_action = self.add_action(
            icon=ICONS.main_menu_run,
            button_text=button,
            callback=self.run_massenermittlung,
            parent=self.iface.mainWindow(),
            add_to_menu=False,  # Will be added to our custom menu
            add_to_toolbar=False,  # Will be added manually based on BUTTON_TYPE
            tool_tip=tool_tip_text,
        )
        self.plugin_menu.addAction(mto_action)

        # Add an action for re-running the Excel ouput
        # fmt: off
        # ruff: noqa: E501
        button: str = QCoreApplication.translate("Menu_Button", "Re-do the Excel output")
        tool_tip_text: str = QCoreApplication.translate("Menu_ToolTip", "<p><b>Re-do the Excel output</b></p><p><span style='font-weight:normal; font-style:normal;'>After manual changes to the Material-Take-off-layer, the Excel output needs to be updated. Select the result layer and click this button. The Excel output will be updated.</span></p>")
        # fmt: on
        excel_action = self.add_action(
            icon=ICONS.main_menu_excel,
            button_text=button,
            callback=self.rerun_excel_output,
            parent=self.iface.mainWindow(),
            add_to_menu=False,  # Will be added to our custom menu
            add_to_toolbar=False,  # Will be added manually based on BUTTON_TYPE
            tool_tip=tool_tip_text,
        )
        self.plugin_menu.addAction(excel_action)

        # Add the fly-out menu to the main "Plugins" menu
        if menu := self.iface.pluginMenu():
            menu.addMenu(self.plugin_menu)
        toolbar_button = QToolButton()
        toolbar_button.setIcon(self.plugin_icon)
        toolbar_button.setToolTip(self.plugin_name)
        toolbar_button.setMenu(self.plugin_menu)
        if PluginContext.is_qt6():
            toolbar_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        else:
            toolbar_button.setPopupMode(QToolButton.InstantPopup)
        toolbar_action = self.iface.addToolBarWidget(toolbar_button)
        self.actions.append(toolbar_action)

    def unload(self) -> None:
        """Plugin unload method.

        Called when the plugin is unloaded according to the plugin QGIS metadata.
        """
        # Remove the translator
        if self.translator:
            QCoreApplication.removeTranslator(self.translator)

        # Remove toolbar icons for all actions
        for action in self.actions:
            self.iface.removeToolBarIcon(action)

        # Remove the plugin menu from the "Plugins" menu.
        if self.plugin_menu:
            self.iface.pluginMenu().removeAction(self.plugin_menu.menuAction())  # type: ignore[]

        # Remove svg search path
        self._modify_svg_path(add=False)

        self.actions.clear()
        self.plugin_menu = None

    def _modify_svg_path(self, *, add: bool) -> None:
        """Add or remove the plugin's SVG icon path from QGIS search paths.

        Args:
            add: If True, adds the path. If False, removes the path.
        """
        current_svg_paths: list[str] = QgsApplication.svgPaths()
        plugin_svg_path: str = str(PluginContext.icons_path())
        path_exists: bool = plugin_svg_path in current_svg_paths

        if add and not path_exists:
            current_svg_paths.append(plugin_svg_path)
        elif not add and path_exists:
            current_svg_paths.remove(plugin_svg_path)

        QgsApplication.setDefaultSvgPaths(current_svg_paths)

    @contextlib.contextmanager
    def _managed_progress_bar(
        self, initial_message: str
    ) -> Generator[tuple[QProgressBar, Callable[[str], None]], None, None]:
        """Create and manage a progress bar in the QGIS message bar.

        This context manager handles the creation, display, and cleanup of a
        progress bar widget.

        Args:
            initial_message: The initial text to display next to the progress bar.

        Yields:
            A tuple containing the QProgressBar instance and a function to update
            the displayed text.
        """
        progress_widget: QgsMessageBarItem | None = None
        progress_bar = QProgressBar()

        if self.msg_bar:
            progress_widget = self.msg_bar.createMessage(initial_message)
            if progress_widget:
                if PluginContext.is_qt6():
                    alignment = (
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                else:
                    alignment = Qt.AlignLeft | Qt.AlignVCenter
                progress_bar.setAlignment(alignment)
                progress_widget.layout().addWidget(progress_bar)
                self.msg_bar.pushWidget(progress_widget, Qgis.Info)

        def update_text(new_message: str) -> None:
            """Update the text of the progress widget."""
            if progress_widget:
                progress_widget.setText(new_message)

        try:
            yield progress_bar, update_text
        finally:
            # Clean up the progress bar from the message bar
            if self.msg_bar and progress_widget:
                self.msg_bar.popWidget(progress_widget)

    def run_massenermittlung(self) -> None:
        """Call the main function."""

        log_debug("... STARTING PLUGIN RUN ... (run_massenermittlung)", icon="✨✨✨")
        temp_point_layer: QgsVectorLayer | None = None
        reprojected_layer: QgsVectorLayer | None = None

        # Re-initialize LayerManager to clear cached layers from previous runs
        self.layer_manager = LayerManager(self.project, self.iface)

        try:
            # fmt: off
            initial_message: str = QCoreApplication.translate("progress_bar", "Performing bulk assessment...")
            # fmt: on
            with self._managed_progress_bar(initial_message) as (
                progress_bar,
                update_text,
            ):
                reprojected_layer = self.layer_manager.selected_layer
                if not reprojected_layer:
                    raise_runtime_error("Reprojected layer is not initialized.")

                # Create a temporary layer for the results
                temp_point_layer = self.layer_manager.create_temporary_point_layer()

                finder = PointOfInterestClassifier(
                    selected_layer=reprojected_layer,
                    temp_point_layer=temp_point_layer,
                )

                # Run the analysis
                finder.find_features(progress_bar, update_text)

                # Copy features from the temporary layer to the final layer
                new_layer: QgsVectorLayer = self.layer_manager.new_layer
                self.layer_manager.copy_features_to_layer(
                    source_layer=temp_point_layer,
                    target_layer=new_layer,
                    progress_bar=progress_bar,
                    pgb_update_text=update_text,
                )

                # --- Remove duplicates from the final layer ---
                DuplicateFilter().remove_duplicates(new_layer)

                # --- Log and display result summary ---
                self.layer_manager.set_layer_style(new_layer)
                summary_single_line: str = create_summary_message(
                    new_layer, reprojected_layer.name(), multiline=False
                )
                summary_multi_line: str = create_summary_message(
                    new_layer, reprojected_layer.name(), multiline=True
                )

                # --- Export results to XLSX for Excel ---
                ExcelExporter().export_results(new_layer, reprojected_layer)

                log_debug(
                    summary_single_line,
                    level=Qgis.Success,
                    file_line_number="✨✨✨",
                    icon="✨✨✨",
                )
                show_message(summary_single_line, level=Qgis.Success, duration=30)

                metadata: QgsLayerMetadata = new_layer.metadata()
                metadata.setAbstract(summary_multi_line)
                new_layer.setMetadata(metadata)

                # --- Set the new layer as active ---
                self.iface.setActiveLayer(new_layer)

        except Exception as e:  # noqa: BLE001
            if e.__class__.__name__ in {"CustomUserError", "CustomRuntimeError"}:
                return

            if tb := e.__traceback__:
                # Get the last frame from the traceback for the origin of the error
                last_frame: traceback.FrameSummary = traceback.extract_tb(tb)[-1]
                filename: str = Path(last_frame.filename).name
                lineno: int | None = last_frame.lineno
                file_line_info: str = f" [{filename}: {lineno}]"
            else:
                file_line_info = " [Unknown location]"

            log_debug(
                f"Unexpected error: {e!s}",
                level=Qgis.Critical,
                file_line_number=file_line_info,
            )
            show_message(
                f"Unexpected error: {e!s}{file_line_info}", level=Qgis.Critical
            )
            return

        finally:
            project: QgsProject | None = QgsProject.instance()

            if temp_point_layer is not None and project is not None:
                project.removeMapLayer(temp_point_layer.id())
                log_debug("Temporary point layer removed.")

            if reprojected_layer is not None and project is not None:
                try:
                    project.removeMapLayer(reprojected_layer.id())
                    log_debug("In-memory copy of the selected layer removed.")
                except RuntimeError:
                    log_debug(
                        "Could not remove reprojected layer (already deleted?)",
                        Qgis.Warning,
                    )

    def rerun_excel_output(self) -> None:
        """Rerun the Excel export for a manually edited result layer.

        This function takes the currently selected result layer (which must be a
        point layer ending with the plugin's suffix) and re-exports its data to
        an Excel file. It also finds the original source line layer to include
        route lengths.
        """
        log_debug("... Rerunning Excel output ...", icon="✨✨✨")
        reprojected_layer_excel: QgsVectorLayer | None = None

        if not self.layer_manager:
            raise_runtime_error("Layer manager is not initialized.")

        try:
            # 1. Get the currently selected layer, which should be the result layer.
            layer_tree: QgsLayerTreeView | None = self.iface.layerTreeView()
            if not layer_tree:
                raise_runtime_error("Could not get layer tree view.")

            selected_nodes: list[QgsLayerTreeNode] = layer_tree.selectedNodes()
            if len(selected_nodes) != 1:
                # fmt: off
                ue_msg: str = QCoreApplication.translate("UserError", "Please select a single result layer to export.")
                # fmt: on
                raise_user_error(ue_msg)

            result_layer = selected_nodes[0].layer()
            if not isinstance(
                result_layer, QgsVectorLayer
            ) or not result_layer.name().endswith(Names.new_layer_suffix):
                # fmt: off
                ue_msg: str = QCoreApplication.translate("UserError", "The selected layer is not a valid result layer from this plugin.")
                # fmt: on
                raise_user_error(ue_msg)

            # 2. Find the original source line layer to get line lengths.
            reprojected_layer_excel = self.layer_manager.find_source_layer(result_layer)

            # 3. Export the results.
            ExcelExporter().export_results(result_layer, reprojected_layer_excel)

            show_message(
                QCoreApplication.translate("summary", "Excel export has been updated."),
                level=Qgis.Success,
                duration=10,
            )

        except (CustomUserError, CustomRuntimeError):
            # Errors are already logged and shown to the user.
            return
        except Exception as e:  # noqa: BLE001
            log_debug(f"An unexpected error occurred during Excel export: {e!s}")
            show_message(
                f"An unexpected error occurred during Excel export: {e!s}",
                level=Qgis.Critical,
            )

        finally:
            # Clean up the temporary reprojected layer
            project: QgsProject | None = QgsProject.instance()
            if reprojected_layer_excel is not None and project is not None:
                project.removeMapLayer(reprojected_layer_excel.id())
                log_debug("In-memory copy of the source layer removed (rerun).")
