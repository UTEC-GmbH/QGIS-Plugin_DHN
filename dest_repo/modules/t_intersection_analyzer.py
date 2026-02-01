"""Module: t_intersection_analyzer.py

This module contains the TIntersectionAnalyzer class.
"""

from typing import NamedTuple

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
)
from qgis.PyQt.QtCore import QCoreApplication

from .constants import Names, Numbers
from .feature_creator import FeatureCreator
from .logs_and_errors import log_debug


class PipeAnalysisResult(NamedTuple):
    """Represents the result of pipe analysis at a T-intersection."""

    main_pipe: list[QgsFeature]
    connecting_pipe: QgsFeature


class TIntersectionAnalyzer(FeatureCreator):
    """A class to classify T-pieces and associated features."""

    def process_t_intersection(
        self, point: QgsPointXY, features: list[QgsFeature]
    ) -> int:
        """Analyzes a 3-way intersection and creates appropriate features.

        This method identifies the main and connecting pipes, checks for bends
        and dimension changes, and creates T-pieces, bends, and reducers
        as needed.

        Args:
            point: The intersection point.
            features: The three features intersecting at the point.

        Returns:
            The number of features created.
        """
        created_count: int = 0

        # 1. Find the main pipe (the two most collinear lines)
        pipe_analysis: PipeAnalysisResult | None = self._find_main_pipe(point, features)
        if not pipe_analysis:
            # fmt: off
            note_text:str = QCoreApplication.translate("feature_note", "Could not determine main pipe.")  # noqa: E501
            # fmt: on
            return self.create_questionable_point(point, features, note=note_text)

        # 2. Get the remote endpoints of the main pipe
        p_main_1: QgsPointXY | None = self.get_other_endpoint(
            pipe_analysis.main_pipe[0], point
        )
        p_main_2: QgsPointXY | None = self.get_other_endpoint(
            pipe_analysis.main_pipe[1], point
        )

        if not p_main_1 or not p_main_2:
            # fmt: off
            note_text:str = QCoreApplication.translate("feature_note", "Could not determine main pipe endpoints.")  # noqa: E501
            # fmt: on
            return self.create_questionable_point(point, features, note=note_text)

        # 2.1 Check for a bend in the connecting pipe (deviation from 90°)
        created_count += self._check_and_create_connecting_bend(
            point, p_main_1, p_main_2, pipe_analysis.connecting_pipe
        )

        # 3. Check for a bend in the main pipe. The logic depends on this.
        bend_angle: float = self.calculate_angle(p_main_1, point, p_main_2)
        t_point: QgsPointXY = point

        if bend_angle > Numbers.min_angle_bend:
            # If there's a significant bend in the main pipe, create a bend feature
            # at the intersection point. The T-piece will also be at this point.
            # fmt: off
            note: str = QCoreApplication.translate("feature_note", "Bend in main pipe (behind T-piece)")  # noqa: E501
            # fmt: on
            created_count += self.create_bend(
                point, pipe_analysis.main_pipe, bend_angle, note
            )

        # Build the note with main pipe IDs and dimensions
        note_parts: list[str] = []
        for feature in pipe_analysis.main_pipe:
            part: str = str(feature.attribute("original_fid"))
            if self.dim_field_name and (dim := feature.attribute(self.dim_field_name)):
                part += f" ({Names.dim_prefix}{dim})"
            note_parts.append(part)

        # fmt: off
        note_text: str = QCoreApplication.translate("feature_note", "Main pipe: {0}").format(" & ".join(note_parts))  # noqa: E501
        # fmt: on
        created_count += self.create_t_piece(
            t_point, pipe_analysis.main_pipe, pipe_analysis.connecting_pipe, note_text
        )

        # 4. Check if a reducer on the main pipe is needed
        if self.dim_field_name:
            created_count += self._check_and_create_reducer(
                point, pipe_analysis.main_pipe, pipe_analysis.connecting_pipe
            )

        return created_count

    def process_t_intersection_from_split_line(
        self,
        point: QgsPointXY,
        main_pipe_feature: QgsFeature,
        connecting_pipe: QgsFeature,
        p_before: QgsPointXY,
        p_after: QgsPointXY,
    ) -> int:
        """Analyzes a T-intersection derived from splitting a single main pipe.

        This is used for "pseudo" T-intersections where one line terminates on
        the segment of another.

        Args:
            point: The intersection point.
            main_pipe_feature: The single feature representing the main pipe.
            connecting_pipe: The feature that terminates on the main pipe.
            p_before: The vertex on the main pipe segment before the intersection.
            p_after: The vertex on the main pipe segment after the intersection.

        Returns:
            The number of features created.
        """

        log_debug(
            f"Processing intersection between "
            f"connecting pipe '{connecting_pipe.attribute('original_fid')}' "
            f"and main pipe '{main_pipe_feature.attribute('original_fid')}'"
        )

        created_count: int = 0

        # 1. Check for a bend in the main pipe.
        bend_angle: float = self.calculate_angle(p_before, point, p_after)

        # To reuse the main logic, we create two dummy features that represent
        # the two sides of the main pipe. This is simpler than rewriting the
        # downstream logic.
        main_pipe_dummy1 = QgsFeature(main_pipe_feature)
        main_pipe_dummy2 = QgsFeature(main_pipe_feature)
        main_pipe_features: list[QgsFeature] = [main_pipe_dummy1, main_pipe_dummy2]

        if bend_angle > Numbers.min_angle_bend:
            # fmt: off
            note: str = QCoreApplication.translate("feature_note", "Bend in main pipe (behind T-piece)")  # noqa: E501
            # fmt: on
            created_count += self.create_bend(
                point, main_pipe_features, bend_angle, note
            )

        # 1.1 Check for a bend in the connecting pipe
        created_count += self._check_and_create_connecting_bend(
            point, p_before, p_after, connecting_pipe
        )

        # 2. Build the note and create the T-piece
        part: str = str(main_pipe_feature.attribute("original_fid"))
        if self.dim_field_name and (
            dim := main_pipe_feature.attribute(self.dim_field_name)
        ):
            part += f" ({Names.dim_prefix}{dim})"
        # fmt: off
        note_text: str = QCoreApplication.translate("feature_note", "Main pipe: {0}").format(part)  # noqa: E501
        # fmt: on
        created_count += self.create_t_piece(
            point, main_pipe_features, connecting_pipe, note_text
        )

        # 3. Check for a reducer. This is not possible when the main pipe is a
        # single feature, so we only check the connecting pipe.
        if self.dim_field_name:
            created_count += self._check_and_create_reducer(
                point, main_pipe_features, connecting_pipe
            )

        return created_count

    def _find_main_pipe(
        self, point: QgsPointXY, features: list[QgsFeature]
    ) -> PipeAnalysisResult | None:
        """Identify the main pipe and connecting pipe from three features.

        This method first attempts to identify the connecting pipe by checking for a
        feature with a free endpoint (a house connection). If that fails, it falls
        back to finding the two most collinear features, which are assumed to form
        the main pipe.

        Args:
            point: The intersection point.
            features: A list of three intersecting features.

        Returns:
            A PipeAnalysisResult object containing the main and connecting pipes,
            or None if identification fails.
        """
        if len(features) != Numbers.intersec_t:
            return None

        # Strategy 1: Check for a single, unconnected endpoint (house connection)
        result: PipeAnalysisResult | None = self._find_pipe_by_endpoint_connectivity(
            point, features
        )
        return result or self._find_pipe_by_angle(point, features)

    def _find_pipe_by_endpoint_connectivity(
        self, point: QgsPointXY, features: list[QgsFeature]
    ) -> PipeAnalysisResult | None:
        """Identify pipes by checking for a feature with a free endpoint.

        If exactly one of the three features has an endpoint that is not connected
        to any other line, that feature is considered the connecting pipe.

        Args:
            point: The intersection point.
            features: A list of three intersecting features.

        Returns:
            A PipeAnalysisResult object if successful, otherwise None.
        """
        unconnected_features: list[QgsFeature] = []
        for feature in features:
            other_end: QgsPointXY | None = self.get_other_endpoint(feature, point)
            if not other_end:
                continue

            search_geom: QgsGeometry = QgsGeometry.fromPointXY(other_end).buffer(
                Numbers.search_radius, 5
            )
            # A free end intersects with only one feature: the line itself.
            if len(self.get_intersecting_features(search_geom)) == 1:
                unconnected_features.append(feature)

        if len(unconnected_features) == 1:
            connecting_pipe: QgsFeature = unconnected_features[0]
            main_pipe: list[QgsFeature] = [f for f in features if f != connecting_pipe]
            return PipeAnalysisResult(main_pipe, connecting_pipe)

        return None

    def _find_pipe_by_angle(
        self, point: QgsPointXY, features: list[QgsFeature]
    ) -> PipeAnalysisResult | None:
        """Identify the main pipe by finding the most collinear pair of features.

        Args:
            point: The intersection point.
            features: A list of three intersecting features.

        Returns:
            A PipeAnalysisResult object if successful, otherwise None.
        """
        # log_prefix: str = "Find Main Pipe → "
        feature_endpoints: dict[int, QgsPointXY | None] = {
            f.id(): self.get_other_endpoint(f, point) for f in features
        }

        min_angle: float = Numbers.circle_full
        main_pipe: list[QgsFeature] = []

        for i in range(len(features)):
            for j in range(i + 1, len(features)):
                f1: QgsFeature = features[i]
                f2: QgsFeature = features[j]
                p1: QgsPointXY | None = feature_endpoints.get(f1.id())
                p2: QgsPointXY | None = feature_endpoints.get(f2.id())

                if p1 and p2:
                    angle: float = self.calculate_angle(p1, point, p2)
                    if angle < min_angle:
                        min_angle = angle
                        main_pipe = [f1, f2]
                        # log_debug(
                        #     f"features: [{f1.attribute('original_fid')}, "
                        #     f"{f2.attribute('original_fid')}] → "
                        #     f"angle: {round(angle, 1)}°",
                        #     icon="🐞",
                        #     prefix=log_prefix,
                        # )

        if not main_pipe:
            return None

        connecting_pipe: QgsFeature | None = next(
            (f for f in features if f not in main_pipe), None
        )
        if connecting_pipe:
            # log_debug(
            #     "Connecting pipe identified through angle\n"
            #     f"Connecting pipe: {connecting_pipe.attribute('original_fid')} | "
            #     f"Main pipe: {[pip.attribute('original_fid') for pip in main_pipe]}",
            #     icon="🐞",
            #     prefix=log_prefix,
            # )
            return PipeAnalysisResult(main_pipe, connecting_pipe)
        return None

    def _check_and_create_reducer(
        self,
        point: QgsPointXY,
        main_pipe: list[QgsFeature],
        connecting_pipe: QgsFeature,
    ) -> int:
        """Check for dimension changes and creates a reducer if necessary.

        This method handles two scenarios:
        1. A dimension change within the main pipe.
        2. A connecting pipe having a larger diameter than the main pipe.

        Args:
            point: The T-intersection point.
            main_pipe: The two features forming the main pipe.
            connecting_pipe: The feature forming the connecting pipe.

        Returns:
            The number of reducer features created (0 or more).
        """
        if not self.dim_field_name:
            return 0

        dim_main_1: int | None = main_pipe[0].attribute(self.dim_field_name)
        dim_main_2: int | None = main_pipe[1].attribute(self.dim_field_name)

        if not isinstance(dim_main_1, int) or not isinstance(dim_main_2, int):
            return 0

        if abs(dim_main_1 - dim_main_2) > 0:
            return self.create_reducers(point, dim_main_1, dim_main_2, main_pipe)

        dim_conn: int | None = connecting_pipe.attribute(self.dim_field_name)
        if dim_conn is not None and dim_main_1 is not None and dim_conn > dim_main_1:
            # fmt: off
            note_text: str = QCoreApplication.translate("feature_note", "Connecting pipe has a larger diameter than the main pipe.")  # noqa: E501
            # fmt: on
            return self.create_questionable_point(
                point, [*main_pipe, connecting_pipe], note=note_text
            )
        return 0

    def _check_and_create_connecting_bend(
        self,
        point: QgsPointXY,
        p_main_1: QgsPointXY,
        p_main_2: QgsPointXY,
        connecting_pipe: QgsFeature,
    ) -> int:
        """Check if the connecting pipe requires a bend feature.

        This method calculates the angle between the connecting pipe and the
        main pipe segments. If the angle deviates from 90 degrees by more than
        the minimum bend angle (and the T-piece cannot be aligned to avoid it),
        a bend feature is created.

        Args:
            point: The T-intersection point.
            p_main_1: The remote endpoint of the first main pipe segment.
            p_main_2: The remote endpoint of the second main pipe segment.
            connecting_pipe: The connecting pipe feature.

        Returns:
            1 if a bend was created, 0 otherwise.
        """
        p_conn: QgsPointXY | None = self.get_other_endpoint(connecting_pipe, point)
        if not p_conn:
            return 0

        # Calculate angles relative to both sides of the main pipe
        angle_1: float = self.calculate_angle(p_main_1, point, p_conn)
        angle_2: float = self.calculate_angle(p_main_2, point, p_conn)

        # Calculate deviation from 90 degrees
        dev_1: float = abs(90 - angle_1)
        if dev_1 < Numbers.min_angle_bend:
            return 0

        dev_2: float = abs(90 - angle_2)
        if dev_2 < Numbers.min_angle_bend:
            return 0

        # We assume the T-piece will be aligned causing the minimum deviation
        min_dev: float = min(dev_1, dev_2)

        if min_dev > Numbers.min_angle_bend:
            # fmt: off
            note: str = QCoreApplication.translate("feature_note", "Bend in connecting pipe (doesn't join the T-intersection at 90° angle.)")  # noqa: E501
            # fmt: on
            # Create a bend regarding the connecting pipe
            return self.create_bend(point, [connecting_pipe], min_dev, note)

        return 0
