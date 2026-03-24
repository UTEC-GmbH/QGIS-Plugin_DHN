"""Module: network_numbering.py

This module contains the logic to number the network, assigning branches
and designations to pipes.
"""

from collections import deque
from typing import TYPE_CHECKING

from qgis.core import (
    QgsFeature,
    QgsFeatureRequest,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorLayer,
)

from .constants import (
    FittingType,
    NewLineLayerFields,
    NewPointLayerFields,
    Numbers,
    PipeType,
)
from .graph_definitions import (
    BranchStart,
    InitializedBranch,
    MainPipeGraph,
    NetworkEdge,
    NetworkOrientation,
    Node,
    PipeInfo,
    TraversalContext,
)
from .logs_and_errors import log_debug
from .vector_analysis_tools import VectorAnalysisTools

if TYPE_CHECKING:
    from qgis.core import QgsRectangle


class NetworkNumberer:
    """Classifies and numbers network branches."""

    def __init__(self, pipe_layer: QgsVectorLayer, point_layer: QgsVectorLayer) -> None:
        """Initialize the NetworkNumberer.

        Args:
            pipe_layer: The vector layer containing the pipe network.
            point_layer: The vector layer containing the point features (fittings).
        """
        self.pipe_layer: QgsVectorLayer = pipe_layer
        self.point_layer: QgsVectorLayer = point_layer
        # Build spatial index locally for pipe lookups
        self.pipe_index = QgsSpatialIndex(pipe_layer.getFeatures())

    def run(self) -> None:
        """Orchestrate the network numbering process.

        This method assigns branch numbers and unique designations to each pipe
        segment in the main network.
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
        branches: list[tuple[Node, list[NetworkEdge]]] = self._trace_network_branches(
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

        Returns:
            A MainPipeGraph object containing the adjacency list and pipe info.
        """
        adjacency_map: dict[Node, list[NetworkEdge]] = {}
        pipe_info: dict[int, PipeInfo] = {}

        fields: QgsFields = self.pipe_layer.fields()
        type_idx: int = fields.lookupField(NewLineLayerFields.type.field_name)
        dim_idx: int = fields.lookupField(NewLineLayerFields.dim.field_name)
        main_types: set[str] = {PipeType.MAIN.translated, PipeType.FORK.translated}

        for feat in self.pipe_layer.getFeatures():
            if feat.attribute(type_idx) not in main_types:
                continue

            endpoints: list[QgsPointXY] = VectorAnalysisTools.get_start_end_of_line(
                feat
            )
            if len(endpoints) != Numbers.min_points_line:
                continue

            fid = feat.id()
            node_1 = Node(round(endpoints[0].x(), 4), round(endpoints[0].y(), 4))
            node_2 = Node(round(endpoints[1].x(), 4), round(endpoints[1].y(), 4))
            adjacency_map.setdefault(node_1, []).append(NetworkEdge(fid, node_2))
            adjacency_map.setdefault(node_2, []).append(NetworkEdge(fid, node_1))

            dim_val = feat.attribute(dim_idx)
            try:
                dim: int = int(dim_val) if dim_val is not None else 0
            except (ValueError, TypeError):
                dim = 0

            if geom := feat.geometry():
                pipe_info[fid] = PipeInfo(dim=dim, length=geom.length())

        return MainPipeGraph(adjacency=adjacency_map, pipe_info=pipe_info)

    def _orient_network_by_depth(
        self, adjacency_map: dict[Node, list[NetworkEdge]]
    ) -> NetworkOrientation:
        """Orient the network by calculating node depth via BFS from a root.

        Args:
            adjacency_map: The adjacency list of the graph.

        Returns:
            A NetworkOrientation object containing the root node and depth map.
        """
        root: Node = max(adjacency_map, key=lambda n: len(adjacency_map[n]))
        node_depth: dict[Node, int] = {root: 0}
        queue: deque[Node] = deque([root])

        while queue:
            current_node: Node = queue.popleft()
            for edge in adjacency_map.get(current_node, []):
                if edge.neighbor not in node_depth:
                    node_depth[edge.neighbor] = node_depth[current_node] + 1
                    queue.append(edge.neighbor)

        return NetworkOrientation(root=root, node_depth=node_depth)

    def _trace_network_branches(
        self,
        adjacency_map: dict[Node, list[NetworkEdge]],
        pipe_info: dict[int, PipeInfo],
        root: Node,
        node_depth: dict[Node, int],
    ) -> list[tuple[Node, list[NetworkEdge]]]:
        """Trace the main branches of the network.

        Args:
            adjacency_map: The adjacency list of the network graph.
            pipe_info: A dictionary containing information about pipes.
            root: The root node of the network.
            node_depth: A dictionary mapping nodes to their depth.

        Returns:
            A list of tuples, each containing a start Node and a list of NetworkEdges
            representing a branch.
        """
        branches: list[tuple[Node, list[NetworkEdge]]] = []
        visited_pipes: set[int] = set()
        branch_queue: deque[BranchStart] = deque([BranchStart(root, None)])

        context = TraversalContext(
            adj=adjacency_map,
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
                branches.append((branch_start.node, current_branch))

        return branches

    def _initialize_branch(
        self,
        branch_start: BranchStart,
        context: TraversalContext,
    ) -> InitializedBranch:
        """Initialize a new branch segment.

        Args:
            branch_start: The starting configuration for the branch.
            context: The traversal context.

        Returns:
            An InitializedBranch object containing the branch segments and current node.
        """
        current_branch: list[NetworkEdge] = []
        curr_node: Node = branch_start.node

        if branch_start.first_pipe_id is not None and (
            edge := next(
                (
                    edge_candidate
                    for edge_candidate in context.adj.get(curr_node, [])
                    if edge_candidate.pipe_id == branch_start.first_pipe_id
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
            current_branch: The list of edges currently in the branch.
            start_node: The node to start extending from.
            context: The traversal context.
        """
        curr_node: Node = start_node
        while True:
            candidates: list[NetworkEdge] = self._get_sorted_candidates(
                curr_node, context
            )

            if not candidates:
                break

            best_edge: NetworkEdge = candidates[0]
            current_branch.append(best_edge)
            context.visited_pipes.add(best_edge.pipe_id)

            for other_edge in candidates[1:]:
                context.branch_queue.append(BranchStart(curr_node, other_edge.pipe_id))

            curr_node = best_edge.neighbor

    def _get_sorted_candidates(
        self,
        node: Node,
        context: TraversalContext,
    ) -> list[NetworkEdge]:
        """Get valid outgoing pipes sorted by dimension and length.

        Args:
            node: The current node.
            context: The traversal context.

        Returns:
            A list of NetworkEdge candidates sorted by dimension and length.
        """
        current_depth: int = context.node_depth.get(node, 0)

        # Heuristic: Don't go "backwards" against the depth-first orientation
        candidates: list[NetworkEdge] = [
            edge
            for edge in context.adj.get(node, [])
            if edge.pipe_id not in context.visited_pipes
            and context.node_depth.get(edge.neighbor, 0) >= current_depth
        ]

        candidates.sort(
            key=lambda edge_item: (
                context.pipe_info[edge_item.pipe_id].dim,
                context.pipe_info[edge_item.pipe_id].length,
            ),
            reverse=True,
        )
        return candidates

    def _map_hcs_to_main_nodes(
        self, adjacency_map: dict[Node, list[NetworkEdge]]
    ) -> dict[Node, list[int]]:
        """Map house connection points to their corresponding main network nodes.

        Args:
            adjacency_map: The adjacency list of the main network.

        Returns:
            A dictionary mapping main network nodes to lists of house connection
            feature IDs.
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
            if main_node := self._find_main_node_for_hc(hc_pt, adjacency_map):
                node_hcs.setdefault(main_node, []).append(hc_feat.id())

        return node_hcs

    def _apply_numbering(
        self,
        branches: list[tuple[Node, list[NetworkEdge]]],
        node_hcs: dict[Node, list[int]],
        adjacency_map: dict[Node, list[NetworkEdge]],
    ) -> None:
        """Apply branch and designation numbering to pipe and point layers.

        Args:
            branches: A list of branches, where each branch is a tuple of
                (start_node, list of edges).
            node_hcs: A dictionary mapping nodes to connected house connections.
            adjacency_map: The adjacency list of the main network.
        """
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

        for index, (start_node, branch_pipes) in enumerate(branches, 1):
            branch_id: str = f"{index:02d}"

            # Build list of (edge, u, v) to allow checking both nodes later
            annotated_edges: list[tuple[NetworkEdge, Node, Node]] = []
            curr_node = start_node
            for edge in branch_pipes:
                annotated_edges.append((edge, curr_node, edge.neighbor))
                curr_node = edge.neighbor

            counter = 1

            # Iterate in reverse to start numbering from the tip
            for edge, node_start, node_end in reversed(annotated_edges):
                degree_start = len(adjacency_map.get(node_start, []))
                degree_end = len(adjacency_map.get(node_end, []))
                is_fork = (
                    degree_start > Numbers.min_intersec
                    or degree_end > Numbers.min_intersec
                )

                self.pipe_layer.changeAttributeValue(
                    edge.pipe_id, idx_branch_pipe, branch_id
                )

                pipe_designation = ""

                # Check for HCs at the end of the segment (v)
                if hcs := node_hcs.get(node_end, []):
                    hcs.sort(key=self._get_conn_pipe_length, reverse=True)

                    for hc_fid in hcs:
                        num_str: str = f"{branch_id}-{counter:03d}"
                        self.point_layer.changeAttributeValue(
                            hc_fid, idx_desig_pt, num_str
                        )
                        self._update_conn_pipe_name(hc_fid, f"a{num_str}", branch_id)

                        pipe_designation = f"v{num_str}"
                        counter += 1
                else:
                    num_str = f"{branch_id}-{counter:03d}"
                    pipe_designation = f"g{num_str}"
                    counter += 1

                # Apply fork overrides (all pipes connected to a fork start with 'g')
                if is_fork:
                    pipe_designation = f"g{pipe_designation[1:]}"
                    self.pipe_layer.changeAttributeValue(
                        edge.pipe_id, idx_type_pipe, PipeType.FORK.translated
                    )
                else:
                    self.pipe_layer.changeAttributeValue(
                        edge.pipe_id, idx_type_pipe, PipeType.MAIN.translated
                    )

                self.pipe_layer.changeAttributeValue(
                    edge.pipe_id, idx_desig_pipe, pipe_designation
                )

        self.pipe_layer.commitChanges()
        self.point_layer.commitChanges()

    def _get_conn_pipe_length(self, house_connection_fid: int) -> float:
        """Get the length of the connecting pipe for a given HC.

        Args:
            house_connection_fid: The feature ID of the house connection.

        Returns:
            The length of the connecting pipe, or 0.0 if not found.
        """
        hc_feat: QgsFeature = self.point_layer.getFeature(house_connection_fid)
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
        self,
        house_connection_point: QgsPointXY,
        adjacency_map: dict[Node, list[NetworkEdge]],
    ) -> Node | None:
        """Find the main pipe node connected to a house connection point.

        Args:
            house_connection_point: The geometry point of the house connection.
            adjacency_map: The adjacency list of the network.

        Returns:
            The connected Node if found, otherwise None.
        """
        key = Node(
            round(house_connection_point.x(), 4), round(house_connection_point.y(), 4)
        )
        if key in adjacency_map:
            return key

        pipes: list[QgsFeature] = self._get_pipes_at_point(house_connection_point)
        conn_type = PipeType.CONN.translated
        type_idx: int = self.pipe_layer.fields().lookupField(
            NewLineLayerFields.type.field_name
        )

        for pipe in pipes:
            if pipe.attribute(type_idx) == conn_type:
                endpoints: list[QgsPointXY] = VectorAnalysisTools.get_start_end_of_line(
                    pipe
                )
                for p in endpoints:
                    node_key = Node(round(p.x(), 4), round(p.y(), 4))
                    if node_key in adjacency_map:
                        return node_key
        return None

    def _update_conn_pipe_name(
        self, house_connection_fid: int, name: str, branch: str
    ) -> None:
        """Update the designation of the connection pipe(s) for a given HC.

        Args:
            house_connection_fid: The feature ID of the house connection.
            name: The new designation name.
            branch: The branch ID.
        """
        hc_feat: QgsFeature = self.point_layer.getFeature(house_connection_fid)
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

    def _get_pipes_at_point(self, point: QgsPointXY) -> list[QgsFeature]:
        """Find pipes intersecting a point within a small search radius.

        Args:
            point: The center point for the search.

        Returns:
            A list of pipe features that intersect the search buffer.
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
