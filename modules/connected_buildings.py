"""Module: connected_buildings.py

This module contains the logic to propagate connected building information
through the pipe network.
"""

from typing import TYPE_CHECKING

from qgis.core import QgsFeatureRequest, QgsVectorLayer

from .constants import (
    FittingType,
    Names,
    NewLineLayerFields,
    NewPointLayerFields,
    Numbers,
)
from .graph_definitions import NetworkEdge, NetworkGraph, Node
from .logs_and_errors import log_debug
from .vector_analysis_tools import VectorAnalysisTools

if TYPE_CHECKING:
    from qgis.core import QgsPointXY


class ConnectedBuildingsPropagator:
    """Propagates connected building information through the network."""

    def __init__(self, pipe_layer: QgsVectorLayer, point_layer: QgsVectorLayer) -> None:
        """Initialize the propagator.

        Args:
            pipe_layer: The vector layer containing the pipe network.
            point_layer: The vector layer containing the points (house connections).
        """
        self.pipe_layer: QgsVectorLayer = pipe_layer
        self.point_layer: QgsVectorLayer = point_layer

    def run(self) -> None:
        """Identify and list connected buildings for each pipe segment.

        This method builds a graph of the entire pipe network and uses a "leaf
        peeling" algorithm. It starts from the leaves of the graph (typically
        house connections) and propagates their designation strings up through
        the network branches, accumulating them on each pipe segment.
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

    def _build_network_graph(self) -> NetworkGraph:
        """Build the graph representation of the pipe network.

        Only linear features with exactly two distinct endpoints are included
        in the graph.

        Returns:
            A NetworkGraph object containing the adjacency list and node degrees.
        """
        graph = NetworkGraph()

        for feature in self.pipe_layer.getFeatures():
            endpoints: list[QgsPointXY] = VectorAnalysisTools.get_start_end_of_line(
                feature
            )
            # We strictly need 2 endpoints for a valid pipe segment in this graph
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

        This method implements a "leaf peeling" algorithm. It iteratively removes
        leaf nodes (nodes with degree 1) and propagates the associated house
        connection information to their neighbors.

        Note:
            This method modifies the `graph` in-place by removing edges and
            updates `node_hcs` with accumulated designations.

        Args:
            graph: The NetworkGraph object representing the pipe network.
            node_hcs: The mapping of HC designations to their graph nodes.

        Returns:
            A dictionary mapping each pipe feature ID to a set of all HC
            designations it serves.
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

        This method handles the editing session (start/commit) for the layer.

        Args:
            pipe_hcs: A dictionary mapping pipe feature IDs to sets of
                connected house connection strings.
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
