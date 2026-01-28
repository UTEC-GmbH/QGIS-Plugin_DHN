"""Module: point_collector.py

This module contains the PointCollector class for gathering points of interest.
"""

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtWidgets import QProgressBar

from .logs_and_errors import log_debug


class PointCollector:
    """A class to collect unique points of interest from a line layer."""

    def __init__(
        self, layer: QgsVectorLayer, spatial_index: QgsSpatialIndex | None = None
    ) -> None:
        """Initialize the PointCollector.

        Args:
            layer: The line vector layer to process.
            spatial_index: An optional pre-built spatial index for the layer.
        """
        self.layer: QgsVectorLayer = layer
        self.spatial_index: QgsSpatialIndex = spatial_index or QgsSpatialIndex(
            layer.getFeatures(QgsFeatureRequest().setNoAttributes())
        )
        self.checked_points: set[tuple[float, float]] = set()

    def _collect_vertices(self, features: list[QgsFeature]) -> list[QgsPointXY]:
        """Collect all unique vertices from a list of features.

        Args:
            features: A list of features to process.

        Returns:
            A list of unique QgsPointXY points representing the vertices.
        """
        vertices: list[QgsPointXY] = []
        for feature in features:
            geom: QgsGeometry = feature.geometry()
            if not geom or geom.isNull():
                continue

            for vertex in geom.vertices():
                point = QgsPointXY(vertex)
                point_key: tuple[float, float] = (
                    round(point.x(), 4),
                    round(point.y(), 4),
                )
                if point_key not in self.checked_points:
                    vertices.append(point)
                    self.checked_points.add(point_key)
        return vertices

    def collect_points(self, progress_bar: QProgressBar) -> dict[str, list[QgsPointXY]]:
        """Collect all unique vertices and intersection points from the layer.

        Args:
            progress_bar: A progress bar to report progress.

        Returns:
            A dictionary containing two lists of unique QgsPointXY points:
            'vertices' for all feature vertices and 'intersections' for
            true mid-segment intersections.
        """
        features: list[QgsFeature] = list(self.layer.getFeatures())
        progress_bar.setMaximum(len(features))
        intersections: list[QgsPointXY] = []

        # 1. Collect all vertices first
        vertices: list[QgsPointXY] = self._collect_vertices(features)

        # 2. Find and add true intersection points
        for i, feature in enumerate(features):
            geom: QgsGeometry = feature.geometry()
            if not geom or geom.isNull():
                continue

            candidate_ids: list[int] = self.spatial_index.intersects(geom.boundingBox())
            for candidate_id in candidate_ids:
                if candidate_id <= feature.id():
                    continue

                candidate_feat: QgsFeature = self.layer.getFeature(candidate_id)
                candidate_geom: QgsGeometry = candidate_feat.geometry()

                if not candidate_geom or not geom.intersects(candidate_geom):
                    continue

                intersection: QgsGeometry = geom.intersection(candidate_geom)
                if not intersection or intersection.isEmpty():
                    continue

                self._add_intersection_points(
                    intersection, intersections, [feature, candidate_feat]
                )

            progress_bar.setValue(i + 1)

        log_debug(
            f"Collected {len(vertices)} vertices and "
            f"{len(intersections)} true intersections.",
            Qgis.Success,
        )
        return {"vertices": vertices, "intersections": intersections}

    def _add_intersection_points(
        self,
        intersection_geom: QgsGeometry,
        points_list: list[QgsPointXY],
        intersecting_features: list[QgsFeature],
    ) -> None:
        """Extract and add unique points from an intersection geometry.

        Only adds points that are not vertices of the intersecting features.

        Args:
            intersection_geom: The geometry of the intersection.
            points_list: The list to append found points to.
            intersecting_features: The features involved in the intersection.
        """
        points_to_add: list[QgsPointXY] = []
        wkb_type: Qgis.WkbType = intersection_geom.wkbType()

        if wkb_type == QgsWkbTypes.Point:
            points_to_add.append(intersection_geom.asPoint())
        elif wkb_type == QgsWkbTypes.MultiPoint:
            points_to_add.extend(intersection_geom.asMultiPoint())
        elif wkb_type in (QgsWkbTypes.LineString, QgsWkbTypes.MultiLineString):
            # For line intersections, only add the start and end points
            for part in intersection_geom.parts():
                points_to_add.extend([part.startPoint(), part.endPoint()])

        for point in points_to_add:
            point_key: tuple[float, float] = (round(point.x(), 4), round(point.y(), 4))
            if point_key not in self.checked_points and not self._is_vertex_of_any(
                point, intersecting_features
            ):
                points_list.append(point)
                self.checked_points.add(point_key)

    @staticmethod
    def _is_vertex_of_any(
        point: QgsPointXY, features: list[QgsFeature], tolerance: float = 1e-4
    ) -> bool:
        """Check if a point is a vertex of any of the given features.

        Args:
            point: The point to check.
            features: The list of features to check against.
            tolerance: The distance tolerance for the check. Defaults to 1e-4.

        Returns:
            True if the point is close to a vertex of any feature, False otherwise.
        """
        for feature in features:
            geom = feature.geometry()
            if not geom:
                continue

            # check if the distance to that closest vertex is within tolerance.
            closest_vertex, *_ = geom.closestVertex(point)
            if point.distance(closest_vertex) < tolerance:
                return True
        return False
