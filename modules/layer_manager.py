"""Module: layer_manager.py

This module contains the LayerManager class.
"""

import contextlib
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLayerTree,
    QgsLayerTreeNode,
    QgsProject,
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
    PipeType,
    QMT_Int,
)
from .context import PluginContext
from .logs_and_errors import log_debug, raise_runtime_error, raise_user_error
from .vector_analysis_tools import FieldNames, VectorAnalysisTools

if TYPE_CHECKING:
    from pathlib import Path

    from qgis.core import QgsMapLayer
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

    def _create_reprojected_layer_structure(
        self, source_layer: QgsVectorLayer
    ) -> QgsVectorLayer:
        """Create the structure (fields, CRS) for the reprojected layer.

        Args:
            source_layer: The source layer to copy structure from.

        Returns:
            A new memory layer with the reprojected structure.
        """
        target_crs: QgsCoordinateReferenceSystem = self.project.crs()
        if source_layer.crs() == target_crs:
            log_debug(
                "Layer CRS matches project CRS. "
                "Creating new layer with filtered fields.",
                Qgis.Success,
            )
        else:
            log_debug(
                f"Layer CRS ({source_layer.crs().authid()}) does not match project CRS "
                f"({target_crs.authid()}). Reprojecting...",
                icon="♻️",
            )

        geometry_type_str: str = QgsWkbTypes.displayString(source_layer.wkbType())
        reprojected_layer = QgsVectorLayer(
            f"{geometry_type_str}?crs={target_crs.authid()}",
            source_layer.name(),
            "memory",
        )

        data_provider: QgsVectorDataProvider | None = reprojected_layer.dataProvider()
        if data_provider is None:
            raise_runtime_error(
                f"Could not get data provider for layer: {reprojected_layer.name()}"
            )

        filtered_fields: list[QgsField] = []
        for field in source_layer.fields():
            if field.type() not in PROBLEMATIC_FIELD_TYPES and field.name() != "fid":
                filtered_fields.append(field)
            else:
                log_debug(
                    f"Skipping problematic field '{field.name()}' of type "
                    f"'{field.typeName()}' during layer reprojection/cloning."
                )

        data_provider.addAttributes(filtered_fields)
        data_provider.addAttributes([QgsField("original_fid", QMT_Int)])
        reprojected_layer.updateFields()
        log_debug(
            f"The in-memory layer has {len(reprojected_layer.fields())} fields "
            f"(the selected layer has {len(source_layer.fields())} fields).",
            icon="🐞",
        )
        return reprojected_layer

    def _create_reprojected_feature(
        self,
        source_feature: QgsFeature,
        target_fields: QgsFields,
        transform: QgsCoordinateTransform,
    ) -> QgsFeature | None:
        """Create a single reprojected feature with mapped attributes.

        Args:
            source_feature: The feature to reproject.
            target_fields: The fields of the target layer.
            transform: The coordinate transform to apply.

        Returns:
            The reprojected feature, or None if reprojection failed.
        """
        try:
            new_feature = QgsFeature()
            new_feature.setFields(target_fields, initAttributes=True)
            new_feature.setAttribute("original_fid", source_feature.id())

            for field in target_fields:
                if source_feature.fieldNameIndex(field.name()) != -1:
                    new_feature.setAttribute(
                        field.name(), source_feature.attribute(field.name())
                    )

            geom: QgsGeometry = source_feature.geometry()
            if geom.transform(transform) != 0:
                log_debug(
                    f"Feature {source_feature.id()} could not be reprojected.",
                    Qgis.Warning,
                )
                return None

            new_feature.setGeometry(geom)

        except (AttributeError, TypeError, ValueError, RuntimeError) as e:
            log_debug(
                f"Could not process feature with ID {source_feature.id()} "
                f"for reprojection: {e!s}",
                Qgis.Warning,
            )
            return None

        else:
            return new_feature

    def _populate_reprojected_layer(
        self, source_layer: QgsVectorLayer, target_layer: QgsVectorLayer
    ) -> None:
        """Reproject features and add them to the target layer.

        Args:
            source_layer: The layer containing original features.
            target_layer: The layer to populate with reprojected features.
        """
        transform = QgsCoordinateTransform(
            source_layer.crs(), self.project.crs(), self.project.transformContext()
        )

        all_ids: list = list(source_layer.allFeatureIds())
        log_debug(f"Found {len(all_ids)} feature IDs in the selected layer.")
        if not all_ids:
            return

        new_features: list[QgsFeature] = []
        for fid in all_ids:
            source_feature: QgsFeature = source_layer.getFeature(fid)
            if new_feature := self._create_reprojected_feature(
                source_feature, target_layer.fields(), transform
            ):
                new_features.append(new_feature)

        log_debug(f"Processed {len(new_features)} features for reprojection.")

        if new_features:
            self._add_features_to_layer(target_layer, new_features)

    def _add_features_to_layer(
        self, target_layer: QgsVectorLayer, new_features: list[QgsFeature]
    ) -> None:
        """Add features to the target layer.

        Args:
            target_layer: The layer to add features to.
            new_features: The list of features to add.
        """
        target_layer.startEditing()
        target_layer.addFeatures(new_features)

        # Add the in-memory layer to the project to ensure
        # it's not garbage collected or invalidated.
        # We don't add it to the layer tree, so it remains invisible.
        self.project.addMapLayer(target_layer, addToLegend=False)
        log_debug(
            f"Added reprojected layer to map registry. "
            f"Feature count: {target_layer.featureCount()}"
        )

        if target_layer.commitChanges():
            log_debug(
                f"Successfully committed {target_layer.featureCount()} "
                "features to reprojected in-memory layer.",
                Qgis.Success,
            )
        else:
            log_debug(
                f"Failed to commit changes to reprojected in-memory layer. "
                f"Feature count: {target_layer.featureCount()}",
                Qgis.Critical,
            )
        target_layer.updateExtents()

    def reproject_layer_to_project_crs(self, layer: QgsVectorLayer) -> QgsVectorLayer:
        """Reproject a vector layer to the project's CRS.

        Creates a new in-memory layer with the same fields and reprojects
        all features from the source layer into it.

        Args:
            layer: The source QgsVectorLayer to reproject.

        Returns:
            A new, reprojected in-memory QgsVectorLayer.
        """
        log_debug("Creating in-memory layer for reprojection and field filtering.")

        # Clear any selection on the layer
        layer.removeSelection()

        reprojected_layer: QgsVectorLayer = self._create_reprojected_layer_structure(
            layer
        )
        self._populate_reprojected_layer(layer, reprojected_layer)

        log_debug(
            f"The selected layer has {layer.featureCount()} features "
            f"and {len(layer.fields())} fields."
            f"The reprojected layer has {reprojected_layer.featureCount()} features "
            f"and {len(reprojected_layer.fields())} fields.",
            icon="🐞",
        )
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

        gpkg_path: Path = PluginContext.project_gpkg()
        new_layer_name: str = f"{self.fix_layer_name(self.selected_layer.name())}{Names.new_fittings_layer_suffix}"

        if existing_layers := self.project.mapLayersByName(new_layer_name):
            self.project.removeMapLayers([layer.id() for layer in existing_layers])

        empty_layer = QgsVectorLayer(
            f"Point?crs={self.project.crs().authid()}", "in_memory_layer", "memory"
        )

        data_provider: QgsVectorDataProvider | None = empty_layer.dataProvider()
        if data_provider is None:
            raise_runtime_error(
                f"Could not get data provider for layer: {empty_layer.name()}"
            )

        fields_to_add: list[QgsField] = [
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewPointLayerFields
        ]
        data_provider.addAttributes(fields_to_add)
        empty_layer.updateFields()

        self._write_layer_to_gpkg(empty_layer, gpkg_path, new_layer_name)
        log_debug(
            f"Empty layer '{new_layer_name}' created in GeoPackage.",
            Qgis.Success,
        )

        root: QgsLayerTree | None = self.project.layerTreeRoot()
        if not root:
            raise_runtime_error("Could not get layer tree root.")

        # Construct the layer URI and create a QgsVectorLayer
        uri: str = f"{gpkg_path!s}|layername={new_layer_name}"
        gpkg_layer = QgsVectorLayer(uri, new_layer_name, "ogr")

        if not gpkg_layer.isValid():
            raise_runtime_error(
                f"Could not find layer '{new_layer_name}' in GeoPackage '{gpkg_path}'"
            )

        # Add the layer to the project registry first, but not the layer tree
        self.project.addMapLayer(gpkg_layer, addToLegend=False)
        # Then, insert it at the top of the layer tree
        root.insertLayer(0, gpkg_layer)

        log_debug(
            f"Added layer '{gpkg_layer.name()}' from GeoPackage to the project.",
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
        gpkg_path: Path = PluginContext.project_gpkg()
        base_name: str = self.fix_layer_name(self.selected_layer.name())
        new_layer_name: str = f"{base_name}{Names.new_pipe_layer_suffix}"

        if existing_layers := self.project.mapLayersByName(new_layer_name):
            self.project.removeMapLayers([layer.id() for layer in existing_layers])

        # 1. Create a temporary in-memory layer with the desired structure
        temp_pipe_layer = QgsVectorLayer(
            f"LineString?crs={self.project.crs().authid()}",
            "temp_pipe_layer",
            "memory",
        )
        data_provider: QgsVectorDataProvider | None = temp_pipe_layer.dataProvider()
        if data_provider is None:
            raise_runtime_error("Could not create data provider for temp pipe layer.")

        fields_to_add: list[QgsField] = [
            QgsField(field_enum.field_name, field_enum.data_type)
            for field_enum in NewLineLayerFields
        ]
        data_provider.addAttributes(fields_to_add)
        temp_pipe_layer.updateFields()

        # 2. Find source field names for dimensions and load
        found_fields: FieldNames = VectorAnalysisTools.find_layer_fields(
            self.selected_layer
        )
        dim_field_name: str | None = found_fields.dim
        load_field_name: str | None = found_fields.load

        # 3. Populate the temporary layer with features and mapped attributes
        new_features: list[QgsFeature] = []
        for source_feature in self.selected_layer.getFeatures():
            new_feature = QgsFeature(temp_pipe_layer.fields())
            new_feature.setGeometry(source_feature.geometry())

            new_feature.setAttribute(
                NewLineLayerFields.org_id.field_name,
                source_feature.attribute("original_fid"),
            )
            if dim_field_name:
                new_feature.setAttribute(
                    NewLineLayerFields.dim.field_name,
                    source_feature.attribute(dim_field_name),
                )
            if load_field_name:
                new_feature.setAttribute(
                    NewLineLayerFields.load.field_name,
                    source_feature.attribute(load_field_name),
                )

            new_feature.setAttribute(
                NewLineLayerFields.length.field_name, source_feature.geometry().length()
            )

            new_feature.setAttribute(
                NewLineLayerFields.type.field_name, PipeType.MAIN.translated
            )
            new_features.append(new_feature)

        temp_pipe_layer.startEditing()
        temp_pipe_layer.addFeatures(new_features)
        temp_pipe_layer.commitChanges()

        # 4. Write the temporary layer to the GeoPackage
        self._write_layer_to_gpkg(temp_pipe_layer, gpkg_path, new_layer_name)

        # 5. Load the new layer from the GeoPackage and add to project
        uri: str = f"{gpkg_path!s}|layername={new_layer_name}"
        gpkg_layer = QgsVectorLayer(uri, new_layer_name, "ogr")

        if not gpkg_layer.isValid():
            raise_runtime_error(
                f"Could not find layer '{new_layer_name}' in GeoPackage '{gpkg_path}'"
            )

        if root := self.project.layerTreeRoot():
            self.project.addMapLayer(gpkg_layer, addToLegend=False)
            root.insertLayer(1, gpkg_layer)

        log_debug(
            f"Created pipe layer copy '{gpkg_layer.name()}' in GeoPackage.",
            Qgis.Success,
        )

        return gpkg_layer

    def _write_layer_to_gpkg(
        self,
        layer_to_write: QgsVectorLayer,
        gpkg_path: "Path",
        layer_name: str,
    ) -> None:
        """Write a vector layer to a GeoPackage file.

        Args:
            layer_to_write: The layer to write.
            gpkg_path: The path to the output GeoPackage file.
            layer_name: The name of the layer within the GeoPackage.

        Raises:
            CustomRuntimeError: If writing the layer fails.
        """
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer_name
        if gpkg_path.exists():
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        else:
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile

        error: tuple = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer_to_write,
            str(gpkg_path),
            self.project.transformContext(),
            options,
        )
        if error[0] != QgsVectorFileWriter.WriterError.NoError:
            raise_runtime_error(
                f"Failed to create layer '{layer_name}' in '{gpkg_path}'. "
                f"Error: {error[1]}"
            )

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

        qml_path: Path = PluginContext.resources_path() / "DHN_style.qml"

        layer.loadNamedStyle(str(qml_path))

        layer.triggerRepaint()
        log_debug("Layer style set.", Qgis.Success)
