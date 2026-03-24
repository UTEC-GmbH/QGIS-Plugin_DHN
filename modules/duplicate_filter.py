"""Module: duplicate_filter.py

This module contains the DuplicateFilter class for cleaning up layers.
"""

from dataclasses import dataclass

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsVectorLayer,
)

from .constants import NewPointLayerFields
from .logs_and_errors import log_debug, raise_runtime_error


@dataclass
class DuplicateCheckResult:
    """Holds the results of a duplicate feature check.

    Attributes:
        to_delete: A list of feature IDs identified for deletion.
        removed_by_type: A dictionary mapping feature types to the count of
            removed features of that type.
    """

    to_delete: list[int]
    removed_by_type: dict[str, int]


@dataclass(frozen=True)
class FeatureKey:
    """Unique key for identifying duplicate features.

    Attributes:
        x: The x-coordinate of the feature.
        y: The y-coordinate of the feature.
        feature_type: The type of the feature.
        dim_1: The first dimension of the feature.
        angle: The angle of the feature.
        connected: The connected pipes string.
    """

    x: float
    y: float
    feature_type: str | None
    dim_1: int | str
    angle: int | None
    connected: str


class DuplicateFilter:
    """A class to detect and remove duplicate features from a layer."""

    def remove_duplicates(self, layer: QgsVectorLayer) -> None:
        """Detect and remove duplicate features in the given layer.

        Two features are considered duplicates if they share the same location
        (rounded to 4 decimals) and have identical classification-relevant
        attributes (type, dimensions, angle, connected). The first occurrence is
        kept, subsequent ones are removed.

        Args:
            layer: The layer to process for duplicates.

        Raises:
            CustomRuntimeError: If editing cannot be started or committed.
        """
        log_debug("Starting duplicate check on the final layer...")

        duplicates: DuplicateCheckResult = self._find_duplicates(layer)
        self._delete_features_from_layer(layer, duplicates.to_delete)
        self._log_duplicate_summary(
            len(duplicates.to_delete), duplicates.removed_by_type
        )

    def _find_duplicates(self, layer: QgsVectorLayer) -> DuplicateCheckResult:
        """Find duplicate features in a layer based on a composite key.

        Args:
            layer: The layer to search for duplicates.

        Returns:
            A DuplicateCheckResult object containing IDs to delete and stats.
        """
        to_delete: list[int] = []
        removed_by_type: dict[str, int] = {}
        seen: dict[FeatureKey, int] = {}

        request: QgsFeatureRequest = QgsFeatureRequest()
        for feature in layer.getFeatures(request):  # pyright: ignore[reportGeneralTypeIssues]
            if not (key := self._build_feature_key(feature)):
                continue

            if key in seen:
                original_fid: int = seen[key]
                log_debug(
                    f"Duplicate feature found: {key.feature_type} "
                    f"(fid {feature.id()}). "
                    f"Keeping original feature (fid {original_fid}).",
                    icon="🐑",
                )
                to_delete.append(feature.id())
                feature_type = str(key.feature_type)
                removed_by_type[feature_type] = removed_by_type.get(feature_type, 0) + 1
            else:
                seen[key] = feature.id()
        return DuplicateCheckResult(
            to_delete=to_delete, removed_by_type=removed_by_type
        )

    def _build_feature_key(self, feature: QgsFeature) -> FeatureKey | None:
        """Build a unique key for a feature to detect duplicates.

        Args:
            feature: The feature to build a key for.

        Returns:
            A FeatureKey representing the feature's key, or None if geometry is invalid.
        """
        feature_geometry: QgsGeometry = feature.geometry()
        if feature_geometry is None or feature_geometry.isEmpty():
            return None

        return FeatureKey(
            x=round(feature_geometry.asPoint().x(), 4),
            y=round(feature_geometry.asPoint().y(), 4),
            feature_type=feature.attribute(NewPointLayerFields.type.field_name),
            dim_1=feature.attribute(NewPointLayerFields.dim_1.field_name) or "",
            angle=feature.attribute(NewPointLayerFields.angle.field_name) or None,
            connected=feature.attribute(NewPointLayerFields.connected.field_name) or "",
        )

    def _delete_features_from_layer(
        self, layer: QgsVectorLayer, fids_to_delete: list[int]
    ) -> None:
        """Delete features from a layer by their IDs.

        Args:
            layer: The layer to delete features from.
            fids_to_delete: A list of feature IDs to delete.

        Raises:
            CustomRuntimeError: If starting editing or committing changes fails.
        """
        if not fids_to_delete:
            return

        if not layer.isEditable() and not layer.startEditing():
            raise_runtime_error("Failed to start editing to remove duplicates.")

        try:
            if hasattr(layer, "deleteFeatures"):
                layer.deleteFeatures(fids_to_delete)
            else:
                for fid in fids_to_delete:
                    layer.deleteFeature(fid)
        except Exception as e:  # noqa: BLE001
            log_debug(f"Batch deletion failed: {e}", Qgis.Warning)
            for fid in fids_to_delete:
                layer.deleteFeature(fid)

        if not layer.commitChanges():
            raise_runtime_error("Failed to commit duplicate deletions.")

    def _log_duplicate_summary(
        self, deleted_count: int, removed_by_type: dict[str, int]
    ) -> None:
        """Log a summary of the removed duplicate features.

        Args:
            deleted_count: The total number of deleted features.
            removed_by_type: A dictionary counting removed features by type.
        """
        summary_parts: list[str] = [
            f"{type_name}: {count}" for type_name, count in removed_by_type.items()
        ]
        type_summary: str = f" ({', '.join(summary_parts)})" if summary_parts else ""

        log_debug(
            f"Duplicate check finished. {deleted_count} duplicates removed."
            f"{type_summary}",
            Qgis.Success,
        )
