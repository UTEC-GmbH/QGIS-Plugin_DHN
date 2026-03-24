"""Module: pipe_classifier.py

This module contains the logic to classify pipes in the network, specifically
identifying pipes that connect buildings to the main network.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsFields,
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
    from qgis.core import QgsRectangle


@dataclass(frozen=True)
class Node:
    """Represents a node in the network graph, defined by its coordinates."""

    x: float
    y: float


@dataclass(frozen=True)
class NetworkEdge:
    """Represents an edge in the network graph."""

    pipe_id: int
    neighbor: Node


@dataclass
class NetworkGraph:
    """Holds the graph representation of the pipe network."""

    adjacency: dict[Node, set[NetworkEdge]] = field(default_factory=dict)
    degrees: dict[Node, int] = field(default_factory=dict)


@dataclass
class PipeInfo:
    """Holds information about a pipe segment."""

    dim: int
    length: float


@dataclass(frozen=True)
class BranchStart:
    """Represents the starting point of a new branch."""

    node: Node
    first_pipe_id: int | None


@dataclass
class MainPipeGraph:
    """Graph representation of the main pipe network."""

    adjacency: dict[Node, list[NetworkEdge]]
    pipe_info: dict[int, PipeInfo]


@dataclass
class NetworkOrientation:
    """Orientation of the network relative to a root node."""

    root: Node
    node_depth: dict[Node, int]


@dataclass
class TraversalContext:
    """Holds context data for network traversal."""

    adj: dict[Node, list[NetworkEdge]]
    pipe_info: dict[int, PipeInfo]
    node_depth: dict[Node, int]
    visited_pipes: set[int]
    branch_queue: deque[BranchStart]


@dataclass
class InitializedBranch:
    """Represents a partially traced branch segment."""

    branch_segments: list[NetworkEdge]
    current_node: Node


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
        self._process_network_numbering()

        # 4. Populate connected buildings string (legacy/info field)
        self._populate_connected_buildings()

    def _trace_from_house(self, hc_feat: QgsFeature) -> set[int]:
        """Trace the path from a house connection to the main network.

        Starting from a house connection point, this method traverses the pipe
        network outwards until it encounters a T-piece, which is assumed to be
        part of the main network. All pipes along this path are considered
        connection pipes.

        Args:
            hc_feat: The house connection feature to start tracing from.

        Returns:
            A set of feature IDs for the pipes identified as part of the
            connection path.
        """
        identified_pipes: set[int] = set()

        point_geom: QgsGeometry = hc_feat.geometry()
        if not point_geom:
            return identified_pipes
        start_point: QgsPointXY = point_geom.asPoint()

        # Find the pipe connected to the HC
        start_pipes: list[QgsFeature] = self._get_pipes_at_point(start_point)

        if not start_pipes:
            return identified_pipes

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
            if len(endpoints) != Numbers.min_points_line:
                continue

            p1: QgsPointXY = endpoints[0]
            p2: QgsPointXY = endpoints[1]

            # pick the point furthest from entry_point as exit
            exit_point: QgsPointXY = (
                p2 if p1.distance(entry_point) < p2.distance(entry_point) else p1
            )

            if self._is_t_piece(exit_point):
                # T-Piece found -> Reached the main line. Stop this branch.
                continue

            # Find next pipes at exit point and filter out the pipe we just came from
            next_pipes: list[QgsFeature] = self._get_pipes_at_point(exit_point)
            next_pipes = [p for p in next_pipes if p.id() != current_pipe.id()]

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
        pipes: list[QgsFeature] = []
        for fid in candidate_ids:
            feat: QgsFeature = self.pipe_layer.getFeature(fid)
            if feat.geometry().intersects(search_geom):
                pipes.append(feat)
        return pipes

    def _get_endpoints(self, feature: QgsFeature) -> list[QgsPointXY]:
        """Get the start and end points of a line feature.

        Handles both LineString and MultiLineString geometries. For MultiLineString,
        it returns the start of the first part and the end of the last part.

        Args:
            feature: The line feature.

        Returns:
            A list containing the start and end points, or an empty list if
            the geometry is invalid.
        """
        geom: QgsGeometry = feature.geometry()
        if not geom:
            return []

        # Handle MultiLineString
        if geom.wkbType() == QgsWkbTypes.MultiLineString:
            lines = geom.asMultiPolyline()
            return [lines[0][0], lines[-1][-1]] if lines else []
        if geom.wkbType() == QgsWkbTypes.LineString:
            polyline = geom.asPolyline()
            if len(polyline) < Numbers.min_points_line:
                return []
            return [polyline[0], polyline[-1]]

        return []

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
            if fid in self.point_types:
                t_type: str = self.point_types[fid]
                if t_type == FittingType.T_PIECE.translated:
                    # Double check geometry intersection
                    feat: QgsFeature = self.point_layer.getFeature(fid)
                    if feat.geometry().intersects(search_geom):
                        return True
        return False

    def _populate_connected_buildings(self) -> dict[int, set[str]]:
        """Identify and list connected buildings for each pipe segment.

        This method builds a graph of the entire pipe network and uses a "leaf
        peeling" algorithm. It starts from the leaves of the graph (typically
        house connections) and propagates their designation strings up through
        the network branches, accumulating them on each pipe segment.

        Returns:
            A dictionary mapping each pipe feature ID to a set of house
            connection designation strings it serves.
        """
        log_debug("Populating connected buildings...")

        # 1. Build Graph: Node -> Set of (PipeID, NeighborNode)
        graph: NetworkGraph = self._build_network_graph()

        # 2. Map House Connections to Graph Nodes
        node_hcs: dict[Node, set[str]] = self._map_hcs_to_nodes()

        # 3. Peeling Algorithm (Propagate from leaves)
        pipe_hcs: dict[int, set[str]] = self._propagate_hcs_from_leaves(graph, node_hcs)

        # 4. Write results to layer
        self._write_connected_buildings(pipe_hcs)

        return pipe_hcs

    def _build_network_graph(self) -> NetworkGraph:
        """Build the graph representation of the pipe network.

        Returns:
            A NetworkGraph object containing the adjacency list and node degrees.
        """
        # Node is a tuple of (x, y) rounded to 4 decimals.
        graph = NetworkGraph()

        for feature in self.pipe_layer.getFeatures():
            endpoints: list[QgsPointXY] = self._get_endpoints(feature)
            if len(endpoints) != Numbers.min_points_line:
                continue

            p1 = Node(round(endpoints[0].x(), 4), round(endpoints[0].y(), 4))
            p2 = Node(round(endpoints[1].x(), 4), round(endpoints[1].y(), 4))
            fid = feature.id()

            graph.adjacency.setdefault(p1, set()).add(NetworkEdge(fid, p2))
            graph.adjacency.setdefault(p2, set()).add(NetworkEdge(fid, p1))

            graph.degrees[p1] = graph.degrees.get(p1, 0) + 1
            graph.degrees[p2] = graph.degrees.get(p2, 0) + 1

        return graph

    def _map_hcs_to_nodes(self) -> dict[Node, set[str]]:
        """Map house connection points to graph nodes.

        Returns:
            A dictionary mapping node coordinates (Node) to sets of HC designations.
        """
        node_hcs: dict[Node, set[str]] = {}
        req: QgsFeatureRequest = QgsFeatureRequest().setFilterExpression(
            f'"{NewPointLayerFields.type.field_name}" = '
            f"'{FittingType.HOUSE_CONN.translated}'"
        )
        for feat in self.point_layer.getFeatures(req):
            if geom := feat.geometry():
                p = geom.asPoint()
                key = Node(round(p.x(), 4), round(p.y(), 4))
                desig = feat.attribute(NewPointLayerFields.designation.field_name)
                val: str = str(desig) if desig else str(feat.id())
                node_hcs.setdefault(key, set()).add(val)
        return node_hcs

    def _propagate_hcs_from_leaves(
        self,
        graph: NetworkGraph,
        node_hcs: dict[Node, set[str]],
    ) -> dict[int, set[str]]:
        """Propagate house connection info from leaves up the network.

        This method implements a "leaf peeling" algorithm. It iteratively
        removes leaf nodes (nodes with degree 1) from the graph, propagating
        their associated house connection (HC) designations to the parent pipe
        and node. This process continues until no more leaf nodes can be
        removed.

        Args:
            graph: The NetworkGraph object representing the pipe network.
            node_hcs: The initial mapping of HC designations to their graph nodes.

        Returns:
            A dictionary mapping each pipe feature ID to a set of all HC
            designations it helps to serve.
        """
        pipe_hcs: dict[int, set[str]] = {}
        leaves: list[Node] = [n for n, d in graph.degrees.items() if d == 1]

        while leaves:
            leaf: Node = leaves.pop(0)
            if graph.degrees[leaf] == 0:
                continue

            # Get the single connected edge
            if not graph.adjacency[leaf]:
                continue
            edge: NetworkEdge = next(iter(graph.adjacency[leaf]))
            fid: int = edge.pipe_id
            neighbor: Node = edge.neighbor

            if hcs := node_hcs.get(leaf, set()):
                pipe_hcs.setdefault(fid, set()).update(hcs)
                node_hcs.setdefault(neighbor, set()).update(hcs)

            # Remove edge from graph
            graph.adjacency[leaf].remove(edge)

            neighbor_edge = NetworkEdge(fid, leaf)
            if neighbor_edge in graph.adjacency[neighbor]:
                graph.adjacency[neighbor].remove(neighbor_edge)

            graph.degrees[leaf] -= 1
            graph.degrees[neighbor] -= 1

            # If neighbor becomes a leaf, add to queue
            if graph.degrees[neighbor] == 1:
                leaves.append(neighbor)

        return pipe_hcs

    def _write_connected_buildings(self, pipe_hcs: dict[int, set[str]]) -> None:
        """Write the connected buildings attribute to the pipe layer.

        Args:
            pipe_hcs: Dictionary mapping pipe FIDs to sets of connected HC strings.
        """
        self.pipe_layer.startEditing()
        idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.conn_buildings.field_name
        )
        if idx != -1:
            for fid, hcs in pipe_hcs.items():
                val: str = Names.separator.join(sorted(hcs))
                self.pipe_layer.changeAttributeValue(fid, idx, val)
        self.pipe_layer.commitChanges()

    def _process_network_numbering(self) -> None:
        """Orchestrate the network numbering process.

        This method assigns branch numbers and unique designations to each pipe
        segment in the main network. It involves building a graph, orienting it,
        tracing the branches, and finally applying the calculated numbers to the
        layer attributes.
        """
        log_debug("Processing network numbering...")

        # 1. Build a graph of the main pipe network.
        main_graph: MainPipeGraph = self._build_main_pipe_graph()
        if not main_graph.adjacency:
            return

        # 2. Orient the network by depth, starting from a root node.
        orientation: NetworkOrientation = self._orient_network_by_depth(
            main_graph.adjacency
        )

        # 3. Trace the main branches of the network.
        branches: list[list[NetworkEdge]] = self._trace_network_branches(
            main_graph.adjacency,
            main_graph.pipe_info,
            orientation.root,
            orientation.node_depth,
        )

        # 4. Map house connections to their corresponding main network nodes.
        node_hcs: dict[Node, list[int]] = self._map_hcs_to_main_nodes(
            main_graph.adjacency
        )

        # 5. Apply the calculated branch and designation numbering to the layers.
        self._apply_numbering(branches, node_hcs, main_graph.adjacency)

    def _build_main_pipe_graph(self) -> MainPipeGraph:
        """Build a graph representation of the main pipe network.

        This method iterates through the pipe layer, selecting only pipes marked
        as 'Main Pipe' or 'Main Pipe (Fork)' to construct a graph.

        Returns:
            A MainPipeGraph object containing the adjacency list and pipe info
            (dimension and length) for the main network.
        """
        adj: dict[Node, list[NetworkEdge]] = {}
        pipe_info: dict[int, PipeInfo] = {}

        fields: QgsFields = self.pipe_layer.fields()
        type_idx: int = fields.lookupField(NewLineLayerFields.type.field_name)
        dim_idx: int = fields.lookupField(NewLineLayerFields.dim.field_name)
        main_types: set[str] = {PipeType.MAIN.translated, PipeType.FORK.translated}

        for feat in self.pipe_layer.getFeatures():
            if feat.attribute(type_idx) not in main_types:
                continue

            endpoints: list[QgsPointXY] = self._get_endpoints(feat)
            if len(endpoints) != Numbers.min_points_line:
                continue

            fid = feat.id()
            p1 = Node(round(endpoints[0].x(), 4), round(endpoints[0].y(), 4))
            p2 = Node(round(endpoints[1].x(), 4), round(endpoints[1].y(), 4))
            adj.setdefault(p1, []).append(NetworkEdge(fid, p2))
            adj.setdefault(p2, []).append(NetworkEdge(fid, p1))

            dim_val = feat.attribute(dim_idx)
            try:
                dim: int = int(dim_val) if dim_val is not None else 0
            except (ValueError, TypeError):
                dim = 0

            if geom := feat.geometry():
                pipe_info[fid] = PipeInfo(dim=dim, length=geom.length())

        return MainPipeGraph(adjacency=adj, pipe_info=pipe_info)

    def _orient_network_by_depth(
        self, adj: dict[Node, list[NetworkEdge]]
    ) -> NetworkOrientation:
        """Orient the network by calculating node depth via BFS from a root.

        The root is chosen as the node with the highest degree (most connections),
        which is a heuristic for the network's source (e.g., heating plant).

        Args:
            adj: The adjacency list of the network graph.

        Returns:
            A NetworkOrientation object with the root node and node depths.
        """
        root: Node = max(adj, key=lambda n: len(adj[n]))
        node_depth: dict[Node, int] = {root: 0}
        queue: deque[Node] = deque([root])

        while queue:
            u = queue.popleft()
            for edge in adj.get(u, []):
                if edge.neighbor not in node_depth:
                    node_depth[edge.neighbor] = node_depth[u] + 1
                    queue.append(edge.neighbor)

        return NetworkOrientation(root=root, node_depth=node_depth)

    def _trace_network_branches(
        self,
        adj: dict[Node, list[NetworkEdge]],
        pipe_info: dict[int, PipeInfo],
        root: Node,
        node_depth: dict[Node, int],
    ) -> list[list[NetworkEdge]]:
        """Trace the main branches of the network.

        This method uses a main path-first strategy. From any junction, it
        continues the current branch along the "largest" pipe (by diameter, then
        length). Other outgoing pipes from the junction become starting points
        for new sub-branches.

        Args:
            adj: The adjacency list of the network graph.
            pipe_info: A dictionary with information about each pipe.
            root: The root node of the network.
            node_depth: A dictionary mapping nodes to their depth.

        Returns:
            A list of branches, where each branch is a list of NetworkEdge objects.
        """
        branches: list[list[NetworkEdge]] = []
        visited_pipes: set[int] = set()
        branch_queue: deque[BranchStart] = deque([BranchStart(root, None)])

        context = TraversalContext(
            adj=adj,
            pipe_info=pipe_info,
            node_depth=node_depth,
            visited_pipes=visited_pipes,
            branch_queue=branch_queue,
        )

        while branch_queue:
            branch_start: BranchStart = branch_queue.popleft()

            initialized: InitializedBranch = self._initialize_branch(
                branch_start, context
            )
            current_branch: list[NetworkEdge] = initialized.branch_segments
            curr_node: Node = initialized.current_node

            if branch_start.first_pipe_id is not None and not current_branch:
                continue

            self._extend_branch(current_branch, curr_node, context)

            if current_branch:
                branches.append(current_branch)

        return branches

    def _initialize_branch(
        self,
        branch_start: BranchStart,
        context: TraversalContext,
    ) -> InitializedBranch:
        """Initialize a new branch segment.

        If this is a sub-branch (first_pipe_fid is not None), it adds that
        initial pipe to the branch and marks it visited.

        Args:
            branch_start: The starting configuration for this branch segment.
            context: The traversal context containing graph and state.

        Returns:
            An InitializedBranch object containing the initial segments and the
            node to continue from.
        """
        current_branch: list[NetworkEdge] = []
        curr_node: Node = branch_start.node

        if branch_start.first_pipe_id is not None and (
            edge := next(
                (
                    e
                    for e in context.adj.get(curr_node, [])
                    if e.pipe_id == branch_start.first_pipe_id
                ),
                None,
            )
        ):
            current_branch.append(edge)
            context.visited_pipes.add(edge.pipe_id)
            curr_node = edge.neighbor

        return InitializedBranch(branch_segments=current_branch, current_node=curr_node)

    def _extend_branch(
        self,
        current_branch: list[NetworkEdge],
        start_node: Node,
        context: TraversalContext,
    ) -> None:
        """Extend the branch along the main path and queue side branches.

        Args:
            current_branch: The list of pipes in the current branch.
            start_node: The node to start extending from.
            context: The traversal context containing graph and state.
        """
        curr_node: Node = start_node
        while True:
            candidates: list[NetworkEdge] = self._get_sorted_candidates(
                curr_node, context
            )

            if not candidates:
                break

            # The best candidate continues the current branch
            best_edge: NetworkEdge = candidates[0]
            current_branch.append(best_edge)
            context.visited_pipes.add(best_edge.pipe_id)

            # Other candidates form new branches starting from the current fork
            for other_edge in candidates[1:]:
                context.branch_queue.append(BranchStart(curr_node, other_edge.pipe_id))

            curr_node = best_edge.neighbor

    def _get_sorted_candidates(
        self,
        node: Node,
        context: TraversalContext,
    ) -> list[NetworkEdge]:
        """Get valid outgoing pipes sorted by dimension and length.

        This method finds all unvisited, "forward" (away from the root) pipes
        connected to the given node and sorts them in descending order of
        priority (diameter, then length).

        Args:
            node: The current node.
            context: The traversal context containing graph and state.

        Returns:
            A sorted list of candidate NetworkEdge objects.
        """
        candidates: list[NetworkEdge] = []
        current_depth: int = context.node_depth.get(node, 0)

        for edge in context.adj.get(node, []):
            if edge.pipe_id in context.visited_pipes:
                continue

            # Heuristic: Don't go "backwards" against the depth-first orientation
            if context.node_depth.get(edge.neighbor, 0) < current_depth:
                continue
            candidates.append(edge)

        # Sort candidates to find the main path: diameter then length
        candidates.sort(
            key=lambda e: (
                context.pipe_info[e.pipe_id].dim,
                context.pipe_info[e.pipe_id].length,
            ),
            reverse=True,
        )
        return candidates

    def _map_hcs_to_main_nodes(
        self, adj: dict[Node, list[NetworkEdge]]
    ) -> dict[Node, list[int]]:
        """Map house connection points to their corresponding main network nodes.

        Args:
            adj: The adjacency list of the main network graph.

        Returns:
            A dictionary mapping main network nodes to a list of connected HC FIDs.
        """
        node_hcs: dict[Node, list[int]] = {}
        hc_req: QgsFeatureRequest = QgsFeatureRequest().setFilterExpression(
            f'"{NewPointLayerFields.type.field_name}" = '
            f"'{FittingType.HOUSE_CONN.translated}'"
        )

        for hc_feat in self.point_layer.getFeatures(hc_req):
            if not (geom := hc_feat.geometry()):
                continue
            hc_pt = geom.asPoint()
            # Trace from the HC point to find the main node it connects to
            if main_node := self._find_main_node_for_hc(hc_pt, adj):
                node_hcs.setdefault(main_node, []).append(hc_feat.id())

        return node_hcs

    def _apply_numbering(
        self,
        branches: list[list[NetworkEdge]],
        node_hcs: dict[Node, list[int]],
        adj: dict[Node, list[NetworkEdge]],
    ) -> None:
        """Apply branch and designation numbering to pipe and point layers.

        Args:
            branches: The list of traced network branches.
            node_hcs: A mapping of main nodes to connected house connections.
            adj: The adjacency list of the main network graph.
        """
        # Get field indices
        pipe_fields: QgsFields = self.pipe_layer.fields()
        idx_desig_pipe: int = pipe_fields.lookupField(
            NewLineLayerFields.designation.field_name
        )
        idx_branch_pipe: int = pipe_fields.lookupField(
            NewLineLayerFields.branch.field_name
        )
        idx_type_pipe: int = pipe_fields.lookupField(NewLineLayerFields.type.field_name)

        idx_desig_pt: int = self.point_layer.fields().lookupField(
            NewPointLayerFields.designation.field_name
        )

        self.pipe_layer.startEditing()
        self.point_layer.startEditing()

        for i, branch_pipes in enumerate(branches, 1):
            branch_id: str = f"{i:02d}"
            counter = 1

            for edge in branch_pipes:
                self.pipe_layer.changeAttributeValue(
                    edge.pipe_id, idx_branch_pipe, branch_id
                )

                if hcs := node_hcs.get(edge.neighbor, []):
                    # Sort HCs by connecting pipe length for consistent numbering
                    hcs.sort(key=self._get_conn_pipe_length, reverse=True)

                    for hc_fid in hcs:
                        num_str: str = f"{branch_id}-{counter:03d}"
                        self.point_layer.changeAttributeValue(
                            hc_fid, idx_desig_pt, num_str
                        )
                        self._update_conn_pipe_name(hc_fid, f"a{num_str}", branch_id)
                        self.pipe_layer.changeAttributeValue(
                            edge.pipe_id, idx_desig_pipe, f"v{num_str}"
                        )
                        self.pipe_layer.changeAttributeValue(
                            edge.pipe_id, idx_type_pipe, PipeType.MAIN.translated
                        )
                        counter += 1
                else:
                    # No HC at this node, it's a junction or bend
                    num_str = f"{branch_id}-{counter:03d}"
                    self.pipe_layer.changeAttributeValue(
                        edge.pipe_id, idx_desig_pipe, f"g{num_str}"
                    )
                    # If it's a fork (feeds more than one continuing branch)
                    if len(adj.get(edge.neighbor, [])) > Numbers.min_intersec:
                        self.pipe_layer.changeAttributeValue(
                            edge.pipe_id, idx_type_pipe, PipeType.FORK.translated
                        )
                    counter += 1

        self.pipe_layer.commitChanges()
        self.point_layer.commitChanges()

    def _get_conn_pipe_length(self, hc_fid: int) -> float:
        """Get the length of the connecting pipe for a given HC.

        Args:
            hc_fid: The feature ID of the house connection point.

        Returns:
            The length of the connected pipe, or 0.0 if not found.
        """
        hc_feat: QgsFeature = self.point_layer.getFeature(hc_fid)
        if not hc_feat.isValid():
            return 0.0

        hc_pt: QgsPointXY = hc_feat.geometry().asPoint()
        pipes: list[QgsFeature] = self._get_pipes_at_point(hc_pt)
        conn_type = PipeType.CONN.translated
        type_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )

        for pipe in pipes:
            if (pipe.attribute(type_idx) == conn_type) and (geom := pipe.geometry()):
                return geom.length()
        return 0.0

    def _find_main_node_for_hc(
        self, hc_pt: QgsPointXY, adj: dict[Node, list[NetworkEdge]]
    ) -> Node | None:
        """Find the main pipe node connected to a house connection point.

        This method first checks if the HC point is directly on a main network
        node. If not, it checks for any attached 'Connecting Pipe' and follows
        it to find the main node.

        Args:
            hc_pt: The point geometry of the house connection.
            adj: The adjacency list of the main network graph.

        Returns:
            The connected main network node, or None if not found.
        """
        # 1. Check if HC is directly on a main node
        key = Node(round(hc_pt.x(), 4), round(hc_pt.y(), 4))
        if key in adj:
            return key

        # 2. Check if connected via a CONN pipe
        pipes: list[QgsFeature] = self._get_pipes_at_point(hc_pt)
        conn_type = PipeType.CONN.translated
        type_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )

        for pipe in pipes:
            if pipe.attribute(type_idx) == conn_type:
                endpoints: list[QgsPointXY] = self._get_endpoints(pipe)
                for p in endpoints:
                    node_key = Node(round(p.x(), 4), round(p.y(), 4))
                    if node_key in adj:
                        return node_key
        return None

    def _update_conn_pipe_name(self, hc_fid: int, name: str, branch: str) -> None:
        """Update the designation of the connection pipe(s) for a given HC.

        Args:
            hc_fid: The feature ID of the house connection point.
            name: The new designation name for the pipe.
            branch: The branch ID to assign to the pipe.
        """
        hc_feat: QgsFeature = self.point_layer.getFeature(hc_fid)
        if not hc_feat.isValid():
            return

        hc_pt: QgsPointXY = hc_feat.geometry().asPoint()
        pipes: list[QgsFeature] = self._get_pipes_at_point(hc_pt)
        conn_type = PipeType.CONN.translated
        type_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )
        desig_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.designation.field_name
        )
        branch_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.branch.field_name
        )

        for pipe in pipes:
            if pipe.attribute(type_idx) == conn_type:
                self.pipe_layer.changeAttributeValue(pipe.id(), desig_idx, name)
                self.pipe_layer.changeAttributeValue(pipe.id(), branch_idx, branch)
