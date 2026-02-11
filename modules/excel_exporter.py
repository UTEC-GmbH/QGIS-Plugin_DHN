"""Module: excel_exporter.py

This module contains the ExcelExporter class for exporting results.
"""

import shutil
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsCoordinateTransformContext,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from .constants import Names
from .context import PluginContext
from .logs_and_errors import log_debug, raise_runtime_error


class ExcelExporter:
    """A class to handle exporting analysis results to Excel."""

    def export_results(
        self, fittings_layer: QgsVectorLayer, pipe_layer: QgsVectorLayer
    ) -> None:
        """Export the analysis results to an XLSX file.

        Args:
            fittings_layer: The layer containing the point features (fittings).
            pipe_layer: The layer containing the line features (pipe runs).
        """
        # --- Prepare output directory ---
        output_dir: Path = PluginContext.project_path().parent / Names.excel_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        # --- Prepare plugin output file ---
        layer_name: str = fittings_layer.name().removesuffix(
            Names.new_fittings_layer_suffix
        )
        output_file_name: str = f"{Names.excel_file_output} - {layer_name}.xlsx"
        output_path: Path = output_dir / output_file_name

        # --- Export point features (fittings) to plugin output file ---
        sheet_name: str = QCoreApplication.translate("XlsxExport", "Fittings")
        self._write_to_plugin_output_file(fittings_layer, output_path, sheet_name)

        # --- Export line features (pipe runs) to plugin output file ---
        sheet_name: str = QCoreApplication.translate("XlsxExport", "Pipe Runs")
        self._write_to_plugin_output_file(pipe_layer, output_path, sheet_name)

        # --- Copy summary template file ---
        try:
            self._copy_summary_file(layer_name, output_dir)
        except OSError as e:
            raise_runtime_error(f"Could not copy template: {e}")

    def _copy_summary_file(self, layer_name: str, output_dir: Path) -> None:
        """Create the output file and copy the template file.

        Args:
            layer_name: The name of the layer being exported.
            output_dir: The directory where the output file should be created.
        """
        template_name: str = Names.excel_file_template
        template_path = Path(template_name)
        dest_file_name: str = (
            f"{Names.excel_file_summary} - {layer_name}{template_path.suffix}"
        )

        template_src: Path = PluginContext.templates_path() / template_name
        dest_file: Path = output_dir / dest_file_name

        if not template_src.exists():
            raise_runtime_error(f"Template file not found at: {template_src}")
        elif not dest_file.exists():
            shutil.copy(template_src, dest_file)
            log_debug(f"Copied summary template to: {dest_file}")
        else:
            log_debug(f"Summary template already exists at: {dest_file}")

    def _write_to_plugin_output_file(
        self, layer: QgsVectorLayer, output_path: Path, sheet_name: str
    ) -> None:
        """Write a vector layer to an Excel file using QgsVectorFileWriter.

        Args:
            layer: The layer to write.
            output_path: The path to the output file.
            sheet_name: The name of the sheet in the Excel file.
        """

        log_debug(f"Exporting features to plugin output file (sheet: {sheet_name})...")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "XLSX"
        options.datasourceOptions = ["GEOMETRY=NO"]
        options.layerName = sheet_name

        if output_path.exists():
            options.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            )
        else:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
            )

        write_layer: tuple = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, str(output_path), QgsCoordinateTransformContext(), options
        )

        if write_layer[0] == QgsVectorFileWriter.WriterError.NoError:
            log_debug(f"Excel file saved to \n{output_path}", Qgis.Success)
        else:
            raise_runtime_error(
                f"Could not write to file \n{output_path}\n({write_layer[1]})"
            )
