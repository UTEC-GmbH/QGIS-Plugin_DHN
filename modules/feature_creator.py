"""Module: feature_creator.py

This module contains the FeatureCreator class for creating QgsFeature objects.
"""

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from .constants import FittingType, NewPointLayerFields, Numbers, PipeDimensions
from .logs_and_errors import log_debug
from .vector_analysis_tools import VectorAnalysisTools


class FeatureCreator(VectorAnalysisTools):
    """A class to create specific types of point features."""

    def __init__(
        self,
        selected_layer: QgsVectorLayer,
        temp_point_layer: QgsVectorLayer,
    ) -> None:
        """Initialize the FeatureCreator.

        Args:
            selected_layer: The QgsVectorLayer being analyzed.
            temp_point_layer: The temporary QgsVectorLayer to add new features to.
        """
        super().__init__(selected_layer, temp_point_layer)

    def create_questionable_point(
        self,
        point: QgsPointXY,
        features: list[QgsFeature] | None = None,
        note: str | None = None,
    ) -> int:
        """Create a 'questionable' point feature.

        Args:
            point: The geometry point for the new feature.
            features: A list of connected features. If None, intersecting
                features are found by searching around the point.
            note: An optional note to add to the feature's attributes.

        Returns:
            1 if the feature was created successfully, 0 otherwise.
        """
        if features is None:
            search_geom: QgsGeometry = QgsGeometry.fromPointXY(point).buffer(
                Numbers.search_radius, 5
            )
            features = self.get_intersecting_features(search_geom)

        if not features:
            log_debug(
                f"Could not create questionable point at {point.asWkt()}: "
                "No features found.",
                Qgis.Warning,
            )
            return 0

        attrs: dict[str, str | None] = {
            NewPointLayerFields.type.field_name: FittingType.QUESTIONABLE.translated
        }
        attrs |= self.get_connected_attributes(features)
        if note:
            attrs[NewPointLayerFields.notes.field_name] = note
        return 1 if self.create_feature(QgsGeometry.fromPointXY(point), attrs) else 0

    def create_house_connection(
        self, point: QgsPointXY, features: list[QgsFeature]
    ) -> int:
        """Create a 'house connection' feature.

        Args:
            point: The location of the house connection.
            features: A list of connected features (expected to contain exactly
                one feature).

        Returns:
            1 if the feature was created successfully, 0 otherwise.
        """
        attrs: dict = {
            NewPointLayerFields.type.field_name: FittingType.HOUSE_CONN.translated
        }
        attrs |= self.get_connected_attributes(features)

        if len(features) != 1:
            return (
                1 if self.create_feature(QgsGeometry.fromPointXY(point), attrs) else 0
            )

        feature: QgsFeature = features[0]
        if (field := self.load_heat_field_name) is not None:
            attrs[NewPointLayerFields.load_heat.field_name] = feature.attribute(field)
        if (field := self.load_water_field_name) is not None:
            attrs[NewPointLayerFields.load_water.field_name] = feature.attribute(field)
        if (field := self.load_total_field_name) is not None:
            attrs[NewPointLayerFields.load_total.field_name] = feature.attribute(field)

        return 1 if self.create_feature(QgsGeometry.fromPointXY(point), attrs) else 0

    def create_bend(
        self,
        point: QgsPointXY,
        features: list[QgsFeature],
        angle: float,
        note: str | None = None,
    ) -> int:
        """Create a 'bend' feature if the angle is sufficient.

        Args:
            point: The location of the bend.
            features: A list of features connected at the bend.
            angle: The deflection angle of the bend in degrees.
            note: An optional note to add to the feature's attributes.

        Returns:
            1 if the feature was created successfully, 0 otherwise.
        """
        if angle < Numbers.min_angle_bend:
            return 0

        attrs: dict = {
            NewPointLayerFields.type.field_name: FittingType.BEND.translated,
            NewPointLayerFields.angle.field_name: round(angle),
        }
        attrs |= self.get_connected_attributes(features)
        if note:
            attrs[NewPointLayerFields.notes.field_name] = note

        attrs.pop(NewPointLayerFields.dim_2.field_name, None)
        return 1 if self.create_feature(QgsGeometry.fromPointXY(point), attrs) else 0

    def create_t_piece(
        self,
        point: QgsPointXY,
        main_pipe: list[QgsFeature],
        connecting_pipe: QgsFeature,
        note: str,
    ) -> int:
        """Create a T-piece.

        The T-piece dimension is determined by the largest dimension of the main
        pipe and the dimension of the connecting pipe.

        Args:
            point: The geometry point for the T-piece.
            main_pipe: A list of the two features forming the main pipe.
            connecting_pipe: The feature representing the connecting pipe.
            note: The text for the note attribute.

        Returns:
            1 if the feature was created successfully, 0 otherwise.
        """
        all_features: list[QgsFeature] = [*main_pipe, connecting_pipe]
        attrs: dict = {
            NewPointLayerFields.type.field_name: FittingType.T_PIECE.translated,
            NewPointLayerFields.notes.field_name: note,
        }
        attrs |= self.get_connected_attributes(all_features)

        if self.dim_field_name:
            main_dims: list[int] = sorted(
                [
                    f.attribute(self.dim_field_name)
                    for f in main_pipe
                    if f.attribute(self.dim_field_name) is not None
                ]
            )
            conn_dim: int | None = connecting_pipe.attribute(self.dim_field_name)

            if main_dims and conn_dim is not None:
                attrs[NewPointLayerFields.dim_1.field_name] = main_dims[-1]
                attrs[NewPointLayerFields.dim_2.field_name] = conn_dim

        return 1 if self.create_feature(QgsGeometry.fromPointXY(point), attrs) else 0

    def create_reducers(
        self,
        point: QgsPointXY,
        dim_main_1: float,
        dim_main_2: float,
        main_pipe: list[QgsFeature],
        distance: float = Numbers.distance_t_reducer,
    ) -> int:
        """Create reducers for a dimension change in the main pipe.

        This method calculates how many reducers are needed and creates them
        at appropriate distances from the intersection point.

        Args:
            point: The point where the dimension change occurs.
            dim_main_1: Dimension of the first main pipe feature.
            dim_main_2: Dimension of the second main pipe feature.
            main_pipe: The two features forming the main pipe.
            distance: The distance from the point to place the first reducer.
                Defaults to Numbers.distance_t_reducer.

        Returns:
            The number of reducer features created.
        """
        large_dim: float = max(dim_main_1, dim_main_2)
        small_dim: float = min(dim_main_1, dim_main_2)
        smaller_dim_feature: QgsFeature = (
            main_pipe[0] if dim_main_1 < dim_main_2 else main_pipe[1]
        )

        try:
            large_idx: int = PipeDimensions.diameters.index(large_dim)
            small_idx: int = PipeDimensions.diameters.index(small_dim)
        except ValueError:
            # fmt: off
            note_text: str = QCoreApplication.translate("feature_note", "Non-standard pipe dimension detected for reducer.")  # noqa: E501
            # fmt: on
            return self.create_questionable_point(point, main_pipe, note=note_text)

        dim_steps: int = large_idx - small_idx
        if dim_steps <= 0:
            return 0

        num_reducers: int = (dim_steps - 1) // PipeDimensions.max_dim_jump_reducer + 1
        created_count = 0

        for i in range(num_reducers):
            # The first reducer is at 'distance',
            # subsequent ones have a constant distance between them.
            current_distance: float = distance + (Numbers.distance_t_reducer * i)
            reducer_point: QgsPointXY | None = self.get_point_along_line(
                point, smaller_dim_feature, current_distance
            )
            if not reducer_point:
                continue

            current_large_idx: int = large_idx - i * PipeDimensions.max_dim_jump_reducer
            current_small_idx: int = max(
                small_idx,
                large_idx - (i + 1) * PipeDimensions.max_dim_jump_reducer,
            )
            dim_from: int = PipeDimensions.diameters[current_large_idx]
            dim_to: int = PipeDimensions.diameters[current_small_idx]
            # fmt: off
            note_text: str = QCoreApplication.translate("feature_note", "Reducer from DN{0} to DN{1}").format(dim_from, dim_to)  # noqa: E501
            # fmt: on

            reducer_attrs: dict = self.get_connected_attributes([smaller_dim_feature])
            reducer_attrs |= {
                NewPointLayerFields.type.field_name: FittingType.REDUCER.translated,
                NewPointLayerFields.dim_1.field_name: dim_from,
                NewPointLayerFields.dim_2.field_name: dim_to,
                NewPointLayerFields.notes.field_name: note_text,
            }

            created_count += self.create_feature(
                QgsGeometry.fromPointXY(reducer_point), reducer_attrs
            )

        return created_count
