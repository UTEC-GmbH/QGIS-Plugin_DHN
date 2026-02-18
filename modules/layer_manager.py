"""Module: layer_manager.py

This module contains the LayerManager class.
"""

import contextlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsField,
    QgsLayerTree,
    QgsLayerTreeGroup,
    QgsLayerTreeNode,
    QgsProject,
    QgsRandomColorRamp,
    QgsRendererCategory,
    QgsVectorDataProvider,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QProgressBar

from .constants import (
    PROBLEMATIC_FIELD_TYPES,
    Colours,
    Names,
    NewLineLayerFields,
    NewPointLayerFields,
    QMT_Int,
)
from .context import PluginContext
from .line_merger import LineMerger
from .logs_and_errors import log_debug, raise_runtime_error, raise_user_error
from .vector_analysis_tools import FieldNames, VectorAnalysisTools

if TYPE_CHECKING:
    from pathlib import Path

    from qgis.core import QgsFields, QgsMapLayer
    from qgis.gui import QgsLayerTreeView


class LayerManager:
    """A class to manage the layers used in the plugin."""

    def __init__(self, project: QgsProject, iface: QgisInterface) -> None:
        """Initialize the layer manager class.

        Args:
            project: The current QGIS project instance.
            iface: The QGIS interface instance.
        """
        self.project: QgsProject = project
        self.iface: QgisInterface = iface
        self._selected_layer: QgsVectorLayer | None = None
        self._new_point_layer: QgsVectorLayer | None = None
        self._new_line_layer: QgsVectorLayer | None = None

    @property
    def selected_layer(self) -> QgsVectorLayer:
        """The selected layer in the plugin."""
        if self._selected_layer is None:
            self.initialize_selected_layer()
        if self._selected_layer is None:
            raise_runtime_error("Selected layer is not set.")
        return self._selected_layer

    @selected_layer.setter
    def selected_layer(self, layer: QgsVectorLayer) -> None:
        self._selected_layer = layer

    def initialize_selected_layer(self) -> None:
        """Initialize the selected layer."""
        self._selected_layer = self.get_selected_layer()

    @property
    def new_point_layer(self) -> QgsVectorLayer:
        """The new layer created by the plugin."""
        if self._new_point_layer is None:
            self.initialize_new_layer()
        if self._new_point_layer is None:
            raise_runtime_error("New layer is not set.")
        return self._new_point_layer

    @new_point_layer.setter
    def new_point_layer(self, layer: QgsVectorLayer) -> None:
        self._new_point_layer = layer

    def initialize_new_layer(self) -> None:
        """Initialize the new layer."""
        self.new_point_layer = self.create_point_layer()

    @property
    def new_line_layer(self) -> QgsVectorLayer:
        """The new pipe layer created by the plugin."""
        if self._new_line_layer is None:
            self.initialize_new_pipe_layer()
        if self._new_line_layer is None:
            raise_runtime_error("New pipe layer is not set.")
        return self._new_line_layer

    @new_line_layer.setter
    def new_line_layer(self, layer: QgsVectorLayer) -> None:
        self._new_line_layer = layer

    def initialize_new_pipe_layer(self) -> None:
        """Initialize the new pipe layer."""
        self.new_line_layer = self.create_line_layer()

    def _get_or_create_group(self) -> QgsLayerTreeGroup:
        """Get or create a layer tree group.

        Returns:
            The found or created QgsLayerTreeGroup.
        """
        root: QgsLayerTree | None = self.project.layerTreeRoot()
        if not root:
            raise_runtime_error("Could not get layer tree root.")

        group_name: str = Names.layer_group
        group: QgsLayerTreeGroup | None = root.findGroup(group_name)
        if group is None:
            group = root.insertGroup(0, group_name)
            if group is None:
                raise_runtime_error(f"Could not create group '{group_name}'.")
            group.setExpanded(True)

        return group

    def fix_layer_name(self, name: str) -> str:
        """Fix encoding mojibake and sanitize a string to be a valid layer name.

        This function first attempts to fix a common mojibake encoding issue,
        where a UTF-8 string was incorrectly decoded as cp1252
        (for example: 'Ãœ' becomes 'Ü').
        It then sanitizes the string to remove or replace characters
        that might be problematic in layer names,
        especially for file-based formats or databases.

        Args:
            name: The potentially garbled and raw layer name.

        Returns:
            A fixed and sanitized version of the name.
        """
        fixed_name: str = name
        with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
            fixed_name = name.encode("cp1252").decode("utf-8")

        # Remove or replace problematic characters
        sanitized_name: str = re.sub(r'[<>:"/\\|?*,]+', "_", fixed_name)

        return sanitized_name

    def reproject_layer_to_project_crs(self, layer: QgsVectorLayer) -> QgsVectorLayer:
        """Reproject a vector layer to the project's CRS.

        Creates a new in-memory layer with the same fields and reprojects
        all features from the source layer into it.

        Args:
            layer: The source QgsVectorLayer to reproject.

        Returns:
            A new, reprojected in-memory QgsVectorLayer.
        """
        target_crs: QgsCoordinateReferenceSystem = self.project.crs()
        if layer.crs() != target_crs:
            log_debug(
                f"Layer CRS ({layer.crs().authid()}) does not match project CRS "
                f"({target_crs.authid()}). Reprojecting...",
                icon="♻️",
            )

        # Clear any selection on the layer
        layer.removeSelection()

        # Create memory layer
        uri = f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={target_crs.authid()}"
        reprojected_layer = QgsVectorLayer(uri, layer.name(), "memory")
        dp = reprojected_layer.dataProvider()
        if not dp:
            raise_runtime_error(
                f"Could not get data provider for layer: {reprojected_layer.name()}"
            )

        # Add fields
        fields = [
            f
            for f in layer.fields()
            if f.type() not in PROBLEMATIC_FIELD_TYPES and f.name() != "fid"
        ]
        dp.addAttributes(fields + [QgsField("original_fid", QMT_Int)])
        reprojected_layer.updateFields()

        # Reproject features
        transform = QgsCoordinateTransform(
            layer.crs(), target_crs, self.project.transformContext()
        )
        new_features = []
        target_fields = reprojected_layer.fields()

        for feat in layer.getFeatures():
            new_feat = QgsFeature(target_fields)
            geom = feat.geometry()
            if geom.transform(transform) == 0:
                new_feat.setGeometry(geom)
                new_feat.setAttribute("original_fid", feat.id())
                # Map attributes
                for field in fields:
                    idx = feat.fieldNameIndex(field.name())
                    if idx != -1:
                        new_feat.setAttribute(field.name(), feat.attribute(idx))
                new_features.append(new_feat)
            else:
                log_debug(
                    f"Feature {feat.id()} could not be reprojected.", Qgis.Warning
                )

        reprojected_layer.startEditing()
        reprojected_layer.addFeatures(new_features)
        reprojected_layer.commitChanges()

        # Add to project (invisible)
        self.project.addMapLayer(reprojected_layer, addToLegend=False)

        return reprojected_layer

    def get_selected_layer(self) -> QgsVectorLayer:
        """Collect the selected layer in the QGIS layer tree view and reprojects it.

        Returns:
            The selected and reprojected QgsVectorLayer object.

        Raises:
            CustomUserError: If no layer is selected, multiple layers are selected,
                or the selected layer is not a line vector layer.
            CustomRuntimeError: If the layer tree view cannot be accessed.
        """
        layer_tree: QgsLayerTreeView | None = self.iface.layerTreeView()
        if not layer_tree:
            raise_runtime_error("Could not get layer tree view.")

        selected_nodes: list[QgsLayerTreeNode] = layer_tree.selectedNodes()
        if len(selected_nodes) > 1:
            # fmt: off
            raise_user_error(QCoreApplication.translate("UserError", "Multiple layers selected."))  # noqa: E501
            # fmt: on
        if not selected_nodes:
            # fmt: off
            raise_user_error(QCoreApplication.translate("UserError", "No layer selected."))  # noqa: E501
            # fmt: on

        selected_node: QgsLayerTreeNode = next(iter(selected_nodes))
        if not selected_node.layer():
            # fmt: off
            raise_user_error(QCoreApplication.translate("UserError", "Selected node is not a layer."))  # noqa: E501
            # fmt: on

        selected_layer = selected_node.layer()
        if not isinstance(selected_layer, QgsVectorLayer):
            # fmt: off
            raise_user_error(QCoreApplication.translate("UserError", "Selected layer is not a vector layer."))  # noqa: E501
            # fmt: on

        if selected_layer.geometryType() != QgsWkbTypes.LineGeometry:
            # fmt: off
            raise_user_error(QCoreApplication.translate("UserError", "The selected layer is not a line layer."))  # noqa: E501
            # fmt: on

        # Reproject the layer to the project's CRS
        return self.reproject_layer_to_project_crs(selected_layer)

    def create_point_layer(self) -> QgsVectorLayer:
        """Create an empty point layer in the project's GeoPackage.

        Returns:
            The newly created QgsVectorLayer.
        """
        log_debug("Creating new layer in GeoPackage...")
        base_name: str = self.fix_layer_name(self.selected_layer.name())
        fields_to_add: list[QgsField] = [
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewPointLayerFields
        ]

        empty_layer = self._create_memory_layer(
            "in_memory_layer", "Point", fields_to_add
        )
        gpkg_layer = self._save_to_gpkg_and_load(
            empty_layer, base_name, Names.new_fittings_layer_suffix
        )
        self._get_or_create_group().insertLayer(0, gpkg_layer)

        log_debug(
            f"Created point layer '{gpkg_layer.name()}' in GeoPackage.",
            Qgis.Success,
        )
        self.set_point_layer_style(gpkg_layer)

        return gpkg_layer

    def create_line_layer(self) -> QgsVectorLayer:
        """Create a copy of the selected layer to store pipe properties.

        This creates a new line layer in the project's GeoPackage. The new layer
        contains all geometries from the selected layer but has a cleaned-up
        attribute table defined by the `NewLineLayerFields` enum.

        Returns:
             The newly created QgsVectorLayer.
        """
        log_debug("Creating clean pipe layer copy in GeoPackage...")
        base_name: str = self.fix_layer_name(self.selected_layer.name())
        fields_to_add: list[QgsField] = [
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewLineLayerFields
        ]
        temp_pipe_layer = self._create_memory_layer(
            "temp_pipe_layer", "LineString", fields_to_add
        )

        # 2. Find source field names for dimensions and load
        found_fields: FieldNames = VectorAnalysisTools.find_layer_fields(
            self.selected_layer
        )
        dim_field_name: str | None = found_fields.dim
        load_field_name: str | None = found_fields.load

        # 3. Populate the temporary layer with features and mapped attributes
        merger = LineMerger(self.new_point_layer)
        new_features: list[QgsFeature] = merger.create_merged_line_features(
            self.selected_layer,
            temp_pipe_layer.fields(),
            dim_field_name,
            load_field_name,
        )

        temp_pipe_layer.startEditing()
        temp_pipe_layer.addFeatures(new_features)
        temp_pipe_layer.commitChanges()

        gpkg_layer = self._save_to_gpkg_and_load(
            temp_pipe_layer, base_name, Names.new_pipe_layer_suffix
        )
        self._get_or_create_group().insertLayer(1, gpkg_layer)

        log_debug(
            f"Created pipe layer copy '{gpkg_layer.name()}' in GeoPackage.",
            Qgis.Success,
        )
        self.set_line_layer_style(gpkg_layer)

        return gpkg_layer

    def _create_memory_layer(
        self, name: str, geometry_type: str, fields: list[QgsField]
    ) -> QgsVectorLayer:
        """Create an in-memory layer with the specified fields."""
        uri = f"{geometry_type}?crs={self.project.crs().authid()}"
        layer = QgsVectorLayer(uri, name, "memory")
        if dp := layer.dataProvider():
            dp.addAttributes(fields)
            layer.updateFields()
        return layer

    def _save_to_gpkg_and_load(
        self, memory_layer: QgsVectorLayer, base_name: str, suffix: str
    ) -> QgsVectorLayer:
        """Save a memory layer to the project GPKG and load it back."""
        gpkg_path = PluginContext.project_gpkg()
        new_layer_name = f"{base_name}{suffix}"

        if existing := self.project.mapLayersByName(new_layer_name):
            self.project.removeMapLayers([l.id() for l in existing])

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = new_layer_name
        if gpkg_path.exists():
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        else:
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

        error: tuple = QgsVectorFileWriter.writeAsVectorFormatV3(
            memory_layer,
            str(gpkg_path),
            self.project.transformContext(),
            options,
        )
        if error[0] != QgsVectorFileWriter.WriterError.NoError:
            raise_runtime_error(
                f"Failed to create layer '{new_layer_name}' in '{gpkg_path}'. "
                f"Error: {error[1]}"
            )

        uri = f"{gpkg_path}|layername={new_layer_name}"
        gpkg_layer = QgsVectorLayer(uri, new_layer_name, "ogr")

        if not gpkg_layer.isValid():
            raise_runtime_error(
                f"Could not find layer '{new_layer_name}' in GeoPackage '{gpkg_path}'"
            )

        self.project.addMapLayer(gpkg_layer, addToLegend=False)
        return gpkg_layer

    def create_temporary_point_layer(self) -> QgsVectorLayer:
        """Create a temporary in-memory point layer with the standard result fields.

        Returns:
            The created temporary QgsVectorLayer.
        """
        temp_layer = QgsVectorLayer(
            f"Point?crs={self.project.crs().authid()}",
            "temporary_point_layer",
            "memory",
        )
        data_provider: QgsVectorDataProvider | None = temp_layer.dataProvider()
        if data_provider is None:
            raise_runtime_error("Could not create data provider for temporary layer.")

        fields_to_add: list[QgsField] = []
        fields_to_add.extend(
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewPointLayerFields
        )
        data_provider.addAttributes(fields_to_add)

        temp_layer.updateFields()

        log_debug(
            f"Temporary point layer with {len(temp_layer.fields())} fields "
            f" and {temp_layer.featureCount()} features created.",
            Qgis.Success,
        )

        return temp_layer

    def find_source_layer(self, result_layer: QgsVectorLayer) -> QgsVectorLayer:
        """Find the original source layer based on the result layer's name.

        This method is used when re-running an export. It derives the source layer's
        name from the result layer, finds it in the project, and returns it
        after reprojection.

        Args:
            result_layer: The result layer (e.g., 'MyLines - DHN').

        Returns:
            The reprojected source layer.

        Raises:
            CustomUserError: If the source layer cannot be found.
        """
        source_layer_name: str = result_layer.name().removesuffix(
            Names.new_fittings_layer_suffix
        )
        source_layers: list[QgsMapLayer] = self.project.mapLayersByName(
            source_layer_name
        )

        if not source_layers:
            raise_runtime_error(
                "Could not find the original source layer for the export."
            )

        # Assume the first found layer is the correct one
        source_layer: QgsVectorLayer = source_layers[0]

        # Reproject and return it
        return self.reproject_layer_to_project_crs(source_layer)

    def copy_features_to_layer(
        self,
        source_layer: QgsVectorLayer,
        target_layer: QgsVectorLayer,
        progress_bar: QProgressBar,
        pgb_update_text: Callable[[str], None],
    ) -> None:
        """Copy features from a source layer to a target layer.

        This method handles the editing session, progress reporting, and attribute
        mapping between the two layers.

        Args:
            source_layer: The temporary layer to copy features from.
            target_layer: The final layer to copy features to.
            progress_bar: The QProgressBar instance to update.
            pgb_update_text: A function to update the progress bar's text.
        """
        if not target_layer.startEditing():
            raise_runtime_error("Failed to start editing the new layer.")

        feature_count: int = source_layer.featureCount()
        progress_bar.setMaximum(feature_count)
        progress_bar.setValue(0)
        # fmt: off
        pgb_update_text(QCoreApplication.translate("progress_bar", "Writing results to new layer..."))  # noqa: E501
        # fmt: on

        log_debug(
            f"Trying to add {feature_count} features "
            f"from '{source_layer.name()}' to '{target_layer.name()}'."
        )

        target_fields: QgsFields = target_layer.fields()
        for i, feature in enumerate(source_layer.getFeatures()):
            new_feature = QgsFeature(target_fields)
            new_feature.setGeometry(feature.geometry())
            for field in feature.fields():
                # Copy attribute if a field with the same name exists in the target
                idx = target_fields.indexOf(field.name())
                if idx != -1:
                    new_feature.setAttribute(idx, feature.attribute(field.name()))
            target_layer.addFeature(new_feature)
            progress_bar.setValue(i + 1)

        if not target_layer.commitChanges():
            raise_runtime_error("Failed to commit features to new layer.")

        log_debug(
            f"After editing, '{target_layer.name()}' has "
            f"{target_layer.featureCount()} features."
        )

    def set_point_layer_style(self, layer: QgsVectorLayer) -> None:
        """Set the layer style from a QML file.

        Args:
            layer: The layer to apply the style to.
        """

        variables: dict[str, str] = {
            "colour_questionable": Colours.questionable,
            "colour_house": Colours.house,
            "colour_t_piece": Colours.t_piece,
            "colour_reducer": Colours.reducer,
            "colour_bend": Colours.bend,
        }

        for name, value in variables.items():
            QgsExpressionContextUtils.setLayerVariable(layer, name, value)

        qml_path: Path = PluginContext.resources_path() / "point_style.qml"

        layer.loadNamedStyle(str(qml_path))

        layer.triggerRepaint()
        log_debug("Layer style set.", Qgis.Success)

    def set_line_layer_style(self, layer: QgsVectorLayer) -> None:
        """Set the layer style from a QML file for the line layer.

        Args:
            layer: The layer to apply the style to.
        """
        qml_path: Path = PluginContext.resources_path() / "line_style.qml"

        layer.loadNamedStyle(str(qml_path))

        renderer = layer.renderer()
        if isinstance(renderer, QgsCategorizedSymbolRenderer):
            # Preserve the source symbol and color ramp from the QML style
            source_symbol = renderer.sourceSymbol()
            source_ramp = renderer.sourceColorRamp()

            field_name: str = NewLineLayerFields.branch.field_name
            renderer.setClassAttribute(field_name)

            field_index: int = layer.fields().lookupField(field_name)
            if field_index != -1:
                unique_values: set = layer.uniqueValues(field_index)
                categories: list[QgsRendererCategory] = []

                for value in sorted(unique_values, key=str):
                    if value in (None, ""):
                        continue
                    symbol = source_symbol.clone() if source_symbol else None
                    category = QgsRendererCategory(value, symbol, str(value))
                    categories.append(category)

                renderer.deleteAllCategories()
                for category in categories:
                    renderer.addCategory(category)

                if not source_ramp:
                    source_ramp = QgsRandomColorRamp()

                renderer.updateColorRamp(source_ramp)

            if layer_tree := self.iface.layerTreeView():
                layer_tree.refreshLayerSymbology(layer.id())

        layer.triggerRepaint()
        log_debug("Line layer style set.", Qgis.Success)
