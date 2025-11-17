"""Module: general.py

This module contains general functions.
"""

import contextlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from osgeo import ogr
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsLayerTree,
    QgsLayerTreeNode,
    QgsProject,
    QgsVectorDataProvider,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.PyQt.QtWidgets import QProgressBar
from qgis.utils import iface

from modules import constants as cont
from modules.logs_and_errors import log_debug, raise_runtime_error, raise_user_error

if TYPE_CHECKING:
    from qgis.core import QgsGeometry
    from qgis.gui import QgisInterface, QgsLayerTreeView


def get_current_project() -> QgsProject:
    """Return the current QGIS project instance, or raise an exception.

    If no project is open, an error message is logged.

    Returns:
        The current QGIS project instance.

    Raises:
        CustomUserError: If no QGIS project is currently open.
    """
    project: QgsProject | None = QgsProject.instance()
    if project is None:
        # fmt: off
        raise_user_error(QCoreApplication.translate("UserError", "No QGIS project is currently open."))  # noqa: E501
        # fmt: on

    return project


def create_temporary_point_layer(project: QgsProject) -> QgsVectorLayer:
    """Create a temporary in-memory point layer with the standard result fields."""
    temp_layer = QgsVectorLayer(
        f"Point?crs={project.crs().authid()}", "temporary_point_layer", "memory"
    )
    data_provider: QgsVectorDataProvider | None = temp_layer.dataProvider()
    if data_provider is None:
        raise_runtime_error("Could not create data provider for temporary layer.")

    fields_to_add: list[QgsField] = []
    fields_to_add.extend(
        QgsField(field_enum.name, field_enum.data_type)
        for field_enum in cont.NewLayerFields
    )
    data_provider.addAttributes(fields_to_add)

    temp_layer.updateFields()

    log_debug(
        f"Temporary point layer with {len(temp_layer.fields())} fields "
        f" and {temp_layer.featureCount()} features created.",
        Qgis.Success,
    )

    return temp_layer


def project_gpkg() -> Path:
    """Check if a GeoPackage with the same name as the project
    exists in the project folder and creates it if not.

    Example: for a project 'my_project.qgz',
    it looks for 'my_project.gpkg' in the same directory.

    :returns: The Path object to the GeoPackage.
    :raises RuntimeError: If the project is not saved.
    :raises IOError: If the GeoPackage file cannot be created.
    """
    project: QgsProject = get_current_project()
    project_path_str: str = project.fileName()
    if not project_path_str:
        # fmt: off
        raise_user_error(QCoreApplication.translate("UserError", "Project is not saved. Please save the project first."))  # noqa: E501
        # fmt: on

    project_path: Path = Path(project_path_str)
    gpkg_path: Path = project_path.with_suffix(".gpkg")

    if not gpkg_path.exists():
        driver = ogr.GetDriverByName("GPKG")
        data_source = driver.CreateDataSource(str(gpkg_path))
        if data_source is None:
            raise_runtime_error(f"Failed to create GeoPackage at: {gpkg_path}")

        # Dereference the data source to close the file and release the lock.
        data_source = None

    return gpkg_path


