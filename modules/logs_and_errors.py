"""Module: logs&errors.py

This module contains logging functions and custom error classes.
"""

import inspect
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, NoReturn

from qgis.core import Qgis, QgsMessageLog
from qgis.utils import iface

from modules import constants as cont

if TYPE_CHECKING:
    from qgis.gui import QgisInterface

LEVEL_ICON: dict[Qgis.MessageLevel, str] = {
    Qgis.Success: cont.Icons.Success,
    Qgis.Info: cont.Icons.Info,
    Qgis.Warning: cont.Icons.Warning,
    Qgis.Critical: cont.Icons.Critical,
}


def file_line(frame: FrameType | None) -> str:
    """Return the filename and line number of the caller.

    This function inspects the call stack to determine the file and line number
    from which `log_debug` or `log_and_show_error` was called.

    Args:
        frame: The current frame object,
            typically obtained via `inspect.currentframe()`.

    Returns:
        A string formatted as " (filename: line_number)" or an empty string if
        the frame information is not available.
    """

    if frame and frame.f_back:
        filename: str = Path(frame.f_back.f_code.co_filename).name
        lineno: int = frame.f_back.f_lineno
        return f" [{filename}: {lineno}]"
    return ""


def log_debug(
    message: str,
    level: Qgis.MessageLevel = Qgis.Info,
    file_line_number: str | None = None,
    icon: str | None = None,
) -> None:
    """Log a debug message.

    Logs a message to the QGIS message log, prepending an icon and appending
    the filename and line number of the caller.

    Args:
        message: The message to log.
        level: The QGIS message level (Success, Info, Warning, Critical).
            Defaults to Qgis.Info.
        file_line_number: An optional string to append to the message.
            Defaults to the filename and line number of the caller.
        icon: An optional icon string to prepend to the message. If None,
            a default icon based on `msg_level` will be used.

    Returns:
        None
    """

    file_line_number = file_line_number or file_line(inspect.currentframe())

    icon = icon or LEVEL_ICON[level]
    message = f"{icon} {message}{file_line_number}"

    QgsMessageLog.logMessage(f"{message}", "Plugin: UTEC FW Netz", level=level)


def show_message(
    message: str, level: Qgis.MessageLevel = Qgis.Critical, duration: int = 0
) -> None:
    """Display a message in the QGIS message bar.

    This helper function standardizes error handling by ensuring that a critical
    error is logged and displayed to the user.

    :param error_msg: The error message to display and include in the exception.
    :param level: The QGIS message level (Warning, Critical, etc.).
    :param duration: The duration of the message in seconds (default: 0 = until closed)
    :return: None
    """

    qgis_iface: QgisInterface | None = iface
    if qgis_iface and (msg_bar := qgis_iface.messageBar()):
        msg_bar.clearWidgets()
        msg_bar.pushMessage(
            f"{LEVEL_ICON[level]} {message}", level=level, duration=duration
        )
    else:
        QgsMessageLog.logMessage(
            f"{cont.Icons.Warning} iface not set or message bar not available! "
            f"→ Error not displayed in message bar."
        )


class CustomUserError(Exception):
    """Custom exception for user-related errors in the plugin."""


class CustomRuntimeError(Exception):
    """Custom exception for runtime errors in the plugin."""


def raise_user_error(error_msg: str) -> NoReturn:
    """Log a user-facing warning, display it, and raise a CustomUserError."""

    file_line_number: str = file_line(inspect.currentframe())
    log_msg: str = f"{LEVEL_ICON[Qgis.Warning]} {error_msg}{file_line_number}"
    QgsMessageLog.logMessage(
        f"{log_msg}", "Plugin: UTEC FW Netz", level=Qgis.Warning
    )

    show_message(error_msg, level=Qgis.Warning)
    raise CustomUserError(error_msg)


def raise_runtime_error(error_msg: str) -> NoReturn:
    """Log a critical error, display it, and raise a CustomRuntimeError."""

    file_line_number: str = file_line(inspect.currentframe())
    error_msg = f"{error_msg}{file_line_number}"
    log_msg: str = f"{LEVEL_ICON[Qgis.Critical]} {error_msg}"
    QgsMessageLog.logMessage(
        f"{log_msg}", "Plugin: UTEC FW Netz", level=Qgis.Critical
    )

    show_message(error_msg)
    raise CustomRuntimeError(error_msg)


