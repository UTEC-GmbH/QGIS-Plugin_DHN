"""Module: pipe_classifier.py

This module contains the logic to classify pipes in the network, specifically
identifying pipes that connect buildings to the main network.
"""

from typing import TYPE_CHECKING

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

from .constants import (
    FittingType,
    NewLineLayerFields,
    NewPointLayerFields,
    Numbers,
    PipeType,
)
from .logs_and_errors import log_debug

if TYPE_CHECKING:
    from qgis._core import QgsRectangle


class PipeClassifier:
    """Classifies pipes in the network based on topology and point features."""

    def __init__(self, pipe_layer: QgsVectorLayer, point_layer: QgsVectorLayer) -> None:
        """Initialize the PipeClassifier.

        Args:
            pipe_layer: The vector layer containing the pipe network (lines).
                        This should be the mutable copy.
            point_layer: The vector layer containing classified points (points).
        """
        self.pipe_layer: QgsVectorLayer = pipe_layer
        self.point_layer: QgsVectorLayer = point_layer

        # Spatial index for pipes to find connected segments
        self.pipe_index = QgsSpatialIndex(pipe_layer.getFeatures())

        # Spatial index for points to identify node types (T-pieces, etc.)
        self.point_index = QgsSpatialIndex(point_layer.getFeatures())

        # Cache point types for faster lookup
        self.point_types: dict[int, str] = {}
        self._cache_point_types()

    def _cache_point_types(self) -> None:
        """Cache the type of each point feature for fast lookup."""
        req: QgsFeatureRequest = QgsFeatureRequest().setSubsetOfAttributes(
            [NewPointLayerFields.type.field_name], self.point_layer.fields()
        )
        for feat in self.point_layer.getFeatures(req):
            if type_val := feat.attribute(NewPointLayerFields.type.field_name):
                self.point_types[feat.id()] = type_val

    def classify_pipes(self) -> None:
        """Identify and mark pipes connecting house connections to the network."""
        log_debug("Starting pipe classification...")

        pipes_to_mark: set[int] = set()

        # 1. Get all House Connection points
        req: QgsFeatureRequest = QgsFeatureRequest().setFilterExpression(
            f'"{NewPointLayerFields.type.field_name}" = '
            f"'{FittingType.HOUSE_CONN.translated}'"
        )
        hc_features: list = list(self.point_layer.getFeatures(req))

        log_debug(f"Found {len(hc_features)} house connection points.")

        for hc_feat in hc_features:
            pipes_to_mark.update(self._trace_from_house(hc_feat))

        # 2. Update the pipe layer
        if not pipes_to_mark:
            log_debug("No pipes identified as house connections.")
            return

        field_name: str = NewLineLayerFields.type.field_name
        idx: int = self.pipe_layer.fields().lookupField(field_name)
        if idx == -1:
            log_debug(f"Field {field_name} not found in pipe layer.", Qgis.Warning)
            return

        self.pipe_layer.startEditing()
        for fid in pipes_to_mark:
            self.pipe_layer.changeAttributeValue(fid, idx, PipeType.CONN.translated)

        if self.pipe_layer.commitChanges():
            log_debug(f"Marked {len(pipes_to_mark)} pipes as house connections.")
        else:
            log_debug("Failed to commit changes to pipe layer.", Qgis.Critical)

    def _trace_from_house(self, hc_feat: QgsFeature) -> set[int]:
        """Trace the path from a house connection to the nearest T-piece."""
        identified_pipes: set[int] = set()

        point_geom: QgsGeometry = hc_feat.geometry()
        if not point_geom:
            return identified_pipes
        start_point: QgsPointXY = point_geom.asPoint()

        # Find the pipe connected to the HC
        start_pipes: list[QgsFeature] = self._get_pipes_at_point(start_point)

        if not start_pipes:
            return identified_pipes

        # We assume one pipe starts at the HC. If multiple, it's ambiguous,
        # but we can try to trace all valid paths.
        queue: list[tuple[QgsFeature, QgsPointXY]] = [
            (p, start_point) for p in start_pipes
        ]

        visited: set[int] = set()

        while queue:
            pop: tuple[QgsFeature, QgsPointXY] = queue.pop(0)
            current_pipe: QgsFeature = pop[0]
            entry_point: QgsPointXY = pop[1]

            if current_pipe.id() in visited:
                continue
            visited.add(current_pipe.id())

            # This pipe is part of the connection
            identified_pipes.add(current_pipe.id())

            # Find the exit point (the other end)
            endpoints: list[QgsPointXY] = self._get_endpoints(current_pipe)
            if len(endpoints) != 2:  # noqa: PLR2004
                continue

            p1: QgsPointXY = endpoints[0]
            p2: QgsPointXY = endpoints[1]
            # Robustly pick the point furthest from entry_point as exit
            # (In case entry_point is slightly offset due to tolerance)
            exit_point: QgsPointXY = (
                p2 if p1.distance(entry_point) < p2.distance(entry_point) else p1
            )

            # Check if we hit a T-Piece
            if self._is_t_piece(exit_point):
                # Reached the main line. Stop this branch.
                continue

            # Find next pipes at the exit point
            next_pipes: list[QgsFeature] = self._get_pipes_at_point(exit_point)
            # Filter out the pipe we just came from
            next_pipes = [p for p in next_pipes if p.id() != current_pipe.id()]

            if len(next_pipes) == 1:
                # Simple continuation (e.g., through a bend or reducer)
                queue.append((next_pipes[0], exit_point))
            elif len(next_pipes) > 1:
                # Branching point that is NOT labeled as a T-Piece.
                # This could be a data issue or a complex junction.
                # To be safe, we stop here as we can't determine the main path easily.
                pass

        return identified_pipes

    def _get_pipes_at_point(self, point: QgsPointXY) -> list[QgsFeature]:
        """Find pipes intersecting a point (within tolerance)."""
        search_geom: QgsGeometry = QgsGeometry.fromPointXY(point).buffer(
            Numbers.search_radius, 5
        )
        rect: QgsRectangle = search_geom.boundingBox()

        candidate_ids: list[int] = self.pipe_index.intersects(rect)
        pipes: list[QgsFeature] = []
        for fid in candidate_ids:
            feat: QgsFeature = self.pipe_layer.getFeature(fid)
            if feat.geometry().intersects(search_geom):
                pipes.append(feat)
        return pipes

    def _get_endpoints(self, feature: QgsFeature) -> list[QgsPointXY]:
        """Get the start and end points of a line feature."""
        geom: QgsGeometry = feature.geometry()
        if not geom:
            return []

        # Handle MultiLineString
        if geom.wkbType() == QgsWkbTypes.MultiLineString:
            lines = geom.asMultiPolyline()
            if not lines:
                return []
            # This assumes single connected component multiline
            # Ideally we should handle all parts, but let's take extremes
            return [lines[0][0], lines[-1][-1]]

        if geom.wkbType() == QgsWkbTypes.LineString:
            polyline = geom.asPolyline()
            if len(polyline) < Numbers.min_points_line:
                return []
            return [polyline[0], polyline[-1]]

        return []

    def _is_t_piece(self, point: QgsPointXY) -> bool:
        """Check if a T-piece exists at the given location."""
        search_geom: QgsGeometry = QgsGeometry.fromPointXY(point).buffer(
            Numbers.search_radius, 5
        )
        rect: QgsRectangle = search_geom.boundingBox()

        candidate_ids: list[int] = self.point_index.intersects(rect)
        for fid in candidate_ids:
            # Check if this point feature is actually a T-Piece
            if fid in self.point_types:
                t_type: str = self.point_types[fid]
                if t_type == FittingType.T_PIECE.translated:
                    # Double check geometry intersection to be sure
                    feat: QgsFeature = self.point_layer.getFeature(fid)
                    if feat.geometry().intersects(search_geom):
                        return True
        return False
