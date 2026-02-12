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
    Names,
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

        pipe_hcs = self._populate_connected_buildings()
        self._generate_designations(pipe_hcs)

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

    def _populate_connected_buildings(self) -> dict[int, set[str]]:
        """Identify and list connected buildings for each pipe.

        This method builds a graph of the network and propagates house connection
        IDs from the leaves (house connections) up through the network branches.
        """
        log_debug("Populating connected buildings...")

        # 1. Build Graph: Node -> Set of (PipeID, NeighborNode)
        # Node is a tuple of (x, y) rounded to 4 decimals.
        adj: dict[tuple[float, float], set[tuple[int, tuple[float, float]]]] = {}
        degrees: dict[tuple[float, float], int] = {}

        for feature in self.pipe_layer.getFeatures():
            endpoints = self._get_endpoints(feature)
            if len(endpoints) != 2:  # noqa: PLR2004
                continue

            p1 = (round(endpoints[0].x(), 4), round(endpoints[0].y(), 4))
            p2 = (round(endpoints[1].x(), 4), round(endpoints[1].y(), 4))
            fid = feature.id()

            adj.setdefault(p1, set()).add((fid, p2))
            adj.setdefault(p2, set()).add((fid, p1))

            degrees[p1] = degrees.get(p1, 0) + 1
            degrees[p2] = degrees.get(p2, 0) + 1

        # 2. Map House Connections to Graph Nodes
        node_hcs: dict[tuple[float, float], set[str]] = {}
        req = QgsFeatureRequest().setFilterExpression(
            f'"{NewPointLayerFields.type.field_name}" = '
            f"'{FittingType.HOUSE_CONN.translated}'"
        )
        for feat in self.point_layer.getFeatures(req):
            if geom := feat.geometry():
                p = geom.asPoint()
                key = (round(p.x(), 4), round(p.y(), 4))
                node_hcs.setdefault(key, set()).add(str(feat.id()))

        # 3. Peeling Algorithm (Propagate from leaves)
        pipe_hcs: dict[int, set[str]] = {}
        leaves = [n for n, d in degrees.items() if d == 1]

        while leaves:
            leaf = leaves.pop(0)
            if degrees[leaf] == 0:
                continue

            # Get the single connected edge
            edge_info = next(iter(adj[leaf]))
            fid, neighbor = edge_info

            if hcs := node_hcs.get(leaf, set()):
                pipe_hcs.setdefault(fid, set()).update(hcs)
                node_hcs.setdefault(neighbor, set()).update(hcs)

            # Remove edge from graph
            adj[leaf].remove(edge_info)
            adj[neighbor].remove((fid, leaf))
            degrees[leaf] -= 1
            degrees[neighbor] -= 1

            # If neighbor becomes a leaf, add to queue
            if degrees[neighbor] == 1:
                leaves.append(neighbor)

        # 4. Write results to layer
        self.pipe_layer.startEditing()
        idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.conn_buildings.field_name
        )
        if idx != -1:
            for fid, hcs in pipe_hcs.items():
                val = Names.separator.join(sorted(hcs, key=int))
                self.pipe_layer.changeAttributeValue(fid, idx, val)
        self.pipe_layer.commitChanges()

        return pipe_hcs

    def _generate_designations(self, pipe_hcs: dict[int, set[str]]) -> None:
        """Generate and assign designations, types, and branches to pipes."""
        log_debug("Generating pipe designations...")

        # 1. Build Node -> Pipes map to analyze topology
        node_pipes: dict[tuple[float, float], list[int]] = {}
        for feature in self.pipe_layer.getFeatures():
            if endpoints := self._get_endpoints(feature):
                for pt in endpoints:
                    key = (round(pt.x(), 4), round(pt.y(), 4))
                    node_pipes.setdefault(key, []).append(feature.id())

        fields = self.pipe_layer.fields()
        pt_fields = self.point_layer.fields()

        idx_desig = fields.lookupField(NewLineLayerFields.designation.field_name)
        idx_type = fields.lookupField(NewLineLayerFields.type.field_name)
        idx_branch = fields.lookupField(NewLineLayerFields.branch.field_name)

        idx_pt_desig = pt_fields.lookupField(NewPointLayerFields.designation.field_name)

        if idx_desig == -1 or idx_type == -1 or idx_branch == -1 or idx_pt_desig == -1:
            return

        self.pipe_layer.startEditing()
        self.point_layer.startEditing()

        # Keep track of assigned building designations to avoid re-numbering
        # Map: building_fid -> designation_string
        building_map: dict[int, str] = {}
        building_counter = 1

        for fid, hcs in pipe_hcs.items():
            if not hcs:
                continue

            feature = self.pipe_layer.getFeature(fid)
            type_val = feature.attribute(idx_type)

            # Placeholder for branch logic.
            # In a full implementation, this would be calculated via graph traversal.
            branch_num = "01"
            designation = ""

            # Helper to format building ID (e.g., '5' -> '005')
            def fmt_id(val: str) -> str:
                try:
                    return f"{int(val):03d}"
                except ValueError:
                    return val

            if type_val == PipeType.CONN.translated:
                # Type 'a': Use the connected building ID
                target_fid = int(sorted(list(hcs), key=int)[0])

                # Assign a new designation to the building if it doesn't have one
                if target_fid not in building_map:
                    b_desig = f"{branch_num}-{fmt_id(str(building_counter))}"
                    building_map[target_fid] = b_desig
                    self.point_layer.changeAttributeValue(
                        target_fid, idx_pt_desig, b_desig
                    )
                    building_counter += 1

                # Pipe designation matches building designation
                designation = f"a{building_map[target_fid]}"

            elif type_val == PipeType.MAIN.translated:
                # Type 'v' or 'g'
                endpoints = self._get_endpoints(feature)
                if len(endpoints) != 2:  # noqa: PLR2004
                    continue

                # Determine Downstream Node
                # The downstream node is where the HCs of other connected pipes
                # equal the HCs flowing through this pipe.
                downstream_node = None
                for pt in endpoints:
                    key = (round(pt.x(), 4), round(pt.y(), 4))
                    connected_fids = node_pipes.get(key, [])

                    other_hcs = set()
                    for other_fid in connected_fids:
                        if other_fid != fid:
                            other_hcs.update(pipe_hcs.get(other_fid, set()))

                    if other_hcs == hcs:
                        downstream_node = key
                        break

                is_fork = False
                if downstream_node:
                    connected_fids = node_pipes[downstream_node]
                    other_fids = [f for f in connected_fids if f != fid]
                    outgoing_mains = sum(
                        1
                        for f_id in other_fids
                        if self.pipe_layer.getFeature(f_id).attribute(idx_type)
                        == PipeType.MAIN.translated
                    )
                    if outgoing_mains > 1:
                        is_fork = True

                if is_fork:
                    # Update type to FORK
                    self.pipe_layer.changeAttributeValue(
                        fid, idx_type, PipeType.FORK.translated
                    )
                    # For forks, we target a sub-branch (placeholder '02' for now)
                    designation = f"g{branch_num}-02"
                else:
                    target_fid = int(sorted(list(hcs), key=int)[0])

                    # If we haven't encountered this building via a connection pipe yet,
                    # we assign it a number now (though usually 'a' pipes are processed too)
                    if target_fid not in building_map:
                        b_desig = f"{branch_num}-{fmt_id(str(building_counter))}"
                        building_map[target_fid] = b_desig
                        self.point_layer.changeAttributeValue(
                            target_fid, idx_pt_desig, b_desig
                        )
                        building_counter += 1

                    designation = f"v{building_map[target_fid]}"

            self.pipe_layer.changeAttributeValue(fid, idx_branch, branch_num)
            if designation:
                self.pipe_layer.changeAttributeValue(fid, idx_desig, designation)

        self.pipe_layer.commitChanges()
        self.point_layer.commitChanges()
