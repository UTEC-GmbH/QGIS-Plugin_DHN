"""Module: find_stuff.py

This module contains the FeatureFinder class that finds things in the selected layer.
"""

from collections.abc import Callable

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QProgressBar

from .constants import Numbers
from .feature_creator import FeatureCreator
from .logs_and_errors import log_debug, raise_runtime_error
from .point_collector import PointCollector
from .t_intersection_analyzer import TIntersectionAnalyzer


class PointOfInterestClassifier(FeatureCreator):
    """A class to find different types of features in a vector layer."""

    def __init__(
        self, selected_layer: QgsVectorLayer, temp_point_layer: QgsVectorLayer
    ) -> None:
        """Initialize the FeatureFinder class.

        Args:
            selected_layer: The QgsVectorLayer to search within.
            temp_point_layer: The QgsVectorLayer to add new features to.
        """
        super().__init__(selected_layer, temp_point_layer)
        self.t_piece_finder = TIntersectionAnalyzer(selected_layer, temp_point_layer)
        self._questionable_points_coords: set[tuple[float, float]] = set()

    def find_features(
        self,
        progress_bar: QProgressBar,
        pgb_update_text: Callable[[str], None],
    ) -> int:
        """Find and classify points of interest.

        Args:
            progress_bar: A QProgressBar to report progress.
            pgb_update_text: A function to update the progress bar's text.

        Returns:
            The total number of features created.
        """
        log_debug("Starting point-centric feature search.")

        if not self.new_layer.startEditing():
            raise_runtime_error("Failed to start editing the new layer.")

        # 1. Collect all points of interest
        # fmt: off
        pgb_update_text(QCoreApplication.translate("progress_bar", "Collecting points..."))  # noqa: E501
        # fmt: on
        collector = PointCollector(self.selected_layer, self.selected_layer_index)
        collected_points: dict[str, list[QgsPointXY]] = collector.collect_points(
            progress_bar
        )

        # 2. Directly classify all intersections without endpoints as "questionable"
        log_debug(
            f"Creating {len(collected_points['intersections'])} "
            "questionable points for intersections without endpoints."
        )
        # fmt: off
        note_text:str = QCoreApplication.translate("feature_note", "Intersection without endpoints - lines must be devided.")  # noqa: E501
        # fmt: on
        created_count: int = 0
        for point in collected_points["intersections"]:
            if self.create_questionable_point(point, note=note_text):
                created_count += 1
                point_key: tuple[float, float] = (
                    round(point.x(), 4),
                    round(point.y(), 4),
                )
                self._questionable_points_coords.add(point_key)

        # 3. Process the remaining vertices
        # fmt: off
        pgb_update_text(QCoreApplication.translate("progress_bar", "Analyzing points..."))  # noqa: E501
        # fmt: on
        progress_bar.setMaximum(len(collected_points["vertices"]))
        for i, point in enumerate(collected_points["vertices"]):
            created_count += self._process_point(point)
            progress_bar.setValue(i + 1)

        if not self.new_layer.commitChanges():
            raise_runtime_error("Failed to commit changes to the new layer.")

        log_debug("Feature search completed.", Qgis.Success)
        return created_count

    def _handle_two_intersections(
        self, point: QgsPointXY, intersecting_features: list[QgsFeature]
    ) -> int:
        """Process a point where two lines intersect.

        This method determines if the intersection is a bend, a pseudo T-piece,
        or a data error (crossing lines without a shared vertex).

        Args:
            point: The intersection point.
            intersecting_features: The two features that intersect.

        Returns:
            The number of features created.
        """
        feat1, feat2 = intersecting_features
        is_endpoint1: bool = self.is_endpoint(point, feat1)
        is_endpoint2: bool = self.is_endpoint(point, feat2)

        if is_endpoint1 and is_endpoint2:
            # Both features end here. Treat as a 2-way intersection (bend/reducer).
            return self._process_2_way_intersection(point, intersecting_features)

        if is_endpoint1 ^ is_endpoint2:
            # Exactly one feature ends here. This is a T-intersection.
            return self._process_pseudo_t_intersection(point, feat1, feat2)

        # Neither feature ends here; they just cross. This is a data error.
        # fmt: off
        note_text:str = QCoreApplication.translate("feature_note", "Intersection without endpoints - lines must be devided.")  # noqa: E501
        # fmt: on
        return self.create_questionable_point(
            point, intersecting_features, note=note_text
        )

    def _process_point(self, point: QgsPointXY) -> int:
        """Analyze a single point and create the appropriate feature(s).

        Args:
            point: The point of interest to process.

        Returns:
            The number of features created for this point.
        """
        point_key: tuple[float, float] = (round(point.x(), 4), round(point.y(), 4))
        if point_key in self._questionable_points_coords:
            log_debug(f"Skipping point {point_key} as a questionable point exists.")
            return 0

        search_geom: QgsGeometry = QgsGeometry.fromPointXY(point).buffer(
            Numbers.search_radius, 5
        )
        intersecting_features: list[QgsFeature] = self.get_intersecting_features(
            search_geom
        )
        n_intersections: int = len(intersecting_features)

        # Case 1: A single line is involved.
        # This can be an endpoint --> possible house connection, or
        # an intermediate vertex --> possible bend in multiline.
        if n_intersections == 1:
            return (
                self._possible_house_connection(point, intersecting_features[0])
                if self.is_endpoint(point, intersecting_features[0])
                else self._process_2_way_intersection(point, intersecting_features)
            )
        # Case 2: Two lines intersect.
        if n_intersections == 2:  # noqa: PLR2004
            return self._handle_two_intersections(point, intersecting_features)

        # Case 3: Three lines intersect.
        # This is always a T-piece candidate.
        if n_intersections == Numbers.intersec_t:
            return self.t_piece_finder.process_t_intersection(
                point, intersecting_features
            )

        # Case 4: More than three lines intersect.
        # This is always a questionable point.

        # fmt: off
        note_text: str = QCoreApplication.translate("feature_note", "More than three lines at intersection - split up intersection in multiple points.")  # noqa: E501
        # fmt: on
        return self.create_questionable_point(point, note=note_text)

    def _process_pseudo_t_intersection(
        self, point: QgsPointXY, feat1: QgsFeature, feat2: QgsFeature
    ) -> int:
        """Process an intersection of two features as a T-piece.

        This occurs when one feature passes through the intersection point (which is
        an intermediate vertex for it) and the other feature terminates at that point.

        Args:
            point: The intersection point.
            feat1: The first feature.
            feat2: The second feature.

        Returns:
            The number of features created.
        """
        is_endpoint1: bool = self.is_endpoint(point, feat1)

        # The feature that passes through is the main pipe.
        # The feature that terminates is the connecting pipe.
        main_pipe_feature: QgsFeature = feat2 if is_endpoint1 else feat1
        connecting_pipe: QgsFeature = feat1 if is_endpoint1 else feat2

        log_debug(
            f"Processing pseudo T-intersection between "
            f"main pipe '{main_pipe_feature.attribute('original_fid')}' and "
            f"connecting pipe '{connecting_pipe.attribute('original_fid')}'"
        )

        # To determine the geometry of the main pipe, we need the vertices of
        # the segment that the connecting pipe intersects with.
        p_before, p_after = self.get_adjacent_points_on_segment(
            point, main_pipe_feature
        )

        if not p_before or not p_after:
            return self.create_questionable_point(
                point, [feat1, feat2], note="Lines cross without a shared vertex."
            )

        return self.t_piece_finder.process_t_intersection_from_split_line(
            point, main_pipe_feature, connecting_pipe, p_before, p_after
        )

    def _process_2_way_intersection(
        self, point: QgsPointXY, features: list[QgsFeature]
    ) -> int:
        """Process a point where one or two lines meet, potentially forming a bend.

        This method handles bends within a single feature (an intermediate vertex)
        and bends formed by the intersection of two features.

        Args:
            point: The point of interest (intersection or vertex).
            features: A list containing one or two features.

        Returns:
            The number of features created.
        """
        created_count: int = 0
        p1: QgsPointXY | None = None
        p3: QgsPointXY | None = None
        if len(features) == 1:
            # This is an intermediate vertex on a single line feature.
            # It can only be a bend, not a reducer.
            p1, p3 = self.get_adjacent_vertices(point, features[0])
        elif len(features) == 2:  # noqa: PLR2004
            # This is an intersection of two features.
            # It can be a bend, a reducer, or both.
            p1 = self.get_other_endpoint(features[0], point)
            p3 = self.get_other_endpoint(features[1], point)

            # Check for a dimension change and create reducers if needed.
            if self.dim_field_name:
                dim1: int | None = features[0].attribute(self.dim_field_name)
                dim2: int | None = features[1].attribute(self.dim_field_name)

                if (
                    isinstance(dim1, int)
                    and isinstance(dim2, int)
                    and abs(dim1 - dim2) > 0
                ):
                    created_count += self.create_reducers(
                        point, dim1, dim2, features, 0.0
                    )

        if not p1 or not p3:
            # Could not determine segments to calculate an angle for a bend.
            # Reducers might have been created already.
            return created_count

        angle: float = self.calculate_angle(p1, point, p3)
        created_count += self.create_bend(point, features, angle)
        return created_count

    def _possible_house_connection(self, point: QgsPointXY, feature: QgsFeature) -> int:
        """Process a point that is the endpoint of a single line.

        Args:
            point: The endpoint coordinate.
            feature: The feature ending at this point.

        Returns:
            The number of features created (1 if successful, 0 otherwise).
        """
        # Check if it's a lone line segment or a true house connection
        is_other_end_connected = False
        for s_e_point in self.get_start_end_of_line(feature):
            if s_e_point.compare(point, Numbers.tiny_number):
                continue  # This is the endpoint we are currently processing
            # Check if the *other* endpoint is connected to something
            other_end_search: QgsGeometry = QgsGeometry.fromPointXY(s_e_point).buffer(
                Numbers.search_radius, 5
            )
            if len(self.get_intersecting_features(other_end_search)) > 1:
                is_other_end_connected = True
                break

        # If the other end is not connected, it's a floating line. Mark both ends.
        if not is_other_end_connected:
            # fmt: off
            note_text: str = QCoreApplication.translate("feature_note", "Unconnected line - make sure the line is connected to the network.")  # noqa: E501
            # fmt: on
            return sum(
                self.create_questionable_point(p_end, [feature], note=note_text)
                for p_end in self.get_start_end_of_line(feature)
            )

        return self.create_house_connection(point, [feature])
