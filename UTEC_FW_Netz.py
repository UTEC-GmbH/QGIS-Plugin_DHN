"""***************************************************************************
UTEC FW Netz

***************************************************************************
"""

import configparser
import contextlib
import traceback
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import Qgis, QgsProject, QgsVectorLayer
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import QCoreApplication, QSettings, Qt, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QProgressBar, QToolButton

from . import resources
from .modules import general as ge
from .modules import logs_and_errors as lae
from .modules.poi_classifier import PointOfInterestClassifier

if TYPE_CHECKING:
    from qgis.core import QgsLayerMetadata
    from qgis.gui import QgsMessageBar, QgsMessageBarItem


class UTECFWNetz:
    """QGIS Plugin for renaming and moving layers to a GeoPackage."""

    BUTTON_TYPE = "simple"  # "menu" or "simple"

    def __init__(self, iface: QgisInterface) -> None:
        """Initialize the plugin.

        :param iface: An interface instance that allows interaction with QGIS.
        """

        self.iface: QgisInterface = iface
        self.msg_bar: QgsMessageBar | None = iface.messageBar()
        self.plugin_dir: Path = Path(__file__).parent
        self.actions: list = []
        self.plugin_menu: QMenu | None = None
        self.dlg = None
        self.icon_path: str = ":/compiled_res_FW_Netz/icon.svg"
        self.translator: QTranslator | None = None

        # Read metadata to get the plugin name for UI elements
        self.plugin_name: str = "UTEC FW Netz (dev)"  # Default
        metadata_path: Path = self.plugin_dir / "metadata.txt"
        if metadata_path.exists():
            config = configparser.ConfigParser()
            config.read(metadata_path)
            try:
                self.plugin_name = config.get("general", "name")
            except (configparser.NoSectionError, configparser.NoOptionError):
                lae.log_debug("Could not read name from metadata.txt", Qgis.Warning)

        self.menu: str = self.plugin_name

        # initialize translation
        locale = QSettings().value("locale/userLocale", "en")[:2]
        translator_path: Path = self.plugin_dir / "i18n" / f"{locale}.qm"

        if not translator_path.exists():
            lae.log_debug(f"Translator not found in: {translator_path}", Qgis.Warning)
        else:
            self.translator = QTranslator()
            if self.translator is not None and self.translator.load(
                str(translator_path)
            ):
                QCoreApplication.installTranslator(self.translator)
            else:
                lae.log_debug("Translator could not be installed.", Qgis.Warning)

    def add_action(  # noqa: PLR0913 # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        icon_path: str,
        text: str,
        callback: Callable,
        enabled_flag: bool = True,  # noqa: FBT001, FBT002
        add_to_menu: bool = True,  # noqa: FBT001, FBT002
        add_to_toolbar: bool = True,  # noqa: FBT001, FBT002
        status_tip: str | None = None,
        whats_this: str | None = None,
        parent=None,  # noqa: ANN001
    ) -> QAction:  # type: ignore[]
        """Add a QAction to the plugin's menu and/or toolbar.

        :param icon_path: Path to the icon for the action.
        :param text: Text to display for the action.
        :param callback: Function to call when the action is triggered.
        :param enabled_flag: Whether the action is enabled initially.
        :param add_to_menu: Whether to add the action to the plugin's menu.
        :param add_to_toolbar: Whether to add the action to the QGIS toolbar.
        :param status_tip: Status tip for the action.
        :param whats_this: "What's This?" help text for the action.
        :param parent: Parent widget for the action.
        :returns: The created QAction object.
        """
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        action.setToolTip(text)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            self.iface.addToolBarIcon(action)

        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)

        return action

    def initGui(self) -> None:  # noqa: N802
        """Create the menu entries and toolbar icons for the plugin."""

        # Initialize the resources (icons, etc.)
        resources.qInitResources()

        # Create a menu for the plugin in the "Plugins" menu
        self.plugin_menu = QMenu(self.menu, self.iface.pluginMenu())
        if self.plugin_menu is None:
            lae.raise_runtime_error("Failed to create the plugin menu.")

        self.plugin_menu.setIcon(QIcon(self.icon_path))

        # Add an action for the main functionality
        run_action = self.add_action(
            self.icon_path,
            text=self.plugin_name,
            callback=self.run_plugin,
            parent=self.iface.mainWindow(),
            add_to_menu=False,  # Will be added to our custom menu
            add_to_toolbar=False,  # Will be added manually based on BUTTON_TYPE
            status_tip=self.plugin_name,
            whats_this=f"{self.plugin_name}.",
        )
        self.plugin_menu.addAction(run_action)

        # Add our menu to the main "Plugins" menu
        self.iface.pluginMenu().addMenu(self.plugin_menu)  # type: ignore[]

        if self.BUTTON_TYPE == "menu":
            self.create_toolbar_button(run_action)
        elif self.BUTTON_TYPE == "simple":
            self.iface.addToolBarIcon(run_action)

    def create_toolbar_button(self, run_action: QAction) -> None:  # type: ignore[]
        """Add a toolbutton to the toolbar to show the flyout menu"""
        toolbar_button = QToolButton()
        toolbar_button.setMenu(self.plugin_menu)
        toolbar_button.setDefaultAction(run_action)
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

        self.actions.clear()
        self.plugin_menu = None

        # Unload resources to allow for reloading them
        resources.qCleanupResources()

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
                progress_bar.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
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

    def run_plugin(self) -> None:
        """Call the main function."""

        lae.log_debug("... STARTING PLUGIN RUN ...", icon="✨✨✨")
        temp_point_layer: QgsVectorLayer | None = None
        reprojected_layer: QgsVectorLayer | None = None
        try:
            # fmt: off
            initial_message: str = QCoreApplication.translate("progress_bar", "Performing bulk assessment...")  # noqa: E501
            # fmt: on
            with self._managed_progress_bar(initial_message) as (
                progress_bar,
                update_text,
            ):
                layer_manager = ge.LayerManager()
                reprojected_layer = layer_manager.selected_layer

                # Create a temporary layer for the results
                temp_point_layer = ge.create_temporary_point_layer(
                    layer_manager.project
                )

                finder = PointOfInterestClassifier(
                    selected_layer=reprojected_layer,
                    temp_point_layer=temp_point_layer,
                )

                # Run the analysis
                finder.find_features(progress_bar, update_text)

                # Copy features from the temporary layer to the final layer
                new_layer: QgsVectorLayer = layer_manager.new_layer
                layer_manager.copy_features_to_layer(
                    source_layer=temp_point_layer,
                    target_layer=new_layer,
                    progress_bar=progress_bar,
                    pgb_update_text=update_text,
                )

                # --- Remove duplicates from the final layer ---
                layer_manager.remove_duplicates_from_layer(new_layer)

                # --- Log and display result summary ---
                layer_manager.set_layer_style(new_layer)
                summary_single_line: str = lae.create_summary_message(
                    new_layer, reprojected_layer.name(), multiline=False
                )
                summary_multi_line: str = lae.create_summary_message(
                    new_layer, reprojected_layer.name(), multiline=True
                )

                # --- Export results to XLSX for Excel ---
                layer_manager.export_results(new_layer)

                lae.log_debug(
                    summary_single_line,
                    level=Qgis.Success,
                    file_line_number="✨✨✨",
                    icon="✨✨✨",
                )
                lae.show_message(summary_single_line, level=Qgis.Success, duration=30)

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

            lae.log_debug(
                f"Unexpected error: {e!s}",
                level=Qgis.Critical,
                file_line_number=file_line_info,
            )
            lae.show_message(
                f"Unexpected error: {e!s}{file_line_info}", level=Qgis.Critical
            )
            return

        finally:
            project: QgsProject | None = QgsProject.instance()

            if temp_point_layer is not None and project is not None:
                project.removeMapLayer(temp_point_layer.id())
                lae.log_debug("Temporary point layer removed.")

            if reprojected_layer is not None and project is not None:
                project.removeMapLayer(reprojected_layer.id())
                lae.log_debug("In-memory copy of the selected layer removed.")
