"""Module: constants.py

This module contains constant values.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from qgis.core import QgsApplication, QgsSvgCache
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtCore import QMetaType as Qmt
from qgis.PyQt.QtGui import QColor, QIcon, QPixmap

from .context import PluginContext

if TYPE_CHECKING:
    from pathlib import Path


if PluginContext.is_qt6():
    QMT_Map = Qmt.Type.QVariantMap
    QMT_List = Qmt.Type.QVariantList
    QMT_StringList = Qmt.Type.QStringList  # Qt6 has QStringList in Type
    QMT_String = Qmt.Type.QString
    QMT_Int = Qmt.Type.Int
    QMT_Double = Qmt.Type.Double
else:
    QMT_Map = Qmt.QVariantMap
    QMT_List = Qmt.QVariantList
    QMT_StringList = Qmt.QStringList
    QMT_String = Qmt.QString
    QMT_Int = Qmt.Int
    QMT_Double = Qmt.Double

PROBLEMATIC_FIELD_TYPES: list = [QMT_Map, QMT_List, QMT_StringList]


T = TypeVar("T")


class ClassProperty(Generic[T]):
    """A decorator that converts a method into a read-only class property."""

    def __init__(self, fget: Callable[[Any], T]) -> None:
        """Initialize the class property.

        Args:
            fget: The function to get the value.
        """
        self.fget: Callable[[Any], T] = fget

    def __get__(self, instance: object | None, owner: type | None = None) -> T:
        """Return the value of the property.

        Args:
            instance: The instance accessing the property.
            owner: The class owning the property.

        Returns:
            The value returned by the getter function.
        """
        return self.fget(owner)


def QT_TRANSLATE_NOOP(context: str, source: str) -> str:  # noqa: N802
    """Mark a string for translation without translating it immediately.

    Args:
        context: The translation context.
        source: The string to translate.

    Returns:
        The source string.
    """
    _: str = context
    return source


# pylint: disable=too-few-public-methods
class Icons:
    """Holds plugin icons."""

    @staticmethod
    def _qicon(
        filename: str,
        *,
        dynamic: bool = False,
        dark: str = "#1c274c",
        light: str = "#738ad5",
    ) -> QIcon:
        """Load an icon from the icons directory.

        Args:
            filename: The name of the icon file (including extension).
            dynamic: Whether to load the icon dynamically (default: False).
            dark: The color to use for the dark theme (default: "#1c274c").
            light: The color to use for the light theme (default: "#738ad5").

        Returns:
            The loaded QIcon object.
        """
        icons_path: Path = PluginContext.icons_path()

        if not dynamic:
            return QIcon(str(icons_path / filename))

        is_dark: bool = PluginContext.is_dark_theme()

        fill_colour: QColor = QColor(light) if is_dark else QColor(dark)
        stroke_colour: QColor = QColor(dark) if is_dark else QColor(light)

        svg_cache: QgsSvgCache | None = QgsApplication.svgCache()
        if svg_cache is None:
            return QIcon(str(icons_path / filename))
        icon = svg_cache.svgAsImage(
            str(icons_path / filename), 48, fill_colour, stroke_colour, 1, 1
        )[0]

        return QIcon(QPixmap.fromImage(icon))

    @property
    def main_icon(self) -> QIcon:
        """Return the main plugin icon."""
        return self._qicon("main_icon.svg")

    @property
    def main_menu_run(self) -> QIcon:
        """Return the run icon, dynamically colored for the current theme."""
        return self._qicon("main_menu_run.svg", dynamic=True)

    @property
    def main_menu_excel(self) -> QIcon:
        """Return the redo-output icon, dynamically colored for the current theme."""
        return self._qicon("main_menu_excel.svg", dynamic=True)

    @property
    def fitting_bend(self) -> QIcon:
        """Return the bend icon."""
        return self._qicon("fitting_bend.svg")

    @property
    def fitting_houseconn(self) -> QIcon:
        """Return the house-connection icon."""
        return self._qicon("fitting_houseconnection.svg")

    @property
    def fitting_questionable(self) -> QIcon:
        """Return the questionable icon."""
        return self._qicon("fitting_questionable.svg")

    @property
    def fitting_reducer(self) -> QIcon:
        """Return the reducer icon."""
        return self._qicon("fitting_reducer.svg")

    @property
    def fitting_t_piece(self) -> QIcon:
        """Return the t-piece icon."""
        return self._qicon("fitting_t-piece.svg")


ICONS = Icons()


class FittingType(Enum):
    """Enumeration for the different types of fittings (points of interest).

    The value of each member is the string representation
    used in the attribute table.
    """

    HOUSE_CONN = QT_TRANSLATE_NOOP("cont_FittingType", "House-Connection")
    BEND = QT_TRANSLATE_NOOP("cont_FittingType", "Bend")
    T_PIECE = QT_TRANSLATE_NOOP("cont_FittingType", "T-Piece")
    CONNECTOR = QT_TRANSLATE_NOOP("cont_FittingType", "Connector")
    REDUCER = QT_TRANSLATE_NOOP("cont_FittingType", "Reducer")
    QUESTIONABLE = QT_TRANSLATE_NOOP("cont_FittingType", "Questionable")

    @property
    def translated(self) -> str:
        """Return the translated string representation of the fitting type."""
        return QCoreApplication.translate("cont_FittingType", self.value)


class PipeType(Enum):
    """Enumeration for the different types of pipes.

    The value of each member is the string representation
    used in the attribute table.
    """

    MAIN = QT_TRANSLATE_NOOP("cont_PipeType", "Main Pipe")
    CONN = QT_TRANSLATE_NOOP("cont_PipeType", "Connecting Pipe")
    FORK = QT_TRANSLATE_NOOP("cont_PipeType", "Main Pipe (Fork)")

    @property
    def translated(self) -> str:
        """Return the translated string representation of the pipe type."""
        return QCoreApplication.translate("cont_PipeType", self.value)


@dataclass(frozen=True)
class PipeDimensions:
    """Defines standard pipe dimensions and reduction parameters."""

    max_dim_jump_reducer: int = 2

    diameters: tuple = (
        20,
        25,
        32,
        40,
        50,
        65,
        80,
        100,
        125,
        150,
        200,
        250,
        300,
        350,
        400,
        450,
        500,
        600,
        700,
        800,
        900,
        1000,
    )


@dataclass(frozen=True)
class Colours:
    """Defines hex color codes for different fitting types."""

    bend: str = "#e2b60a"
    house: str = "#55ddff"
    t_piece: str = "#668000"
    reducer: str = "#9900ff"
    connector: str = "#444444"
    questionable: str = "#ff1d1d"


class Names:
    """Contains string constants used for naming layers, files, and fields."""

    dim_prefix: str = "DN"
    separator: str = " / "

    excel_dir: str = "UTEC"
    excel_file_output: str = "plugin_output"
    excel_file_template: str = "UTEC_DHN.xlsx"

    # Mögliche Namen für die Spalte mit den Rohrdimensionen
    # in der Attributtabelle des alten (gewälten) Layers
    sel_layer_field_dim: tuple[str, ...] = (
        "diameter",
        "dim",
        "DN",
        "Dimension",
        "Durchmesser",
        "p_diameter",
        "p_diameter_DN",
    )

    sel_layer_field_load: tuple[str, ...] = (
        "loadRel",
        "p_load",
        "p_load_rel",
    )

    @ClassProperty
    def layer_group(self) -> str:
        """Return the layer group name."""
        return QCoreApplication.translate("cont_Names", "UTEC Network Analysis")

    @ClassProperty
    def new_fittings_layer_suffix(self) -> str:
        """Return the suffix for the new layer name."""
        return QCoreApplication.translate("cont_Names", " - Material")

    @ClassProperty
    def new_pipe_layer_suffix(self) -> str:
        """Return the suffix for the new pipe layer name."""
        return QCoreApplication.translate("cont_Names", " - Net")

    @ClassProperty
    def excel_file_summary(self) -> str:
        """Return the summary sheet name."""
        return QCoreApplication.translate("cont_Names", "UTEC_Network")


@dataclass(frozen=True)
class Numbers:
    """Contains numeric constants used for geometry analysis and layout."""

    circle_full: float = 360  # The number of degrees in a full circle.
    circle_semi: float = 180  # The number of degrees in a semi-circle.

    min_points_line: int = 2  # Minimum number of points for a line.
    min_points_multiline: int = 3  # Minimum number of points for a multiline.

    min_intersec: int = 2  # Minimum number of lines to consider an intersection.
    intersec_t: int = 3  # Minimum number of lines to consider a T-intersection.
    min_angle_bend: int = 3  # Minimum angle to consider a bent line as 'Bogen'.

    distance_t_reducer: float = 0.5  # The distance between T-piece and reducer.
    distance_t_bend: float = 0.25  # The distance between T-piece and bend.

    search_radius: float = 0.05  # Search radius for finding intersections.

    tiny_number: float = 1e-6  # A small number used for floating point comparisons.

    new_layer_font_size: int = 8
    new_layer_label_mask_size: float = 0.8
    new_layer_label_distance: float = 2.5


class NewPointLayerFields(Enum):
    """Constants for layer field attributes, accessible via dot notation.

    This Enum is directly iterable.
    """

    type = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Type"),
        QMT_String,
    )
    designation = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Designation"),
        QMT_String,
    )
    dim_1 = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Diameter 1"),
        QMT_Int,
    )
    dim_2 = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Diameter 2"),
        QMT_Int,
    )
    angle = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Bend-Angle"),
        QMT_Int,
    )
    load_heat = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Load - Heating"),
        QMT_Double,
    )
    load_water = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Load - Water"),
        QMT_Double,
    )
    connected = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Connected Pipes"),
        QMT_String,
    )
    notes = (
        QT_TRANSLATE_NOOP("cont_PointLayerFields", "Notes"),
        QMT_String,
    )

    def __init__(self, display_name: str, q_type: Qmt) -> None:
        """Initialize the enum member.

        Args:
            display_name: The display name of the field.
            q_type: The QMetaType of the field data.
        """
        self._display_name: str = display_name
        self._q_type: Qmt = q_type

    @property
    def field_name(self) -> str:
        """The display name of the field."""
        return QCoreApplication.translate("cont_PointLayerFields", self._display_name)

    @property
    def data_type(self) -> Qmt:
        """The QVariant type of the field."""
        return self._q_type


class NewLineLayerFields(Enum):
    """Constants for pipe layer field attributes, accessible via dot notation.

    This Enum is directly iterable.
    """

    org_id = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Original ID"),
        QMT_Int,
    )
    dim = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Original Diameter"),
        QMT_Int,
    )
    load = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Original Load"),
        QMT_Double,
    )
    length = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Route length"),
        QMT_Double,
    )
    type = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Type"),
        QMT_String,
    )
    branch = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Branch"),
        QMT_String,
    )
    designation = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Designation"),
        QMT_String,
    )
    conn_buildings = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Connected Buildings"),
        QMT_String,
    )
    notes = (
        QT_TRANSLATE_NOOP("cont_LineLayerFields", "Notes"),
        QMT_String,
    )

    def __init__(self, display_name: str, q_type: Qmt) -> None:
        """Initialize the enum member.

        Args:
            display_name: The display name of the field.
            q_type: The QMetaType of the field data.
        """
        self._display_name: str = display_name
        self._q_type: Qmt = q_type

    @property
    def field_name(self) -> str:
        """The display name of the field."""
        return QCoreApplication.translate("cont_LineLayerFields", self._display_name)

    @property
    def data_type(self) -> Qmt:
        """The QVariant type of the field."""
        return self._q_type