class LayerManager:
    """A class to manage the layers used in the plugin."""

    def __init__(self) -> None:
        """Initialize the layer manager class."""
        qgis_iface: QgisInterface | None = iface
        if qgis_iface is None:
            raise_runtime_error("QGIS interface not available.")

        self._qgis_iface: QgisInterface = qgis_iface
        self._project: QgsProject | None = None
        self._selected_layer: QgsVectorLayer | None = None
        self._new_layer: QgsVectorLayer | None = None

    @property
    def project(self) -> QgsProject:
        """The current QGIS project."""
        if self._project is None:
            self._project = get_current_project()
        return self._project

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
    def new_layer(self) -> QgsVectorLayer:
        """The new layer created by the plugin."""
        if self._new_layer is None:
            self.initialize_new_layer()
        if self._new_layer is None:
            raise_runtime_error("New layer is not set.")
        return self._new_layer

    @new_layer.setter
    def new_layer(self, layer: QgsVectorLayer) -> None:
        self._new_layer = layer

    def initialize_new_layer(self) -> None:
        """Initialize the new layer."""
        self.new_layer = self.create_new_layer()

    def fix_layer_name(self, name: str) -> str:
        """Fix encoding mojibake and sanitize a string to be a valid layer name.

        This function first attempts to fix a common mojibake encoding issue,
        where a UTF-8 string was incorrectly decoded as cp1252
        (for example: 'Ãœ' becomes 'Ü').
        It then sanitizes the string to remove or replace characters
        that might be problematic in layer names,
        especially for file-based formats or databases.

        :param name: The potentially garbled and raw layer name.
        :returns: A fixed and sanitized version of the name.
        """
        fixed_name: str = name
        with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
            fixed_name = name.encode("cp1252").decode("utf-8")

        # Remove or replace problematic characters
        sanitized_name: str = re.sub(r'[<>:"/\\|?*,]+', "_", fixed_name)

        return sanitized_name

    def reproject_layer_to_project_crs(self, layer: QgsVectorLayer) -> QgsVectorLayer:
        """Reprojects a vector layer to the project's CRS.

        Creates a new in-memory layer with the same fields and reprojects
        all features from the source layer into it.

        :param layer: The source QgsVectorLayer to reproject.
        :returns: A new, reprojected in-memory QgsVectorLayer.
        """

        log_debug("Creating in-memory layer for reprojection and field filtering.")

        # Clear any selection on the layer
        layer.removeSelection()

        target_crs = self.project.crs()
        if layer.crs() == target_crs:
            log_debug(
                "Layer CRS matches project CRS. "
                "Creating new layer with filtered fields.",
                Qgis.Success,
            )
        else:
            log_debug(
                f"Layer CRS ({layer.crs().authid()}) does not match project CRS "
                f"({target_crs.authid()}). Reprojecting...",
                Qgis.Warning,
            )

        # Create a new in-memory layer with the target CRS
        geometry_type_str: str = QgsWkbTypes.displayString(layer.wkbType())
        reprojected_layer = QgsVectorLayer(
            f"{geometry_type_str}?crs={target_crs.authid()}",
            layer.name(),
            "memory",
        )

        # Copy fields from the source layer, excluding problematic types
        data_provider: QgsVectorDataProvider | None = reprojected_layer.dataProvider()
        if data_provider is None:
            raise_runtime_error(
                f"Could not get data provider for layer: {reprojected_layer.name()}"
            )

        # Define problematic field types that QGIS might struggle with
        filtered_fields: list = []
        for field in layer.fields():
            if (
                field.type() not in cont.PROBLEMATIC_FIELD_TYPES
                and field.name() != "fid"
            ):
                filtered_fields.append(field)
            else:
                log_debug(
                    f"Skipping problematic field '{field.name()}' of type "
                    f"'{field.typeName()}' during layer reprojection/cloning."
                )

        data_provider.addAttributes(filtered_fields)
        data_provider.addAttributes([QgsField("original_fid", QMetaType.Type.Int)])
        reprojected_layer.updateFields()
        log_debug(
            f"The in-memory layer has {len(reprojected_layer.fields())} fields "
            f"(the selected layer has {len(layer.fields())} fields)."
        )

        # Prepare coordinate transformation
        transform = QgsCoordinateTransform(
            layer.crs(), self.project.crs(), self.project.transformContext()
        )

        # Copy and reproject features
        all_ids: list = list(layer.allFeatureIds())
        log_debug(f"Found {len(all_ids)} feature IDs in the selected layer.")
        if not all_ids:
            return reprojected_layer

        new_features: list[QgsFeature] = []
        for fid in all_ids:
            try:
                feature: QgsFeature = layer.getFeature(fid)
                new_feature = QgsFeature()
                new_feature.setFields(reprojected_layer.fields(), initAttributes=True)

                # Copy the original feature ID
                new_feature.setAttribute("original_fid", fid)

                # Copy only attributes that exist in the reprojected_layer's fields
                for field in reprojected_layer.fields():
                    if feature.fieldNameIndex(field.name()) != -1:
                        new_feature.setAttribute(field.name(), feature[field.name()])

                geom: QgsGeometry = feature.geometry()
                if geom.transform(transform) != 0:  # 0 means success
                    log_debug(
                        f"Feature {feature.id()} could not be reprojected.",
                        Qgis.Warning,
                    )
                    continue

                new_feature.setGeometry(geom)
                new_features.append(new_feature)
            except (AttributeError, TypeError, ValueError, RuntimeError) as e:
                log_debug(
                    f"Could not process feature with ID {fid} for reprojection: {e!s}",
                    Qgis.Warning,
                )

        log_debug(f"Processed {len(new_features)} features for reprojection.")

        if new_features:
            reprojected_layer.startEditing()
            reprojected_layer.addFeatures(new_features)

            # Add the in-memory layer to the project to ensure
            # it's not garbage collected or invalidated.
            # We don't add it to the layer tree, so it remains invisible.
            self.project.addMapLayer(reprojected_layer, addToLegend=False)
            log_debug(
                f"Added reprojected layer to map registry. "
                f"Feature count: {reprojected_layer.featureCount()}"
            )

            if reprojected_layer.commitChanges():
                log_debug(
                    f"Successfully committed {reprojected_layer.featureCount()} "
                    "features to reprojected in-memory layer.",
                    Qgis.Success,
                )
            else:
                log_debug(
                    f"Failed to commit changes to reprojected in-memory layer. "
                    f"Feature count: {reprojected_layer.featureCount()}",
                    Qgis.Critical,
                )
            reprojected_layer.updateExtents()
        log_debug(
            f"The selected layer has {layer.featureCount()} features "
            f"and {len(layer.fields())} fields."
            f"The reprojected layer has {reprojected_layer.featureCount()} features "
            f"and {len(reprojected_layer.fields())} fields."
        )
        return reprojected_layer

    def get_selected_layer(self) -> QgsVectorLayer:
        """Collect the selected layer in the QGIS layer tree view and reprojects it.

        :returns: The selected and reprojected QgsVectorLayer object.
        :raises RuntimeError: If no layer is selected, multiple layers are selected,
                              or the selected layer is not a line vector layer.
        """
        layer_tree: QgsLayerTreeView | None = self._qgis_iface.layerTreeView()
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

        # Check for one of the required 'diameter' fields
        if all(
            selected_layer.fields().lookupField(name) == -1
            for name in cont.Names.sel_layer_field_dim
        ):
            log_debug(
                f"None of the specified dimension fields "
                f"({', '.join(cont.Names.sel_layer_field_dim)}) "
                f"were found in the selected layer. Dimension-related "
                f"attributes will be skipped.",
                Qgis.Warning,
            )

        # Reproject the layer to the project's CRS
        return self.reproject_layer_to_project_crs(selected_layer)

    def create_new_layer(self) -> QgsVectorLayer:
        """Create an empty point layer in the project's GeoPackage."""

        log_debug("Creating new layer in GeoPackage...")

        gpkg_path: Path = project_gpkg()
        new_layer_name: str = (
            f"{self.fix_layer_name(self.selected_layer.name())}"
            f"{cont.Names.new_layer_suffix}"
        )

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
            QgsField(field_enum.name, field_enum.data_type)
            for field_enum in cont.NewLayerFields
        ]
        data_provider.addAttributes(fields_to_add)
        empty_layer.updateFields()

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = new_layer_name
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

        error: tuple = QgsVectorFileWriter.writeAsVectorFormatV3(
            empty_layer, str(gpkg_path), self.project.transformContext(), options
        )
        if error[0] == QgsVectorFileWriter.WriterError.NoError:
            log_debug(
                f"Empty layer '{new_layer_name}' "
                f"with {len(empty_layer.fields())} fields and "
                f"{empty_layer.featureCount()} features created "
                f"in GeoPackage.",
                Qgis.Success,
            )
        else:
            raise_runtime_error(
                f"Failed to create empty layer '{new_layer_name}' - Error: {error[1]}"
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
            f"Added empty layer '{gpkg_layer.name()}' "
            f"with {len(gpkg_layer.fields())} fields and "
            f"{gpkg_layer.featureCount()} features created "
            "from the project's GeoPackage to the project.",
            Qgis.Success,
        )
        self.set_layer_style(gpkg_layer)

        return gpkg_layer

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

    def remove_duplicates_from_layer(self, layer: QgsVectorLayer) -> None:
        """Detect and remove duplicate features in the given layer.

        Two features are considered duplicates if they share the same location
        (rounded to 4 decimals) and have identical classification-relevant
        attributes (type, dimensions, angle, connected). The first occurrence is
        kept, subsequent ones are removed.

        Args:
            layer: The layer to process for duplicates.
        """

        log_debug("Starting duplicate check on the final layer...")

        to_delete: list[int] = []
        removed_by_type: dict[str, int] = {}
        seen: dict[tuple, int] = {}

        request = QgsFeatureRequest()
        for feature in layer.getFeatures(request):  # pyright: ignore[reportGeneralTypeIssues]
            feature_geometry = feature.geometry()
            if feature_geometry is None or feature_geometry.isEmpty():
                continue

            key: tuple = (
                round(feature_geometry.asPoint().x(), 4),
                round(feature_geometry.asPoint().y(), 4),
                feature.attribute(cont.NewLayerFields.type.name),
                feature.attribute(cont.NewLayerFields.dim_1.name) or "",
                feature.attribute(cont.NewLayerFields.angle.name) or None,
                feature.attribute(cont.NewLayerFields.connected.name) or "",
            )

            if key in seen:
                original_fid: int = seen[key]
                log_debug(
                    f"Duplicate feature found: {key[2]} (fid {feature.id()}). "
                    f"Keeping original feature (fid {original_fid})."
                )
                to_delete.append(feature.id())
                feature_type = str(key[2])
                removed_by_type[feature_type] = removed_by_type.get(feature_type, 0) + 1
            else:
                seen[key] = feature.id()

        if to_delete:
            if not layer.isEditable() and not layer.startEditing():
                raise_runtime_error("Failed to start editing to remove duplicates.")

            # Prefer batch deletion if available
            try:
                if hasattr(layer, "deleteFeatures"):
                    layer.deleteFeatures(to_delete)
                else:
                    for fid in to_delete:
                        layer.deleteFeature(fid)
            except Exception:  # noqa: BLE001
                # Fallback to per-feature deletion
                for fid in to_delete:
                    layer.deleteFeature(fid)

            if not layer.commitChanges():
                raise_runtime_error("Failed to commit duplicate deletions.")

        summary_parts: list[str] = [
            f"{type_name}: {count}" for type_name, count in removed_by_type.items()
        ]
        type_summary: str = f" ({', '.join(summary_parts)})" if summary_parts else ""

        log_debug(
            f"Duplicate check finished. {len(to_delete)} duplicates removed."
            f"{type_summary}",
            Qgis.Success,
        )

    def set_layer_style(self, layer: QgsVectorLayer) -> None:
        """Set the layer style from a QML file."""

        qml_resource_path = ":/compiled_res_FW_Netz/layer_style/UTEC_FW_Netz_style.qml"
        layer.loadNamedStyle(qml_resource_path)

        layer.triggerRepaint()
        log_debug("Layer style set.", Qgis.Success)
