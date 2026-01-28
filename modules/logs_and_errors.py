"""Module: logs&errors.py

This module contains logging functions and custom error classes.
"""

import inspect
from pathlib import Path
from types import FrameType
from typing import NoReturn

from qgis.core import Qgis, QgsMessageLog, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication

from .constants import Names, NewLayerFields

LOG_TAG: str = "Plugin: DHN"
LEVEL_ICON: dict[Qgis.MessageLevel, str] = {
    Qgis.Success: "🎉",
    Qgis.Info: "💡",
    Qgis.Warning: "💥",
    Qgis.Critical: "💀",
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
    prefix: str | None = None,
) -> None:
    """Log a debug message.

    Logs a message to the QGIS message log, prepending an icon and appending
    the filename and line number of the caller.

    Args:
        message: The message to log.
        level: The QGIS message level.
            (Qgis.Success, Qgis.Info, Qgis.Warning or Qgis.Critical)
            Defaults to Qgis.Info.
        file_line_number: An optional string to append to the message.
            Defaults to the filename and line number of the caller.
        icon: An optional icon string to prepend to the message. If None,
            a default icon based on `msg_level` will be used.
        prefix: An optional prefix string to prepend to the message.

    Returns:
        None
    """
    file_line_number = file_line_number or file_line(inspect.currentframe())

    icon = icon or LEVEL_ICON[level]
    message = f"{icon} {prefix or ''} {message}{file_line_number}"

    QgsMessageLog.logMessage(f"{message}", LOG_TAG, level=level)


def show_message(
    message: str, level: Qgis.MessageLevel = Qgis.Critical, duration: int = 0
) -> None:
    """Display a message in the QGIS message bar.

    This helper function standardizes error handling by ensuring that a critical
    error is logged and displayed to the user.

    Args:
        message: The error message to display and include in the exception.
        level: The QGIS message level (Warning, Critical, etc.).
            Defaults to Qgis.Critical.
        duration: The duration of the message in seconds (default: 0 = until closed).
    """
    # pylint: disable=import-outside-toplevel
    from .context import PluginContext  # noqa: PLC0415

    if msg_bar := PluginContext.message_bar():
        msg_bar.clearWidgets()
        msg_bar.pushMessage(
            f"{LEVEL_ICON[level]} {message}", level=level, duration=duration
        )
    else:
        QgsMessageLog.logMessage(
            f"{LEVEL_ICON[Qgis.Warning]} message bar not available! "
            f"→ Message not displayed in message bar."
        )


class CustomUserError(Exception):
    """Custom exception for user-related errors in the plugin."""


class CustomRuntimeError(Exception):
    """Custom exception for runtime errors in the plugin."""


def raise_runtime_error(error_msg: str) -> NoReturn:
    """Log a critical error, display it, and raise a CustomRuntimeError.

    Args:
        error_msg: The error message to be displayed and raised.

    Raises:
        CustomRuntimeError: The raised exception with the error message.
    """
    file_line_number: str = file_line(inspect.currentframe())
    error_msg = f"{error_msg}{file_line_number}"
    log_msg: str = f"{LEVEL_ICON[Qgis.Critical]} {error_msg}"
    QgsMessageLog.logMessage(f"{log_msg}", LOG_TAG, level=Qgis.Critical)

    show_message(error_msg)
    raise CustomRuntimeError(error_msg)


def raise_user_error(error_msg: str) -> NoReturn:
    """Log a user-facing warning, display it, and raise a CustomUserError.

    Args:
        error_msg: The error message to be displayed and raised.

    Raises:
        CustomUserError: The raised exception with the error message.
    """
    file_line_number: str = file_line(inspect.currentframe())
    log_msg: str = f"{LEVEL_ICON[Qgis.Warning]} {error_msg}{file_line_number}"
    QgsMessageLog.logMessage(f"{log_msg}", LOG_TAG, level=Qgis.Warning)

    show_message(error_msg, level=Qgis.Warning)
    raise CustomUserError(error_msg)


def create_summary_message(
    new_layer: QgsVectorLayer,
    selected_layer_name: str,
    *,
    multiline: bool = False,
) -> str:
    """Create a summary message of the features found in the new layer.

    Args:
        new_layer: The layer containing the new features.
        selected_layer_name: The name of the selected layer.
        multiline: If True, format the summary as a multi-line string.
            Defaults to False.

    Returns:
        A formatted string summarizing the features in the new layer.
    """
    # fmt: off
    # ruff: noqa: E501
    base_message: str = QCoreApplication.translate("summary", "Layer '{0}' analyzed:").format(selected_layer_name)  
    excel_summary: str = QCoreApplication.translate("summary", "(Summary saved to folder '{0}')").format(Names.excel_dir)  
    # fmt: on

    if new_layer.fields().indexFromName(NewLayerFields.type.name) == -1:
        log_debug("Type field not found in new layer.", Qgis.Warning)
        # fmt: off
        fail_field: str = QCoreApplication.translate("summary", "Type field not found in new layer.") 
        # fmt: on
        completed_message: str = (
            f"{base_message} ({LEVEL_ICON[Qgis.Warning]} {fail_field})"
        )
    else:
        type_counts: dict[str, int] = {}
        for feature in new_layer.getFeatures():  # pyright: ignore[reportGeneralTypeIssues]
            type_value = feature.attribute(NewLayerFields.type.name)
            if isinstance(type_value, str) and type_value:
                type_counts[type_value] = type_counts.get(type_value, 0) + 1

        if not type_counts:
            log_debug("Failed to get type counts from new layer.", Qgis.Warning)
            # fmt: off
            fail_counts: str = QCoreApplication.translate("summary", "Failed to get type counts from new layer.")  
            # fmt: on
            completed_message = (
                f"{base_message} ({LEVEL_ICON[Qgis.Warning]} {fail_counts})"
            )

        else:
            found_parts: list[str] = [
                f"{name}: {count}" for name, count in type_counts.items()
            ]
            if multiline:
                details: str = "\n- " + "\n- ".join(found_parts)
                completed_message = f"{base_message}{details} {excel_summary}"
            else:
                completed_message = (
                    f"{base_message} {' | '.join(found_parts)} {excel_summary}"
                )

    return completed_message
