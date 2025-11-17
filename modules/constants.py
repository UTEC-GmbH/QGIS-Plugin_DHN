"""Module: constants.py

This module contains constant values.
"""

from dataclasses import dataclass
from enum import Enum

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtCore import QMetaType as Qmt

PROBLEMATIC_FIELD_TYPES: list = [Qmt.QVariantMap, Qmt.QVariantList, Qmt.QStringList]


@dataclass(frozen=True)
class Icons:
    """Class: Icons

    This class contains icon constants.
    """

    Success: str = "🎉"
    Info: str = "💡"
    Warning: str = "💥"
    Critical: str = "💀"


@dataclass(frozen=True)
class PipeDimensions:
    """Class: PipeDimensions

    This class contains pipe dimensions.
    """

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
class Names:
    """Class: Names

    This class contains names.
    """

    new_layer_suffix: str = " - FW_Netz"
    dim_prefix: str = "DN"
    line_separator: str = " / "

    # Namen für Saplten der Attributtabelle des alten (gewälten) Layers
    sel_layer_field_dim: tuple[str, ...] = (
        "diameter",
        "dim",
        "DN",
        "Dimension",
        "Durchmesser",
    )


@dataclass(frozen=True)
class Numbers:
    """Class: Numbers

    This class contains numeric constants used throughout the plugin.

    """

    circle_full: float = 360  # The number of degrees in a full circle.
    circle_semi: float = 180  # The number of degrees in a semi-circle.


class NewLayerFields(Enum):
    """Constants for layer field attributes, accessible via dot notation.

    This Enum is directly iterable.
    """

    # Enum members are defined as tuples: (display_name, qgis_data_type)
    type: tuple[str, Qmt] = (
        QCoreApplication.translate("NewLayerFields", "type"),
        Qmt.QString,
    )
    dim: tuple[str, Qmt] = (
        QCoreApplication.translate("NewLayerFields", "dim"),
        Qmt.Int,
    )
    connected: tuple[str, Qmt] = (
        QCoreApplication.translate("NewLayerFields", "connected"),
        Qmt.QString,
    )
    notes: tuple[str, Qmt] = (
        QCoreApplication.translate("NewLayerFields", "notes"),
        Qmt.QString,
    )

    def __init__(self, display_name: str, q_type: Qmt) -> None:
        """Initialize the enum member with its attributes."""
        self._display_name: str = display_name
        self._q_type: Qmt = q_type

    @property
    def name(self) -> str:
        """The display name of the field."""
        return self._display_name

    @property
    def data_type(self) -> Qmt:
        """The QVariant type of the field."""
        return self._q_type
