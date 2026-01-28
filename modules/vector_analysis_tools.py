"""Module: vector_analysis_tools.py

This module contains the VectorAnalysisTools class.
"""

from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorDataProvider,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .constants import Names, NewLayerFields, Numbers
from .logs_and_errors import log_debug

if TYPE_CHECKING:
    from collections.abc import Iterable

    from qgis.core import QgsRectangle


class VectorAnalysisTools:
    """A base class for finding features in a vector layer."""

    def __init__(
        self,
        selected_layer: QgsVectorLayer,
        temp_point_layer: QgsVectorLayer,
    ) -> None:
        """Initialize the VectorAnalysisTools class.

        Args:
            selected_layer: The QgsVectorLayer to search within.
            temp_point_layer: The temporary QgsVectorLayer to add new features to.
        """
        self.selected_layer: QgsVectorLayer = selected_layer
        self.new_layer: QgsVectorLayer = temp_point_layer
        log_debug(f"Creating spatial index for layer '{selected_layer.name()}'.")
        request: QgsFeatureRequest = QgsFeatureRequest().setNoAttributes()
        self.selected_layer_index: QgsSpatialIndex = QgsSpatialIndex(
            selected_layer.getFeatures(request)
        )
        log_debug("Spatial index created.", Qgis.Success)
        self.dim_field_name: str | None = self.find_dim_field_name(selected_layer)

    @staticmethod
    def find_dim_field_name(layer: QgsVectorLayer) -> str | None:
        """Find the first matching dimension field name from the constants.

        Args:
            layer: The layer to search for the dimension field.

        Returns:
            The name of the found field, or None if no match is found.
        """
        layer_fields: QgsFields = layer.fields()
        field_names: Iterable[str] = Names.sel_layer_field_dim
        found_name: str | None = next(
            (name for name in field_names if layer_fields.lookupField(name) != -1),
            None,
        )

        if found_name:
            log_debug(f"Found dimension field: '{found_name}'", Qgis.Success)
        else:
            log_debug("No dimension field found in the selected layer.", Qgis.Warning)
        return found_name

    def create_feature(self, geometry: QgsGeometry, attributes: dict) -> bool:
        """Create a new feature in the new layer.

        Args:
            geometry: The geometry for the new feature.
            attributes: A dictionary of attribute values.

        Returns:
            True if the feature was added successfully, False otherwise.
        """
        data_provider: QgsVectorDataProvider | None = self.new_layer.dataProvider()
        if data_provider is None:
            log_debug("Data provider is None for new_layer.", Qgis.Critical)
            return False

        layer_fields: QgsFields = self.new_layer.fields()
        field_names: list[str] = [
            layer_fields[idx].name() for idx in range(layer_fields.count())
        ]

        attr_values: list = [
            attributes.get(field_name)
            for field_name in field_names
            if field_name.lower() != "fid"
        ]
        new_feature = QgsFeature(layer_fields)
        new_feature.setGeometry(geometry)
        new_feature.setAttributes(attr_values)

        return self.new_layer.addFeature(new_feature)

    def get_connected_attributes(self, connected_features: list[QgsFeature]) -> dict:
        """Get attributes from connected features.

        Args:
            connected_features: A list of features connected to the point.

        Returns:
            A dictionary of attributes derived from the connected features.
        """
        connected_ids: list[int] = sorted(
            {feature.attribute("original_fid") for feature in connected_features}
        )
        attributes: dict = {
            NewLayerFields.connected.name: Names.line_separator.join(
                str(id_int) or "???" for id_int in connected_ids
            )
        }
        # Get dimension values if the dimension field was found
        if self.dim_field_name:
            dims: list[int] = sorted(
                {
                    int(feat[self.dim_field_name])
                    for feat in connected_features
                    if feat.attribute(self.dim_field_name) is not None
                },
                reverse=True,
            )
            attributes[NewLayerFields.dim_1.name] = dims[0]
            if len(dims) > 1:
                attributes[NewLayerFields.dim_2.name] = dims[-1]

        return attributes

    def get_intersecting_features(self, search_geom: QgsGeometry) -> list[QgsFeature]:
        """Get intersecting features for a given geometry.

        Args:
            search_geom: The geometry to search for intersections with.

        Returns:
            A list of features that intersect with the search geometry.
        """
        search_rect: QgsRectangle = search_geom.boundingBox()
        candidate_ids: list[int] = self.selected_layer_index.intersects(search_rect)
        return [
            self.selected_layer.getFeature(feat_id)
            for feat_id in candidate_ids
            if self.selected_layer.getFeature(feat_id)
            .geometry()
            .intersects(search_geom)
        ]

    @staticmethod
    def get_start_end_of_line(feature: QgsFeature) -> list[QgsPointXY]:
        """Get the start and end points of a line feature.

        Args:
            feature: The line feature.

        Returns:
            A list containing the start and end points (QgsPointXY).
        """
        points: list = []
        geom: QgsGeometry = feature.geometry()
        if not geom:
            return points

        wkb_type: Qgis.WkbType = geom.wkbType()
        lines: list = []
        if wkb_type == QgsWkbTypes.LineString:
            lines.append(geom.asPolyline())
        elif wkb_type == QgsWkbTypes.MultiLineString:
            lines.extend(geom.asMultiPolyline())

        for line in lines:
            if len(line) > 1:
                points.extend([line[0], line[-1]])
        return points

    @staticmethod
    def calculate_angle(p1: QgsPointXY, p2: QgsPointXY, p3: QgsPointXY) -> float:
        """Calculate the deflection angle between two connected line segments.

        This function calculates the angle of deflection at point `p2`, which
        connects segments `p1-p2` and `p2-p3`.

        - A straight line (p1-p2-p3 are collinear) will return 0°.
        - A 90-degree turn will return 90°.
        - A U-turn (p1 and p3 are at the same location) will return 180°.

        Args:
            p1: The start point of the first segment.
            p2: The common point (vertex) where the segments meet.
            p3: The end point of the second segment.

        Returns:
            The deflection angle in degrees, from 0 to 180.
        """
        # Check for coincident points which would make angle calculation invalid.
        if p2.compare(p1, Numbers.tiny_number) or p2.compare(p3, Numbers.tiny_number):
            log_debug("Coincident points found for angle calculation.", Qgis.Warning)
            # Coincident points mean the lines are on top of each other (a U-turn).
            return Numbers.circle_semi

        # Azimuth of the incoming segment (p1 -> p2)
        azimuth_in: float = p1.azimuth(p2)
        # Azimuth of the outgoing segment (p2 -> p3)
        azimuth_out: float = p2.azimuth(p3)

        # The angle between the two vectors
        angle_diff: float = abs(azimuth_in - azimuth_out)

        # The deflection angle is 180 degrees minus the smaller angle between the
        # two vectors.
        if angle_diff > Numbers.circle_semi:
            angle_diff = Numbers.circle_full - angle_diff

        return angle_diff

    def get_adjacent_vertices(
        self, point: QgsPointXY, feature: QgsFeature
    ) -> tuple[QgsPointXY | None, QgsPointXY | None]:
        """Get vertices adjacent to a point on a feature's geometry.

        Finds the vertex on the feature that matches the input point and returns
        the vertices immediately before and after it.

        Args:
            point: The vertex on the feature.
            feature: The feature containing the geometry.

        Returns:
            A tuple containing the previous and next vertices as QgsPointXY.
            Returns (None, None) if the point is an endpoint or not a vertex.
        """
        geom: QgsGeometry = feature.geometry()
        if not geom:
            return None, None

        # Find the closest vertex on the geometry to the given point
        vertex: tuple[QgsPointXY, int, int, int, float] = geom.closestVertex(point)
        closest_v: QgsPointXY = vertex[0]
        vertex_idx: int = vertex[1]
        dist_sq: float = vertex[4]

        # Ensure the point is actually a vertex (not an intermediate point on a segment)
        if dist_sq > Numbers.tiny_number**2:
            return None, None

        # Check if the vertex is an endpoint of its line part
        if self.is_endpoint(closest_v, feature):
            return None, None

        # Get the vertices before and after the found vertex
        p_before = QgsPointXY(geom.vertexAt(vertex_idx - 1))
        p_after = QgsPointXY(geom.vertexAt(vertex_idx + 1))

        return p_before, p_after

    def get_adjacent_points_on_segment(
        self, point: QgsPointXY, feature: QgsFeature
    ) -> tuple[QgsPointXY | None, QgsPointXY | None]:
        """Get vertices of the segment a point lies on.

        Finds the segment of the feature's geometry that is closest to the
        input point and returns the start and end vertices of that segment.

        Args:
            point: The point on or near the feature.
            feature: The feature containing the geometry.

        Returns:
            A tuple containing the start and end vertices of the segment.
            Returns (None, None) if the point is not on a segment or on error.
        """
        geom: QgsGeometry = feature.geometry()
        if not geom:
            return None, None

        # Find the segment of the line closest to the point
        segment: tuple[float, QgsPointXY, int, int] = geom.closestSegmentWithContext(
            point
        )
        dist_sq: float = segment[0]
        after_vertex_idx: int = segment[2]

        # Ensure the point is actually on the line
        if dist_sq < 0 or dist_sq > Numbers.tiny_number**2:
            log_debug(
                f"Point {point.asWkt()} is not on the line geometry of "
                f"feature {feature.attribute('original_fid')}.",
                Qgis.Warning,
            )
            return None, None

        try:
            # after_vertex_idx is the index of the vertex at the END of the segment
            # Get the vertices that define the segment
            p_before = QgsPointXY(geom.vertexAt(after_vertex_idx - 1))
            p_after = QgsPointXY(geom.vertexAt(after_vertex_idx))
        except ValueError:
            log_debug(
                f"Invalid vertex index for feature "
                f"'{feature.attribute('original_fid')}'.",
                Qgis.Warning,
            )
            return None, None

        return p_before, p_after

    def is_endpoint(self, point: QgsPointXY, feature: QgsFeature) -> bool:
        """Check if a point is an endpoint of a feature's line geometry.

        Args:
            point: The point to check.
            feature: The feature to check against.

        Returns:
            True if the point is a start or end point of the feature's geometry,
            False otherwise.
        """
        if start_end_points := self.get_start_end_of_line(feature):
            return any(
                point.compare(endpoint, Numbers.tiny_number)
                for endpoint in start_end_points
            )
        return False

    def get_other_endpoint(
        self, feature: QgsFeature, reference_point: QgsPointXY
    ) -> QgsPointXY | None:
        """Get the endpoint of a line feature that is furthest from a reference point.

        This is useful for finding the "remote" end of a line when you have an
        intersection point near one of its ends.

        Args:
            feature: The line feature.
            reference_point: The point to measure distance from.

        Returns:
            The QgsPointXY of the other endpoint, or None if endpoints cannot be
            determined.
        """
        endpoints: list[QgsPointXY] = self.get_start_end_of_line(feature)
        if len(endpoints) != 2:  # noqa: PLR2004
            return None

        p1: QgsPointXY = endpoints[0]
        p2: QgsPointXY = endpoints[1]
        return p2 if p1.distance(reference_point) < p2.distance(reference_point) else p1

    def get_point_along_line(
        self, start_point: QgsPointXY, feature: QgsFeature, distance: float
    ) -> QgsPointXY | None:
        """Get a point at a specific distance from the start point along a feature.

        Args:
            start_point: The starting point on the line.
            feature: The line feature.
            distance: The distance to move along the line.

        Returns:
            The calculated point, or None if the other endpoint cannot be found.
        """
        other_endpoint: QgsPointXY | None = self.get_other_endpoint(
            feature, start_point
        )
        if not other_endpoint:
            return None

        azimuth: float = start_point.azimuth(other_endpoint)
        return start_point.project(distance, azimuth)
