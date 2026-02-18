"""Module: line_merger.py

This module contains the LineMerger class for merging line features.
"""

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
from .logs_and_errors import log_debug


class LineMerger:
    """A class to merge connected line features."""

    def __init__(self, point_layer: QgsVectorLayer | None) -> None:
        """Initialize the LineMerger.

        Args:
            point_layer: The point layer containing split points (T-pieces, etc.).
        """
        self.point_layer = point_layer

    def create_merged_line_features(
        self,
        source_layer: QgsVectorLayer,
        target_fields: QgsFields,
        dim_field: str | None,
        load_field: str | None,
    ) -> list[QgsFeature]:
        """Create merged line features from the source layer.

        Merges connected lines with same attributes between split nodes.

        Args:
            source_layer: The source line layer.
            target_fields: The fields for the new features.
            dim_field: The name of the dimension field in the source layer.
            load_field: The name of the load field in the source layer.

        Returns:
            A list of merged QgsFeature objects.
        """
        # 1. Collect points that will act as network splits (e.g., T-pieces)
        split_points, split_point_geoms, sp_index = self._collect_split_points()

        # 2. Build a graph representation of the line network, splitting lines
        #    at the collected split points.
        edges, features_map, node_to_edges = self._build_graph(
            source_layer,
            dim_field,
            load_field,
            split_points,
            split_point_geoms,
            sp_index,
        )

        # 3. Traverse the graph to find paths of connected edges that can be merged.
        merged_paths = self._find_merged_paths(edges, node_to_edges, split_points)

        # 4. Construct a new feature for each merged path.
        merged_features: list[QgsFeature] = [
            self._construct_merged_feature(
                path, edges, features_map, target_fields, dim_field, load_field
            )
            for path in merged_paths
        ]

        log_debug(
            f"Merged {len(edges)} segments into {len(merged_features)} features.",
            Qgis.Success,
        )
        return merged_features

    def _collect_split_points(
        self,
    ) -> tuple[set[tuple[float, float]], dict[int, QgsPointXY], QgsSpatialIndex]:
        """Collect points that should split lines from the point layer.

        Returns:
            A tuple containing:
            - A set of rounded (x, y) coordinates for fast lookups.
            - A dictionary mapping feature ID to QgsPointXY geometry.
            - A spatial index of the split point features.
        """
        split_points: set[tuple[float, float]] = set()
        split_point_geoms: dict[int, QgsPointXY] = {}
        sp_index = QgsSpatialIndex()

        if not self.point_layer:
            return split_points, split_point_geoms, sp_index

        type_idx = self.point_layer.fields().lookupField(
            NewPointLayerFields.type.field_name
        )
        split_types = {
            FittingType.T_PIECE.translated,
            FittingType.HOUSE_CONN.translated,
        }
        for feat in self.point_layer.getFeatures():
            if feat.attribute(type_idx) in split_types:
                if geom := feat.geometry():
                    p = geom.asPoint()
                    split_points.add((round(p.x(), 4), round(p.y(), 4)))
                    split_point_geoms[feat.id()] = p
                    sp_index.addFeature(feat)

        return split_points, split_point_geoms, sp_index

    def _build_graph(
        self,
        source_layer: QgsVectorLayer,
        dim_field: str | None,
        load_field: str | None,
        split_points: set[tuple[float, float]],
        split_point_geoms: dict[int, QgsPointXY],
        sp_index: QgsSpatialIndex,
    ) -> tuple[dict, dict, dict]:
        """Build a graph representation of the line network.

        The graph is composed of edges, which are segments of the original lines
        split at specified split points.

        Args:
            source_layer: The source line layer.
            dim_field: The name of the dimension field.
            load_field: The name of the load field.
            split_points: A set of coordinates for points that break lines.
            split_point_geoms: A map of feature ID to split point geometry.
            sp_index: A spatial index of split points.

        Returns:
            A tuple containing:
            - edges: A dictionary mapping edge ID to edge data.
            - features_map: A dictionary mapping original feature ID to feature.
            - node_to_edges: A dictionary mapping node coordinates to edge IDs.
        """
        edges: dict[tuple[int, int, int], dict] = {}
        features_map: dict[int, QgsFeature] = {
            feat.id(): feat for feat in source_layer.getFeatures()
        }

        for feat in features_map.values():
            geom = feat.geometry()
            if not geom:
                continue

            parts = (
                geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
            )

            attrs = (
                feat.attribute(dim_field) if dim_field else None,
                feat.attribute(load_field) if load_field else None,
            )

            for idx, part in enumerate(parts):
                if len(part) < 2:
                    continue

                # Inject points that lie on segments as new vertices
                modified_part = self._inject_split_points_into_part(
                    part, sp_index, split_point_geoms
                )

                # Split the part into segments at the split points
                segments = self._split_part_at_points(modified_part, split_points)

                for seg_i, segment in enumerate(segments):
                    if len(segment) < 2:
                        continue

                    u_node = (round(segment[0].x(), 4), round(segment[0].y(), 4))
                    v_node = (round(segment[-1].x(), 4), round(segment[-1].y(), 4))
                    edge_id = (feat.id(), idx, seg_i)
                    edges[edge_id] = {
                        "points": segment,
                        "attrs": attrs,
                        "u": u_node,
                        "v": v_node,
                        "fid": feat.id(),
                    }

        node_to_edges: dict[tuple, list[tuple[int, int, int]]] = {}
        for eid, data in edges.items():
            node_to_edges.setdefault(data["u"], []).append(eid)
            node_to_edges.setdefault(data["v"], []).append(eid)

        return edges, features_map, node_to_edges

    def _inject_split_points_into_part(
        self,
        part: list[QgsPointXY],
        sp_index: QgsSpatialIndex,
        split_point_geoms: dict[int, QgsPointXY],
    ) -> list[QgsPointXY]:
        """Inject split points that lie on a line part as new vertices.

        Args:
            part: The original list of points defining the line part.
            sp_index: The spatial index of split points.
            split_point_geoms: A map of feature ID to split point geometry.

        Returns:
            A new list of points for the part, with injected vertices.
        """
        part_geom = QgsGeometry.fromPolylineXY(part)
        candidate_ids = sp_index.intersects(part_geom.boundingBox())
        points_on_part = []

        for cid in candidate_ids:
            pt = split_point_geoms[cid]
            if part_geom.distance(QgsGeometry.fromPointXY(pt)) < Numbers.tiny_number:
                points_on_part.append(pt)

        if not points_on_part:
            return part

        new_part = [part[0]]
        for i in range(len(part) - 1):
            p_start = part[i]
            p_end = part[i + 1]

            segment_points = []
            seg_geom = QgsGeometry.fromPolylineXY([p_start, p_end])

            segment_points.extend(
                pt
                for pt in points_on_part
                if seg_geom.distance(QgsGeometry.fromPointXY(pt)) < Numbers.tiny_number
                and (
                    not pt.compare(p_start, Numbers.tiny_number)
                    and not pt.compare(p_end, Numbers.tiny_number)
                )
            )
            if segment_points:
                segment_points.sort(key=p_start.sqrDist)
                new_part.extend(segment_points)

            new_part.append(p_end)
        return new_part

    def _split_part_at_points(
        self, part: list[QgsPointXY], split_points: set[tuple[float, float]]
    ) -> list[list[QgsPointXY]]:
        """Split a line part into segments at the given split points.

        Args:
            part: The list of points defining the line part.
            split_points: A set of (x, y) tuples for split points.

        Returns:
            A list of segments, where each segment is a list of points.
        """
        segments = []
        current_segment = [part[0]]

        for i in range(1, len(part)):
            p = part[i]
            p_key = (round(p.x(), 4), round(p.y(), 4))
            current_segment.append(p)

            # Split if the point is a split point, but not if it's the last point
            if p_key in split_points and i < len(part) - 1:
                segments.append(current_segment)
                current_segment = [p]

        segments.append(current_segment)
        return segments

    def _find_merged_paths(
        self,
        edges: dict,
        node_to_edges: dict,
        split_points: set[tuple[float, float]],
    ) -> list[list[tuple[int, int, int]]]:
        """Traverse the graph to find and merge connected edges.

        Args:
            edges: The graph's edge data.
            node_to_edges: The graph's node-to-edge mapping.
            split_points: A set of coordinates for points that break lines.

        Returns:
            A list of paths, where each path is a list of edge IDs to be merged.
        """
        merged_paths: list[list[tuple[int, int, int]]] = []
        visited_edges: set[tuple[int, int, int]] = set()

        for eid, e_data in edges.items():
            if eid in visited_edges:
                continue

            visited_edges.add(eid)
            # Grow forward from the 'v' node
            forward_path, _ = self._grow_path(
                e_data["v"], eid, visited_edges, edges, node_to_edges, split_points
            )

            # Grow backward from the 'u' node
            backward_path, _ = self._grow_path(
                e_data["u"], eid, visited_edges, edges, node_to_edges, split_points
            )

            # Combine the paths: backward (reversed) + initial edge + forward
            full_path = [*list(reversed(backward_path)), eid, *forward_path]
            merged_paths.append(full_path)

        return merged_paths

    def _grow_path(
        self,
        start_node: tuple[float, float],
        start_edge: tuple[int, int, int],
        visited_edges: set[tuple[int, int, int]],
        edges: dict,
        node_to_edges: dict,
        split_points: set[tuple[float, float]],
    ) -> tuple[list[tuple[int, int, int]], tuple[float, float]]:
        """Extend a path of edges from a starting node.

        Args:
            start_node: The node from which to start growing the path.
            start_edge: The initial edge in the path.
            visited_edges: A set of already visited edge IDs.
            edges: The graph's edge data.
            node_to_edges: The graph's node-to-edge mapping.
            split_points: A set of coordinates for points that break lines.

        Returns:
            A tuple containing:
            - A list of edge IDs forming the grown path.
            - The final node reached at the end of the path.
        """
        path = []
        curr_node = start_node
        current_attrs = edges[start_edge]["attrs"]
        last_edge = start_edge

        while True:
            if curr_node in split_points:
                break

            candidates = node_to_edges.get(curr_node, [])
            valid_next = [
                c
                for c in candidates
                if c != last_edge
                and c not in visited_edges
                and edges[c]["attrs"] == current_attrs
            ]

            if len(valid_next) == 1:
                next_eid = valid_next[0]
                path.append(next_eid)
                visited_edges.add(next_eid)
                e_data = edges[next_eid]
                # Move to the other end of the new edge
                curr_node = e_data["v"] if e_data["u"] == curr_node else e_data["u"]
                last_edge = next_eid
            else:
                break
        return path, curr_node

    def _construct_merged_feature(
        self,
        path_edges: list[tuple[int, int, int]],
        edges: dict,
        features_map: dict[int, QgsFeature],
        target_fields: QgsFields,
        dim_field: str | None,
        load_field: str | None,
    ) -> QgsFeature:
        """Construct a single merged QgsFeature from a path of edges.

        Args:
            path_edges: An ordered list of edge IDs to merge.
            edges: The graph's edge data.
            features_map: A map of original feature IDs to features.
            target_fields: The fields for the new feature.
            dim_field: The name of the dimension field.
            load_field: The name of the load field.

        Returns:
            The newly constructed and attributed QgsFeature.
        """
        # 1. Determine initial point order and starting node for geometry construction
        e0_data = edges[path_edges[0]]
        pts = e0_data["points"]

        if len(path_edges) > 1:
            e1_data = edges[path_edges[1]]
            # Assume the connection is at the 'v' end of the first edge
            p_end = e0_data["v"]
            if p_end == e1_data["u"] or p_end == e1_data["v"]:
                # Assumption was correct
                full_points = list(pts)
                last_node = p_end
            else:
                # Connection must be at the 'u' end, so reverse points
                full_points = list(reversed(pts))
                last_node = e0_data["u"]
        else:
            # For a single-edge path, order doesn't matter for the geometry
            full_points = list(pts)
            last_node = e0_data["v"]  # Placeholder, not used further

        # 2. Append points from subsequent edges
        for i in range(1, len(path_edges)):
            next_e_data = edges[path_edges[i]]
            next_pts = next_e_data["points"]

            if next_e_data["u"] == last_node:
                full_points.extend(next_pts[1:])
                last_node = next_e_data["v"]
            elif next_e_data["v"] == last_node:
                full_points.extend(list(reversed(next_pts))[1:])
                last_node = next_e_data["u"]

        # 3. Create feature and set geometry
        new_feat = QgsFeature(target_fields)
        new_feat.setGeometry(QgsGeometry.fromPolylineXY(full_points))

        # 4. Set attributes from the first original feature in the path
        first_fid = edges[path_edges[0]]["fid"]
        source_feat = features_map[first_fid]

        new_feat.setAttribute(
            NewLineLayerFields.org_id.field_name,
            source_feat.attribute("original_fid"),
        )
        if dim_field:
            new_feat.setAttribute(
                NewLineLayerFields.dim.field_name, source_feat.attribute(dim_field)
            )
        if load_field:
            new_feat.setAttribute(
                NewLineLayerFields.load.field_name,
                source_feat.attribute(load_field),
            )

        new_feat.setAttribute(
            NewLineLayerFields.length.field_name, new_feat.geometry().length()
        )
        new_feat.setAttribute(
            NewLineLayerFields.type.field_name, PipeType.MAIN.translated
        )

        return new_feat
