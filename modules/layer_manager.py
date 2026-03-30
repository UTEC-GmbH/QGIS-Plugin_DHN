"""Module: layer_manager.py

This module contains the LayerManager class.
"""

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRenderer,
    QgsField,
    QgsFields,
    QgsLayerTree,
    QgsLayerTreeGroup,
    QgsLayerTreeNode,
    QgsProject,
    QgsRandomColorRamp,
    QgsRendererCategory,
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


@dataclass(frozen=True)
class SourceLayers:
    """Holds a pair of related source layers for processing.

    Attributes:
        pipe: The mandatory line layer representing the pipe network.
        building: The optional polygon layer representing buildings.
    """

    pipe: QgsVectorLayer
    building: QgsVectorLayer | None


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
        self._source_layers: SourceLayers | None = None
        self._new_point_layer: QgsVectorLayer | None = None
        self._new_line_layer: QgsVectorLayer | None = None

    @property
    def pipe_layer(self) -> QgsVectorLayer:
        """Get the reprojected pipe (line) layer.

        Returns:
            The pipe QgsVectorLayer.
        """
        if self._source_layers is None:
            self.initialize_layers()

        if (layers := self._source_layers) is None:
            raise_runtime_error("Source layers could not be initialized.")

        return layers.pipe

    @property
    def building_layer(self) -> QgsVectorLayer | None:
        """Get the reprojected building (polygon) layer.

        Returns:
            The building QgsVectorLayer or None if not available.
        """
        if self._source_layers is None:
            self.initialize_layers()

        return None if (layers := self._source_layers) is None else layers.building

    def initialize_layers(self) -> None:
        """Initialize and reproject the selected pipe and building layers."""
        self._source_layers = self._identify_and_reproject_layers()

    @property
    def new_point_layer(self) -> QgsVectorLayer:
        """Get the new point layer created by the plugin.

        Returns:
            The new point QgsVectorLayer.

        Raises:
            CustomRuntimeError: If the new point layer has not been set or initialized.
        """
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
        """Get the new pipe layer created by the plugin.

        Returns:
            The new pipe QgsVectorLayer.

        Raises:
            CustomRuntimeError: If the new pipe layer has not been set or initialized.
        """
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
        uri: str = (
            f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={target_crs.authid()}"
        )
        reprojected_layer = QgsVectorLayer(uri, layer.name(), "memory")
        if not (dp := reprojected_layer.dataProvider()):
            raise_runtime_error(
                f"Could not get data provider for layer: {reprojected_layer.name()}"
            )

        # Determine the required field prefix based on geometry type
        geom_type: QgsWkbTypes.GeometryType = layer.geometryType()
        prefix: str = ""
        if geom_type == QgsWkbTypes.LineGeometry:
            prefix = "p_"
        elif geom_type == QgsWkbTypes.PolygonGeometry:
            prefix = "b_"

        layer_fields: QgsFields = layer.fields()
        # Only apply prefix filtering if the layer uses the p_/b_ convention
        # to avoid stripping fields from standard layers.
        use_filtering: bool = False
        if prefix:
            use_filtering = any(
                field.name().startswith(("p_", "b_")) for field in layer_fields
            )

        # Add fields
        fields: list[QgsField] = [
            field
            for field in layer_fields
            if field.type() not in PROBLEMATIC_FIELD_TYPES
            and field.name() != "fid"
            and (not use_filtering or field.name().startswith(prefix))
        ]
        dp.addAttributes([*fields, QgsField("original_fid", QMT_Int)])
        reprojected_layer.updateFields()

        # Reproject features
        transform = QgsCoordinateTransform(
            layer.crs(), target_crs, self.project.transformContext()
        )
        new_features: list[QgsFeature] = []
        target_fields: QgsFields = reprojected_layer.fields()

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

    def _identify_and_reproject_layers(self) -> SourceLayers:
        """Identify the pipe and building layers from the selection and tree.

        Returns:
            A LayerPair containing the reprojected pipe and building layers.

        Raises:
            CustomUserError: If no valid selection is found or a pipe layer is missing.
        """
        layer_tree: QgsLayerTreeView | None = self.iface.layerTreeView()
        if not layer_tree:
            raise_runtime_error("Could not get layer tree view.")

        selected_nodes: list[QgsLayerTreeNode] = layer_tree.selectedNodes()
        if not selected_nodes:
            # fmt: off
            raise_user_error(QCoreApplication.translate("UserError", "No layer selected."))  # noqa: E501
            # fmt: on

        layers: list[QgsVectorLayer] = [
            node.layer()
            for node in selected_nodes
            if isinstance(node.layer(), QgsVectorLayer)
        ]

        pipe_raw: QgsVectorLayer | None = None
        building_raw: QgsVectorLayer | None = None

        # 1. Sort selected layers by geometry type
        for layer in layers:
            if layer.geometryType() == QgsWkbTypes.LineGeometry:
                pipe_raw = layer
            elif layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                building_raw = layer

        # 2. If one is missing, try to find the companion by name
        if pipe_raw and not building_raw:
            building_raw = self._find_companion(pipe_raw, QgsWkbTypes.PolygonGeometry)
        elif building_raw and not pipe_raw:
            pipe_raw = self._find_companion(building_raw, QgsWkbTypes.LineGeometry)

        # 3. Validation
        if not pipe_raw:
            # fmt: off
            msg: str = QCoreApplication.translate("UserError", "A pipe (line) layer is required for the analysis.")  # noqa: E501
            # fmt: on
            raise_user_error(msg)

        # 4. Reproject
        pipe_reprojected: QgsVectorLayer = self.reproject_layer_to_project_crs(pipe_raw)
        building_reprojected: QgsVectorLayer | None = (
            self.reproject_layer_to_project_crs(building_raw) if building_raw else None
        )

        return SourceLayers(pipe=pipe_reprojected, building=building_reprojected)

    def _find_companion(
        self, base_layer: QgsVectorLayer, target_geom: QgsWkbTypes.GeometryType
    ) -> QgsVectorLayer | None:
        """Find a layer with the same name but different geometry in the project.

        Args:
            base_layer: The layer to find a companion for.
            target_geom: The expected geometry type of the companion.

        Returns:
            The found QgsVectorLayer or None.
        """
        name: str = base_layer.name()
        for layer in self.project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or layer.id() == base_layer.id():
                continue

            if (
                layer.name() == name
                and layer.geometryType() == target_geom
                and self._verify_companion_attributes(layer, target_geom)
            ):
                return layer

        return None

    def _verify_companion_attributes(
        self, layer: QgsVectorLayer, target_geom: QgsWkbTypes.GeometryType
    ) -> bool:
        """Verify if the layer follows the b_ / p_ field prefix convention.

        Args:
            layer: The layer to check.
            target_geom: The geometry type to decide which prefix to look for.

        Returns:
            True if the convention is detected, False otherwise.
        """
        field_names: list[str] = [f.name() for f in layer.fields()]
        has_b: bool = any(n.startswith("b_") for n in field_names)
        has_p: bool = any(n.startswith("p_") for n in field_names)

        if not (has_b and has_p):
            return True  # Fallback to name match if prefixes aren't used

        # For polygons, b_ fields should have data, p_ should be NULL (heuristic check)
        # We just check if the prefixes exist for now as requested.
        if target_geom == QgsWkbTypes.PolygonGeometry:
            return has_b
        return has_p if target_geom == QgsWkbTypes.LineGeometry else True

    def create_point_layer(self) -> QgsVectorLayer:
        """Create an empty point layer in the project's GeoPackage.

        Returns:
            The newly created QgsVectorLayer.
        """
        log_debug("Creating new layer in GeoPackage...")
        base_name: str = self.fix_layer_name(self.pipe_layer.name())
        fields_to_add: list[QgsField] = [
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewPointLayerFields
        ]

        empty_layer: QgsVectorLayer = self._create_memory_layer(
            "in_memory_layer", "Point", fields_to_add
        )
        gpkg_layer: QgsVectorLayer = self._save_to_gpkg_and_load(
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
        """Create a copy of the pipe layer to store network properties.

        Returns:
             The newly created QgsVectorLayer.
        """
        log_debug("Creating clean pipe layer copy in GeoPackage...")
        base_name: str = self.fix_layer_name(self.pipe_layer.name())
        fields_to_add: list[QgsField] = [
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewLineLayerFields
        ]
        temp_pipe_layer: QgsVectorLayer = self._create_memory_layer(
            "temp_pipe_layer", "LineString", fields_to_add
        )

        # 2. Find source field names for dimensions and load
        found_fields: FieldNames = VectorAnalysisTools.find_layer_fields(
            self.pipe_layer
        )

        # 3. Populate the temporary layer with features and mapped attributes
        merger = LineMerger(self.new_point_layer)
        new_features: list[QgsFeature] = merger.create_merged_line_features(
            self.pipe_layer,
            temp_pipe_layer.fields(),
            found_fields,
        )

        temp_pipe_layer.startEditing()
        temp_pipe_layer.addFeatures(new_features)
        temp_pipe_layer.commitChanges()

        gpkg_layer: QgsVectorLayer = self._save_to_gpkg_and_load(
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
        """Create an in-memory layer with the specified fields.

        Args:
            name: The name of the layer.
            geometry_type: The geometry type (e.g., 'Point', 'LineString').
            fields: A list of QgsField objects to add to the layer.

        Returns:
            The created in-memory QgsVectorLayer.
        """
        uri: str = f"{geometry_type}?crs={self.project.crs().authid()}"
        layer = QgsVectorLayer(uri, name, "memory")
        if dp := layer.dataProvider():
            dp.addAttributes(fields)
            layer.updateFields()
        return layer

    def _save_to_gpkg_and_load(
        self, memory_layer: QgsVectorLayer, base_name: str, suffix: str
    ) -> QgsVectorLayer:
        """Save a memory layer to the project GPKG and load it back.

        Args:
            memory_layer: The in-memory layer to save.
            base_name: The base name for the new layer.
            suffix: The suffix to append to the layer name.

        Returns:
            The loaded QgsVectorLayer from the GeoPackage.

        Raises:
            CustomRuntimeError: If writing to the GeoPackage fails or the layer
                cannot be loaded.
        """
        gpkg_path: Path = PluginContext.project_gpkg()
        new_layer_name: str = f"{base_name}{suffix}"

        if existing := self.project.mapLayersByName(new_layer_name):
            self.project.removeMapLayers([layer.id() for layer in existing])

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

        uri: str = f"{gpkg_path}|layername={new_layer_name}"
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
        if (data_provider := temp_layer.dataProvider()) is None:
            raise_runtime_error("Could not create data provider for temporary layer.")

        fields_to_add: list[QgsField] = [
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewPointLayerFields
        ]
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
                idx: int = target_fields.indexOf(field.name())
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

        renderer: QgsFeatureRenderer | None = layer.renderer()
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
