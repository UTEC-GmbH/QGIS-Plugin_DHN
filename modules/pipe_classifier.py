"""Module: pipe_classifier.py

This module contains the logic to classify pipes in the network, specifically
identifying pipes that connect buildings to the main network.
"""

from collections import deque
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

        # 3. Process Network Numbering (Branches, Designations)
        self._process_network_numbering()

        # 4. Populate connected buildings string (legacy/info field)
        self._populate_connected_buildings()

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
                desig = feat.attribute(NewPointLayerFields.designation.field_name)
                val = str(desig) if desig else str(feat.id())
                node_hcs.setdefault(key, set()).add(val)

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
                val = Names.separator.join(sorted(hcs))
                self.pipe_layer.changeAttributeValue(fid, idx, val)
        self.pipe_layer.commitChanges()

        return pipe_hcs

    def _process_network_numbering(self) -> None:
        """Process the network to assign branches and designations."""
        log_debug("Processing network numbering...")

        # 1. Build Graph of Main Pipes
        # Node: (x, y) -> List of (PipeFID, NeighborNode)
        adj: dict[tuple[float, float], list[tuple[int, tuple[float, float]]]] = {}
        type_idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )
        dim_idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.dim.field_name
        )
        main_type = PipeType.MAIN.translated
        fork_type = PipeType.FORK.translated

        # Helper to get pipe info
        pipe_info = {}  # fid -> {dim: int, length: float}

        for feat in self.pipe_layer.getFeatures():
            t = feat.attribute(type_idx)
            if t in (main_type, fork_type):
                fid = feat.id()
                endpoints = self._get_endpoints(feat)
                if len(endpoints) == 2:  # noqa: PLR2004
                    p1 = (round(endpoints[0].x(), 4), round(endpoints[0].y(), 4))
                    p2 = (round(endpoints[1].x(), 4), round(endpoints[1].y(), 4))
                    adj.setdefault(p1, []).append((fid, p2))
                    adj.setdefault(p2, []).append((fid, p1))

                    dim_val = feat.attribute(dim_idx)
                    try:
                        dim = int(dim_val) if dim_val is not None else 0
                    except (ValueError, TypeError):
                        dim = 0

                    pipe_info[fid] = {"dim": dim, "length": feat.geometry().length()}

        if not adj:
            return

        # 2. Orient Network (BFS) to establish depth
        # Root is node with max degree (heuristic for heating plant/source)
        root = max(adj.keys(), key=lambda n: len(adj[n]))
        node_depth = {root: 0}
        queue = deque([root])

        while queue:
            u = queue.popleft()
            for _, v in adj[u]:
                if v not in node_depth:
                    node_depth[v] = node_depth[u] + 1
                    queue.append(v)

        # 3. Trace Branches (Main Path Strategy)
        # List of branches, where each branch is a list of (PipeFID, EndNode)
        branches: list[list[tuple[int, tuple[float, float]]]] = []
        visited_pipes: set[int] = set()

        # Queue for branch starts: (StartNode, FirstPipeFID|None)
        branch_queue = deque([(root, None)])

        while branch_queue:
            start_node, first_pipe_fid = branch_queue.popleft()

            current_branch = []
            curr_node = start_node

            # If this is a sub-branch starting with a specific pipe
            if first_pipe_fid is not None:
                next_node = next(
                    (n for f, n in adj[start_node] if f == first_pipe_fid), None
                )
                if not next_node:
                    continue

                current_branch.append((first_pipe_fid, next_node))
                visited_pipes.add(first_pipe_fid)
                curr_node = next_node

            # Continue traversing
            while True:
                # Get candidates
                candidates = []
                for fid, neighbor in adj[curr_node]:
                    if fid in visited_pipes:
                        continue

                    # Check flow direction (heuristic: don't go back to lower depth)
                    if node_depth.get(neighbor, 0) < node_depth.get(curr_node, 0):
                        continue

                    candidates.append((fid, neighbor))

                if not candidates:
                    break

                # Sort candidates: Primary: Diameter (desc), Secondary: Length (desc)
                candidates.sort(
                    key=lambda x: (pipe_info[x[0]]["dim"], pipe_info[x[0]]["length"]),
                    reverse=True,
                )

                # Best candidate continues the branch
                best_fid, best_node = candidates[0]

                # Others start new branches from the current node
                for fid, _ in candidates[1:]:
                    branch_queue.append((curr_node, fid))

                current_branch.append((best_fid, best_node))
                visited_pipes.add(best_fid)
                curr_node = best_node

            if current_branch:
                branches.append(current_branch)

        # 4. Map HCs to Nodes
        node_hcs: dict[tuple[float, float], list[int]] = {}
        hc_req = QgsFeatureRequest().setFilterExpression(
            f'"{NewPointLayerFields.type.field_name}" = '
            f"'{FittingType.HOUSE_CONN.translated}'"
        )

        for hc_feat in self.point_layer.getFeatures(hc_req):
            hc_pt = hc_feat.geometry().asPoint()
            # Trace to main node
            main_node = self._find_main_node_for_hc(hc_pt, adj)
            if main_node:
                node_hcs.setdefault(main_node, []).append(hc_feat.id())

        # 5. Apply Numbering
        idx_desig_pipe = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.designation.field_name
        )
        idx_branch_pipe = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.branch.field_name
        )
        idx_type_pipe = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )
        idx_desig_pt = self.point_layer.fields().lookupField(
            NewPointLayerFields.designation.field_name
        )

        self.pipe_layer.startEditing()
        self.point_layer.startEditing()

        for i, branch_pipes in enumerate(branches, 1):
            branch_id = f"{i:02d}"
            counter = 1

            for pipe_fid, node in branch_pipes:
                # Update Branch ID
                self.pipe_layer.changeAttributeValue(
                    pipe_fid, idx_branch_pipe, branch_id
                )

                hcs = node_hcs.get(node, [])

                if hcs:
                    # Sort HCs by connecting pipe length (descending)
                    hcs.sort(key=self._get_conn_pipe_length, reverse=True)

                    # House Connection Node
                    for hc_fid in hcs:
                        num_str = f"{branch_id}-{counter:03d}"

                        # Update HC
                        self.point_layer.changeAttributeValue(
                            hc_fid, idx_desig_pt, num_str
                        )

                        # Update Conn Pipe(s)
                        self._update_conn_pipe_name(hc_fid, f"a{num_str}", branch_id)

                        # Update Main Pipe
                        self.pipe_layer.changeAttributeValue(
                            pipe_fid, idx_desig_pipe, f"v{num_str}"
                        )
                        self.pipe_layer.changeAttributeValue(
                            pipe_fid, idx_type_pipe, PipeType.MAIN.translated
                        )

                        counter += 1
                else:
                    # No HC -> Junction or Bend
                    num_str = f"{branch_id}-{counter:03d}"
                    self.pipe_layer.changeAttributeValue(
                        pipe_fid, idx_desig_pipe, f"g{num_str}"
                    )

                    # If it feeds a fork, set type to FORK
                    if len(adj[node]) > 2:  # noqa: PLR2004
                        self.pipe_layer.changeAttributeValue(
                            pipe_fid, idx_type_pipe, PipeType.FORK.translated
                        )

                    counter += 1

        self.pipe_layer.commitChanges()
        self.point_layer.commitChanges()

    def _get_conn_pipe_length(self, hc_fid: int) -> float:
        """Get the length of the connecting pipe for a given HC."""
        hc_feat = self.point_layer.getFeature(hc_fid)
        if not hc_feat.isValid():
            return 0.0

        hc_pt = hc_feat.geometry().asPoint()
        pipes = self._get_pipes_at_point(hc_pt)
        conn_type = PipeType.CONN.translated
        type_idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )

        for pipe in pipes:
            if pipe.attribute(type_idx) == conn_type:
                return pipe.geometry().length()
        return 0.0

    def _find_main_node_for_hc(
        self, hc_pt: QgsPointXY, adj: dict
    ) -> tuple[float, float] | None:
        """Find the main pipe node connected to a house connection point."""
        # 1. Check if HC is directly on a main node
        key = (round(hc_pt.x(), 4), round(hc_pt.y(), 4))
        if key in adj:
            return key

        # 2. Check if connected via a CONN pipe
        pipes = self._get_pipes_at_point(hc_pt)
        conn_type = PipeType.CONN.translated
        type_idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )

        for pipe in pipes:
            if pipe.attribute(type_idx) == conn_type:
                endpoints = self._get_endpoints(pipe)
                for p in endpoints:
                    node_key = (round(p.x(), 4), round(p.y(), 4))
                    if node_key in adj:
                        return node_key
        return None

    def _update_conn_pipe_name(self, hc_fid: int, name: str, branch: str) -> None:
        """Update the designation of the connection pipe(s) for a given HC."""
        hc_feat = self.point_layer.getFeature(hc_fid)
        if not hc_feat.isValid():
            return

        hc_pt = hc_feat.geometry().asPoint()
        pipes = self._get_pipes_at_point(hc_pt)
        conn_type = PipeType.CONN.translated
        type_idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )
        desig_idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.designation.field_name
        )
        branch_idx = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.branch.field_name
        )

        for pipe in pipes:
            if pipe.attribute(type_idx) == conn_type:
                self.pipe_layer.changeAttributeValue(pipe.id(), desig_idx, name)
                self.pipe_layer.changeAttributeValue(pipe.id(), branch_idx, branch)
