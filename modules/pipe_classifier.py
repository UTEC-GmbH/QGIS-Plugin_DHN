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
)

from .connected_buildings import ConnectedBuildingsPropagator
from .constants import (
    FittingType,
    NewLineLayerFields,
    NewPointLayerFields,
    Numbers,
    PipeType,
)
from .logs_and_errors import log_debug
from .network_numbering import NetworkNumberer
from .vector_analysis_tools import VectorAnalysisTools

if TYPE_CHECKING:
    from qgis.core import QgsRectangle


class PipeClassifier:
    """Classifies pipes in the network based on topology and point features."""

    def __init__(self, pipe_layer: QgsVectorLayer, point_layer: QgsVectorLayer) -> None:
        """Initialize the PipeClassifier.

        Args:
            pipe_layer: The vector layer containing the pipe network (lines).
                This should be the mutable copy.
            point_layer: The vector layer containing classified points (fittings).
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
        """Cache the 'type' attribute of each point feature for fast lookup."""
        req: QgsFeatureRequest = QgsFeatureRequest().setSubsetOfAttributes(
            [NewPointLayerFields.type.field_name], self.point_layer.fields()
        )
        for feat in self.point_layer.getFeatures(req):
            if type_val := feat.attribute(NewPointLayerFields.type.field_name):
                self.point_types[feat.id()] = type_val

    def classify_pipes(self) -> None:
        """Orchestrate the entire pipe classification and numbering process.

        This method performs the following steps:
        1. Traces pipes from house connections back to the main network and
           marks them as 'Connecting Pipe'.
        2. Initiates the network numbering process to assign branch and
           designation IDs.
        3. Populates an informational field on each pipe with a list of the
           buildings it ultimately connects to.
        """
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

        # 3. Process Network Numbering (Branches, Designations)
        NetworkNumberer(self.pipe_layer, self.point_layer).run()

        # 4. Populate connected buildings string (legacy/info field)
        ConnectedBuildingsPropagator(self.pipe_layer, self.point_layer).run()

    def _trace_from_house(self, house_connection_feature: QgsFeature) -> set[int]:
        """Trace the path from a house connection to the main network.

        Starting from a house connection point, this method traverses the pipe
        network outwards (Breadth-First Search) until it encounters a T-piece,
        which is assumed to be part of the main network. All pipes along this
        path are considered connection pipes.

        Args:
            house_connection_feature: The house connection feature to start
                tracing from.

        Returns:
            A set of feature IDs for the pipes identified as part of the
            connection path.
        """
        identified_pipes: set[int] = set()

        point_geom: QgsGeometry = house_connection_feature.geometry()
        if not point_geom:
            return identified_pipes
        start_point: QgsPointXY = point_geom.asPoint()

        # Find the pipe connected to the HC
        start_pipes: list[QgsFeature] = self._get_pipes_at_point(start_point)

        if not start_pipes:
            return identified_pipes

        queue: list[tuple[QgsFeature, QgsPointXY]] = [
            (pipe, start_point) for pipe in start_pipes
        ]

        visited: set[int] = set()

        while queue:
            queue_item: tuple[QgsFeature, QgsPointXY] = queue.pop(0)
            current_pipe: QgsFeature = queue_item[0]
            entry_point: QgsPointXY = queue_item[1]

            if current_pipe.id() in visited:
                continue
            visited.add(current_pipe.id())

            # This pipe is part of the connection
            identified_pipes.add(current_pipe.id())

            # Find the exit point (the other end)
            endpoints: list[QgsPointXY] = VectorAnalysisTools.get_start_end_of_line(
                current_pipe
            )
            if len(endpoints) != Numbers.min_points_line:
                continue

            point_1: QgsPointXY = endpoints[0]
            point_2: QgsPointXY = endpoints[1]
            exit_point: QgsPointXY = (
                point_2
                if point_1.distance(entry_point) < point_2.distance(entry_point)
                else point_1
            )

            if self._is_t_piece(exit_point):
                # T-Piece found -> Reached the main line. Stop this branch.
                continue

            # Find next pipes at exit point and filter out the pipe we just came from
            next_pipes: list[QgsFeature] = self._get_pipes_at_point(exit_point)
            next_pipes = [pipe for pipe in next_pipes if pipe.id() != current_pipe.id()]

            if len(next_pipes) == 1:
                # Simple continuation (e.g., through a bend or reducer)
                queue.append((next_pipes[0], exit_point))

        return identified_pipes

    def _get_pipes_at_point(self, point: QgsPointXY) -> list[QgsFeature]:
        """Find pipes intersecting a point within a small search radius.

        Args:
            point: The point to search around.

        Returns:
            A list of pipe features that intersect the search area.
        """
        search_geom: QgsGeometry = QgsGeometry.fromPointXY(point).buffer(
            Numbers.search_radius, 5
        )
        rect: QgsRectangle = search_geom.boundingBox()

        candidate_ids: list[int] = self.pipe_index.intersects(rect)
        return [
            feat
            for fid in candidate_ids
            if (feat := self.pipe_layer.getFeature(fid))
            and feat.geometry().intersects(search_geom)
        ]

    def _is_t_piece(self, point: QgsPointXY) -> bool:
        """Check if a T-piece fitting exists at the given location.

        This method uses a spatial index for a fast lookup within a small
        search radius around the point.

        Args:
            point: The point to check.

        Returns:
            True if a T-piece is found at the location, False otherwise.
        """
        search_geom: QgsGeometry = QgsGeometry.fromPointXY(point).buffer(
            Numbers.search_radius, 5
        )
        rect: QgsRectangle = search_geom.boundingBox()

        candidate_ids: list[int] = self.point_index.intersects(rect)
        for fid in candidate_ids:
            # Check if this point feature is actually a T-Piece
            if (
                self.point_types.get(fid) == FittingType.T_PIECE.translated
                and (feat := self.point_layer.getFeature(fid))
                and feat.geometry().intersects(search_geom)
            ):
                return True
        return False
