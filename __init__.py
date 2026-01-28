"""__init__.py

This script initializes the plugin, making it known to QGIS.
"""

# pylint: disable=invalid-name, import-outside-toplevel
# ruff: noqa: ANN001, ANN201, N802, PLC0415


def classFactory(iface):
    """Load the main plugin class.

    Args:
        iface: The QGIS interface instance.

    Returns:
        DHN: An instance of the plugin class.
    """

    from .UTEC_DHN import DHN

    return DHN(iface)
