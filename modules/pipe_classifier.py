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

        pipe_hcs: dict[int, set[str]] = self._populate_connected_buildings()

        main_pipe_branches: dict[int, str] = self._assign_branches()
        all_pipe_branches: dict[int, str] = self._propagate_branches_to_connections(
            main_pipe_branches
        )
        self._generate_designations(pipe_hcs, all_pipe_branches)

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

    def _assign_branches(self) -> dict[int, str]:
        """Identifies network branches for main pipes.

        A branch is a sequence of main pipes between fork points (junctions with
        3 or more main pipes) or endpoints.

        Returns:
            A dictionary mapping main pipe FIDs to their assigned branch number.
        """
        log_debug("Identifying network branches...")

        # 1. Build graph of main pipes only
        adj: dict[tuple[float, float], list[tuple[int, tuple[float, float]]]] = {}
        main_pipe_fids: set[int] = set()
        type_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )
        main_type: str = PipeType.MAIN.translated

        for feature in self.pipe_layer.getFeatures():
            if feature.attribute(type_idx) == main_type:
                fid: int = feature.id()
                main_pipe_fids.add(fid)
                endpoints: list[QgsPointXY] = self._get_endpoints(feature)
                if len(endpoints) != 2:  # noqa: PLR2004
                    continue

                p1: tuple[float, float] = (
                    round(endpoints[0].x(), 4),
                    round(endpoints[0].y(), 4),
                )
                p2: tuple[float, float] = (
                    round(endpoints[1].x(), 4),
                    round(endpoints[1].y(), 4),
                )

                adj.setdefault(p1, []).append((fid, p2))
                adj.setdefault(p2, []).append((fid, p1))

        # 2. Identify fork nodes (degree >= 3)
        fork_nodes: set[tuple[float, float]] = {
            node for node, connections in adj.items() if len(connections) >= 3
        }

        # 3. Traverse and assign branches using BFS
        pipe_branches: dict[int, str] = {}
        visited_pipes: set[int] = set()
        branch_counter: int = 1

        for fid in main_pipe_fids:
            if fid in visited_pipes:
                continue

            branch_id: str = f"{branch_counter:02d}"
            branch_counter += 1

            queue: list[int] = [fid]
            visited_pipes.add(fid)

            head: int = 0
            while head < len(queue):
                current_fid: int = queue[head]
                head += 1

                pipe_branches[current_fid] = branch_id

                feature = self.pipe_layer.getFeature(current_fid)
                endpoints = self._get_endpoints(feature)
                if len(endpoints) != 2:  # noqa: PLR2004
                    continue

                for p in endpoints:
                    node: tuple[float, float] = (round(p.x(), 4), round(p.y(), 4))
                    if node in fork_nodes:
                        continue  # Stop traversal at forks

                    for neighbor_fid, _ in adj.get(node, []):
                        if neighbor_fid not in visited_pipes:
                            visited_pipes.add(neighbor_fid)
                            queue.append(neighbor_fid)

        log_debug(f"Identified {branch_counter - 1} main branches.")
        return pipe_branches

    def _propagate_branches_to_connections(
        self, pipe_branches: dict[int, str]
    ) -> dict[int, str]:
        """Propagates branch numbers from main pipes to connected connection pipes."""
        log_debug("Propagating branch numbers to connection pipes...")

        all_branches = pipe_branches.copy()

        # 1. Identify Connection Pipes and build connectivity map
        conn_pipes: list[int] = []
        node_pipes: dict[tuple[float, float], list[int]] = {}

        type_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )
        conn_type: str = PipeType.CONN.translated

        for feature in self.pipe_layer.getFeatures():
            fid = feature.id()
            if feature.attribute(type_idx) == conn_type:
                conn_pipes.append(fid)

            endpoints = self._get_endpoints(feature)
            for p in endpoints:
                key = (round(p.x(), 4), round(p.y(), 4))
                node_pipes.setdefault(key, []).append(fid)

        # 2. Propagate branches iteratively
        # We iterate until no more pipes can be assigned.
        unassigned = set(conn_pipes)
        # Safety: ensure we don't try to assign if already assigned
        unassigned -= set(all_branches.keys())

        changed = True
        while changed:
            changed = False
            assigned_in_this_pass = {}

            for fid in unassigned:
                feature = self.pipe_layer.getFeature(fid)
                endpoints = self._get_endpoints(feature)

                found_branch = None
                for p in endpoints:
                    key = (round(p.x(), 4), round(p.y(), 4))
                    neighbors = node_pipes.get(key, [])

                    for n_fid in neighbors:
                        if n_fid == fid:
                            continue
                        if n_fid in all_branches:
                            found_branch = all_branches[n_fid]
                            break
                    if found_branch:
                        break

                if found_branch:
                    assigned_in_this_pass[fid] = found_branch

            if assigned_in_this_pass:
                all_branches.update(assigned_in_this_pass)
                unassigned -= set(assigned_in_this_pass.keys())
                changed = True

        log_debug(
            f"Propagated branches to {len(conn_pipes) - len(unassigned)} "
            f"connection pipes."
        )
        return all_branches

    def _generate_designations(
        self, pipe_hcs: dict[int, set[str]], pipe_branches: dict[int, str]
    ) -> None:
        """Generate and assign designations, types, and branches to pipes.

        Args:
            pipe_hcs: A dictionary mapping pipe FIDs to the set of house
                connection IDs they serve.
            pipe_branches: A dictionary mapping all pipe FIDs to their
                assigned branch number.
        """
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

        # 1. Update branch attribute for all pipes that have a branch number
        for fid, branch_num in pipe_branches.items():
            self.pipe_layer.changeAttributeValue(fid, idx_branch, branch_num)

        # Keep track of assigned building designations to avoid re-numbering
        # Map: building_fid -> designation_string
        building_map: dict[int, str] = {}
        building_counter = 1

        for fid, hcs in pipe_hcs.items():
            if not hcs:
                continue

            feature = self.pipe_layer.getFeature(fid)
            type_val = feature.attribute(idx_type)

            # Get the branch number assigned in the previous step
            branch_num: str = pipe_branches.get(fid, "99")
            designation = ""

            # Helper to format building ID (e.g., '5' -> '005')
            def fmt_id(val: str) -> str:
                try:
                    return f"{int(val):03d}"
                except ValueError:
                    return val

            if type_val == PipeType.CONN.translated:
                # Type 'a': Use the connected building ID
                target_fid = int(sorted(hcs, key=int)[0])

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
                        self.pipe_layer.getFeature(f_id).attribute(idx_type)
                        == PipeType.MAIN.translated
                        for f_id in other_fids
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
                    target_fid = int(sorted(hcs, key=int)[0])

                    # If we haven't encountered this building via a connection pipe yet,
                    # we assign it a number now
                    # (though usually 'a' pipes are processed too)
                    if target_fid not in building_map:
                        b_desig = f"{branch_num}-{fmt_id(str(building_counter))}"
                        building_map[target_fid] = b_desig
                        self.point_layer.changeAttributeValue(
                            target_fid, idx_pt_desig, b_desig
                        )
                        building_counter += 1

                    designation = f"v{building_map[target_fid]}"

            if designation:
                self.pipe_layer.changeAttributeValue(fid, idx_desig, designation)

        self.pipe_layer.commitChanges()
        self.point_layer.commitChanges()
