"""Module: line_merger.py

This module contains the LineMerger class for merging line features.
"""

from typing import NamedTuple

from qgis.core import (
    Qgis,
    QgsFeature,
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
from .graph_definitions import Node
from .logs_and_errors import log_debug
from .vector_analysis_tools import FieldNames


class EdgeID(NamedTuple):
    """Unique identifier for a graph edge.

    Attributes:
        fid: The feature ID of the original line feature.
        part_idx: The index of the geometry part (for multi-geometries).
        seg_idx: The index of the segment within the part.
    """

    fid: int
    part_idx: int
    seg_idx: int


class GraphEdge(NamedTuple):
    """Represents a graph edge with its identifier and data.

    Attributes:
        id: The unique identifier for the edge.
        points: The list of QgsPointXY defining the edge geometry.
        start_node: The starting Node of the edge.
        end_node: The ending Node of the edge.
    """

    id: EdgeID
    points: list[QgsPointXY]
    start_node: Node
    end_node: Node


class GraphContext(NamedTuple):
    """Holds graph context for line merging.

    Attributes:
        edges: A dictionary mapping EdgeIDs to GraphEdges.
        node_to_edges: A dictionary mapping Nodes to a list of connected GraphEdges.
        split_points: A set of Nodes representing split points (e.g., T-pieces).
    """

    edges: dict[EdgeID, GraphEdge]
    node_to_edges: dict[Node, list[GraphEdge]]
    split_points: set[Node]


class SplitPointData(NamedTuple):
    """Holds data related to split points.

    Attributes:
        coords: A set of Node objects representing the coordinates of split points.
        geoms: A dictionary mapping feature IDs to their QgsPointXY geometries.
        spatial_index: A QgsSpatialIndex containing the split point features.
    """

    coords: set[Node]
    geoms: dict[int, QgsPointXY]
    spatial_index: QgsSpatialIndex


class GraphStructure(NamedTuple):
    """Holds the structural components of the graph.

    Attributes:
        edges: A dictionary of all edges in the graph.
        features_map: A dictionary mapping feature IDs to original QgsFeatures.
        node_to_edges: A dictionary mapping nodes to connected edges.
    """

    edges: dict[EdgeID, GraphEdge]
    features_map: dict[int, QgsFeature]
    node_to_edges: dict[Node, list[GraphEdge]]


class PathResult(NamedTuple):
    """Result of a path growing operation.

    Attributes:
        edges: A list of GraphEdges forming the path.
        end_node: The Node where the path ends.
    """

    edges: list[GraphEdge]
    end_node: Node


class MergedPath(NamedTuple):
    """Represents a merged path of connected edges.

    Attributes:
        edge_ids: A list of GraphEdges that make up the merged path.
    """

    edge_ids: list[GraphEdge]


class LineMerger:
    """A class to merge connected line features."""

    def __init__(self, point_layer: QgsVectorLayer | None) -> None:
        """Initialize the LineMerger.

        Args:
            point_layer: The point layer containing split points (T-pieces, etc.)
                used to break lines. Can be None.
        """
        self.point_layer: QgsVectorLayer | None = point_layer

    def create_merged_line_features(
        self,
        source_layer: QgsVectorLayer,
        target_fields: QgsFields,
        source_fields: FieldNames,
    ) -> list[QgsFeature]:
        """Create merged line features from the source layer.

        Merges connected lines with the same attributes between split nodes.
        It first builds a graph where lines are split at 'split points' (like T-pieces),
        then traces paths of connected segments that don't branch, and merges them
        into single features.

        Args:
            source_layer: The source line layer to process.
            target_fields: The fields definition for the new merged features.
            source_fields: A NamedTuple containing the names of the source fields
                (dimension, load) to preserve/check during merging.

        Returns:
            A list of merged QgsFeature objects.
        """
        # 1. Collect points that will act as network splits (e.g., T-pieces)
        split_data: SplitPointData = self._collect_split_points()

        # 2. Build a graph representation of the line network, splitting lines
        #    at the collected split points.
        graph_struct: GraphStructure = self._build_graph(
            source_layer,
            split_data.coords,
            split_data.geoms,
            split_data.spatial_index,
        )

        # 3. Traverse the graph to find paths of connected edges that can be merged.
        context = GraphContext(
            graph_struct.edges, graph_struct.node_to_edges, split_data.coords
        )
        merged_paths: list[MergedPath] = self._find_merged_paths(context)

        # 4. Construct a new feature for each merged path.
        merged_features: list[QgsFeature] = [
            self._construct_merged_feature(
                path,
                graph_struct.features_map,
                target_fields,
                source_fields,
            )
            for path in merged_paths
        ]

        log_debug(
            f"Merged {len(graph_struct.edges)} segments into "
            f"{len(merged_features)} features.",
            Qgis.Success,
        )
        return merged_features

    def _collect_split_points(
        self,
    ) -> SplitPointData:
        """Collect points that should split lines from the point layer.

        Returns:
            A SplitPointData object containing split points, geometries, and the
            spatial index.
        """
        split_points: set[Node] = set()
        split_point_geoms: dict[int, QgsPointXY] = {}
        sp_index = QgsSpatialIndex()

        if not self.point_layer:
            return SplitPointData(split_points, split_point_geoms, sp_index)

        type_idx: int = self.point_layer.fields().lookupField(
            NewPointLayerFields.type.field_name
        )
        split_types: set[str] = {
            FittingType.T_PIECE.translated,
            FittingType.HOUSE_CONN.translated,
        }
        for feature in self.point_layer.getFeatures():
            if (feature.attribute(type_idx) in split_types) and (
                geom := feature.geometry()
            ):
                point = geom.asPoint()
                split_points.add(Node(round(point.x(), 4), round(point.y(), 4)))
                split_point_geoms[feature.id()] = point
                sp_index.addFeature(feature)

        return SplitPointData(split_points, split_point_geoms, sp_index)

    def _build_graph(
        self,
        source_layer: QgsVectorLayer,
        split_points: set[Node],
        split_point_geoms: dict[int, QgsPointXY],
        sp_index: QgsSpatialIndex,
    ) -> GraphStructure:
        """Build a graph representation of the line network.

        The graph is composed of edges, which are segments of the original lines
        split at specified split points.

        Args:
            source_layer: The source line layer to process.
            split_points: A set of coordinates for points that break lines.
            split_point_geoms: A map of point feature IDs to split point geometries.
            sp_index: A spatial index containing the split points.

        Returns:
            A GraphStructure object containing the edge dictionary, the feature map,
            and the node-to-edge mapping.
        """
        edges: dict[EdgeID, GraphEdge] = {}
        features_map: dict[int, QgsFeature] = {
            feature.id(): feature for feature in source_layer.getFeatures()
        }

        for feature in features_map.values():
            geom: QgsGeometry = feature.geometry()
            if not geom:
                continue

            parts = (
                geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
            )

            for idx, part_points in enumerate(parts):
                if len(part_points) < 2:  # noqa: PLR2004
                    continue

                # Inject points that lie on segments as new vertices
                modified_points: list[QgsPointXY] = self._inject_split_points_into_part(
                    part_points, sp_index, split_point_geoms
                )

                # Split the part into segments at the split points
                segments: list[list[QgsPointXY]] = self._split_part_at_points(
                    modified_points, split_points
                )

                for segment_index, segment in enumerate(segments):
                    if len(segment) < 2:  # noqa: PLR2004
                        continue

                    start_node = Node(
                        x=round(segment[0].x(), 4), y=round(segment[0].y(), 4)
                    )
                    end_node = Node(
                        x=round(segment[-1].x(), 4), y=round(segment[-1].y(), 4)
                    )
                    edge_id = EdgeID(feature.id(), idx, segment_index)
                    edges[edge_id] = GraphEdge(edge_id, segment, start_node, end_node)

        node_to_edges: dict[Node, list[GraphEdge]] = {}
        for edge in edges.values():
            node_to_edges.setdefault(edge.start_node, []).append(edge)
            node_to_edges.setdefault(edge.end_node, []).append(edge)

        return GraphStructure(edges, features_map, node_to_edges)

    def _inject_split_points_into_part(
        self,
        part_points: list[QgsPointXY],
        sp_index: QgsSpatialIndex,
        split_point_geoms: dict[int, QgsPointXY],
    ) -> list[QgsPointXY]:
        """Inject split points that lie on a line part as new vertices.

        Args:
            part_points: The original list of points defining the line geometry part.
            sp_index: The spatial index of split points.
            split_point_geoms: A map of point feature IDs to split point geometries.

        Returns:
            A new list of points for the part, with injected vertices.
        """
        part_geom: QgsGeometry = QgsGeometry.fromPolylineXY(part_points)
        candidate_ids: list[int] = sp_index.intersects(part_geom.boundingBox())
        points_on_part: list[QgsPointXY] = [
            split_point
            for candidate_id in candidate_ids
            if (split_point := split_point_geoms[candidate_id])
            and part_geom.distance(QgsGeometry.fromPointXY(split_point))
            < Numbers.tiny_number
        ]

        if not points_on_part:
            return part_points

        new_part_points: list[QgsPointXY] = [part_points[0]]
        for i in range(len(part_points) - 1):
            point_start: QgsPointXY = part_points[i]
            point_end: QgsPointXY = part_points[i + 1]

            seg_geom: QgsGeometry = QgsGeometry.fromPolylineXY([point_start, point_end])

            if segment_points := [
                split_point
                for split_point in points_on_part
                if seg_geom.distance(QgsGeometry.fromPointXY(split_point))
                < Numbers.tiny_number
                and not split_point.compare(point_start, Numbers.tiny_number)
                and not split_point.compare(point_end, Numbers.tiny_number)
            ]:
                segment_points.sort(key=point_start.sqrDist)
                new_part_points.extend(segment_points)

            new_part_points.append(point_end)
        return new_part_points

    def _split_part_at_points(
        self, part_points: list[QgsPointXY], split_points: set[Node]
    ) -> list[list[QgsPointXY]]:
        """Split a line part into segments at the given split points.

        Args:
            part_points: The list of points defining the line geometry part.
            split_points: A set of Node objects representing the split coordinates.

        Returns:
            A list of segments, where each segment is a list of QgsPointXY.
        """
        segments: list = []
        current_segment: list[QgsPointXY] = [part_points[0]]

        for i in range(1, len(part_points)):
            point: QgsPointXY = part_points[i]
            point_node = Node(round(point.x(), 4), round(point.y(), 4))
            current_segment.append(point)

            # Split if the point is a split point, but not if it's the last point
            if point_node in split_points and i < len(part_points) - 1:
                segments.append(current_segment)
                current_segment = [point]

        segments.append(current_segment)
        return segments

    def _find_merged_paths(
        self,
        context: GraphContext,
    ) -> list[MergedPath]:
        """Traverse the graph to find and merge connected edges.

        Args:
            context: The GraphContext containing edges, nodes, and split points.

        Returns:
            A list of MergedPath objects, where each path contains a list of edge IDs.
        """
        merged_paths: list[MergedPath] = []
        visited_edges: set[EdgeID] = set()

        for edge in context.edges.values():
            if edge.id in visited_edges:
                continue

            visited_edges.add(edge.id)
            # Grow forward from the end node
            forward_res: PathResult = self._grow_path(
                edge.end_node, edge, visited_edges, context
            )

            # Grow backward from the start node
            backward_res: PathResult = self._grow_path(
                edge.start_node, edge, visited_edges, context
            )

            # Combine the paths: backward (reversed) + initial edge + forward
            full_path: list[GraphEdge] = [
                *reversed(backward_res.edges),
                edge,
                *forward_res.edges,
            ]
            merged_paths.append(MergedPath(full_path))

        return merged_paths

    def _grow_path(
        self,
        start_node: Node,
        start_edge: GraphEdge,
        visited_edges: set[EdgeID],
        context: GraphContext,
    ) -> PathResult:
        """Extend a path of edges from a starting node.

        Args:
            start_node: The Node from which to start growing the path.
            start_edge: The initial GraphEdge in the path.
            visited_edges: A set of already visited edge IDs.
            context: The GraphContext containing edges, nodes, and split points.

        Returns:
            A PathResult containing the list of path edges and the final Node.
        """
        path: list[GraphEdge] = []
        current_node: Node = start_node
        last_edge_id: EdgeID = start_edge.id

        while current_node not in context.split_points:
            candidates: list[GraphEdge] = context.node_to_edges.get(current_node, [])
            valid_next: list[GraphEdge] = [
                candidate
                for candidate in candidates
                if candidate.id != last_edge_id and candidate.id not in visited_edges
            ]

            if len(valid_next) == 1:
                next_edge: GraphEdge = valid_next[0]
                path.append(next_edge)
                visited_edges.add(next_edge.id)
                # Move to the other end of the new edge
                current_node = (
                    next_edge.end_node
                    if next_edge.start_node == current_node
                    else next_edge.start_node
                )
                last_edge_id = next_edge.id
            else:
                break
        return PathResult(path, current_node)

    def _merge_geometries(self, path_edges: list[GraphEdge]) -> QgsGeometry:
        """Merge geometries of connected edges into a single polyline.

        Args:
            path_edges: An ordered list of graph edges to merge.

        Returns:
            The merged geometry.
        """
        first_edge: GraphEdge = path_edges[0]
        points: list[QgsPointXY] = first_edge.points

        if len(path_edges) > 1:
            second_edge: GraphEdge = path_edges[1]
            end_node: Node = first_edge.end_node
            # Check connectivity to determine orientation of first segment
            if end_node in [second_edge.start_node, second_edge.end_node]:
                full_points: list[QgsPointXY] = list(points)
                last_node: Node = end_node
            else:
                full_points = list(reversed(points))
                last_node: Node = first_edge.start_node
        else:
            full_points = list(points)
            last_node = first_edge.end_node

        for i in range(1, len(path_edges)):
            next_edge: GraphEdge = path_edges[i]
            next_points: list[QgsPointXY] = next_edge.points

            if next_edge.start_node == last_node:
                full_points.extend(next_points[1:])
                last_node = next_edge.end_node
            elif next_edge.end_node == last_node:
                full_points.extend(next_points[-2::-1])
                last_node = next_edge.start_node

        return QgsGeometry.fromPolylineXY(full_points)

    def _resolve_attributes_and_notes(
        self,
        path_edges: list[GraphEdge],
        features_map: dict[int, QgsFeature],
        source_fields: FieldNames,
    ) -> tuple[dict[str, int | float], str]:
        """Resolve attributes for the merged feature and generate notes.

        Args:
            path_edges: A list of edges in the merged path.
            features_map: A dictionary mapping original feature IDs to features.
            source_fields: The names of the source fields to check.

        Returns:
            A tuple containing a dictionary of attributes to set (dimension, load)
            and a semicolon-separated string of notes.
        """
        dims: set = set()
        loads: set = set()

        for edge in path_edges:
            feature: QgsFeature = features_map[edge.id.fid]
            if (
                source_fields.dim
                and (value := feature.attribute(source_fields.dim)) is not None
            ):
                dims.add(value)
            if (
                source_fields.load
                and (value := feature.attribute(source_fields.load)) is not None
            ):
                loads.add(value)

        attributes: dict[str, int | float] = {}
        notes: list[str] = []

        if len(dims) == 1:
            attributes[NewLineLayerFields.dim.field_name] = next(iter(dims))
        elif len(dims) > 1:
            sorted_dims: list = sorted(dims)
            notes.append(f"Dimensions: {', '.join(map(str, sorted_dims))}")

        if len(loads) == 1:
            attributes[NewLineLayerFields.load.field_name] = next(iter(loads))
        elif len(loads) > 1:
            sorted_loads: list = sorted(loads)
            notes.append(f"Loads: {', '.join(map(str, sorted_loads))}")

        return attributes, "; ".join(notes)

    def _construct_merged_feature(
        self,
        merged_path: MergedPath,
        features_map: dict[int, QgsFeature],
        target_fields: QgsFields,
        source_fields: FieldNames,
    ) -> QgsFeature:
        """Construct a single merged QgsFeature from a path of edges.

        Args:
            merged_path: The MergedPath object containing an ordered list of edge IDs.
            features_map: A map of original feature IDs to QgsFeature objects.
            target_fields: The fields definition for the new feature.
            source_fields: The names of the source fields to extract data from.

        Returns:
            The newly constructed and attributed QgsFeature.
        """
        path_edges: list[GraphEdge] = merged_path.edge_ids

        # 1. Create feature and set geometry
        new_feature = QgsFeature(target_fields)
        new_feature.setGeometry(self._merge_geometries(path_edges))

        # 2. Set attributes from the first original feature in the path
        first_fid: int = path_edges[0].id.fid
        source_feature: QgsFeature = features_map[first_fid]

        new_feature.setAttribute(
            NewLineLayerFields.org_id.field_name,
            source_feature.attribute("original_fid"),
        )

        # 3. Resolve unified attributes (dim, load) and notes
        attributes_and_notes: tuple[dict[str, int | float], str] = (
            self._resolve_attributes_and_notes(path_edges, features_map, source_fields)
        )
        resolved_attributes: dict[str, int | float] = attributes_and_notes[0]
        notes: str = attributes_and_notes[1]

        for name, value in resolved_attributes.items():
            new_feature.setAttribute(name, value)

        if notes:
            new_feature.setAttribute(NewLineLayerFields.notes.field_name, notes)

        # 4. Set calculated attributes
        new_feature.setAttribute(
            NewLineLayerFields.length.field_name, new_feature.geometry().length()
        )
        new_feature.setAttribute(
            NewLineLayerFields.type.field_name, PipeType.MAIN.translated
        )

        return new_feature
