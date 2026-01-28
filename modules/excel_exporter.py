"""Module: excel_exporter.py

This module contains the ExcelExporter class for exporting results.
"""

import shutil
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsVectorDataProvider,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from .constants import Names, QMT_Double, QMT_Int
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
        layer_name: str = fittings_layer.name().removesuffix(Names.new_layer_suffix)
        output_file_name: str = f"{Names.excel_file_output} - {layer_name}.xlsx"
        output_path: Path = output_dir / output_file_name

        # --- Export point features (fittings) to plugin output file ---
        sheet_name: str = QCoreApplication.translate("XlsxExport", "Fittings")
        self._write_to_plugin_output_file(fittings_layer, output_path, sheet_name)

        # --- Export line features (pipe runs) to plugin output file ---
        temporary_table: QgsVectorLayer | None = self._create_line_table(pipe_layer)
        if not temporary_table:
            return
        try:
            sheet_name: str = QCoreApplication.translate("XlsxExport", "Pipe Runs")
            self._write_to_plugin_output_file(temporary_table, output_path, sheet_name)
        finally:
            # Ensure the temporary table is removed from the project registry
            # Note: Since we created it in memory and didn't add it to the project
            # explicitly in this class, we just let it go out of scope, but if it
            # was added to the project, we should remove it.
            pass

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
        template_name: str = f"{Names.excel_file_summary}.xlsx"
        template_path = Path(template_name)
        dest_file_name: str = (
            f"{template_path.stem} - {layer_name}{template_path.suffix}"
        )

        template_src: Path = PluginContext.templates_path() / template_name
        template_dest: Path = output_dir / dest_file_name

        if not template_src.exists():
            raise_runtime_error(f"Template file not found at: {template_src}")
        elif not template_dest.exists():
            shutil.copy(template_src, template_dest)
            log_debug(f"Copied summary template to: {template_dest}")
        else:
            log_debug(f"Summary template already exists at: {template_dest}")

    def _create_line_table(self, source_layer: QgsVectorLayer) -> QgsVectorLayer | None:
        """Create and populate a temporary table with line feature data.

        Args:
            source_layer: The source layer containing line features.

        Returns:
            A temporary QgsVectorLayer with line data, or None if no features found.
        """

        log_debug("Creating temporary (in-memory) layer for line features...")

        temporary_table = QgsVectorLayer("None?crs=", "line_features_data", "memory")
        line_data_provider: QgsVectorDataProvider | None = (
            temporary_table.dataProvider()
        )
        if not line_data_provider:
            raise_runtime_error(
                "Could not create data provider for line features layer."
            )

        line_fields: list[QgsField] = [
            QgsField("ID", QMT_Int),
            QgsField(Names.excel_dim, QMT_Int),
            QgsField(Names.excel_line_length, QMT_Double),
        ]
        line_data_provider.addAttributes(line_fields)
        temporary_table.updateFields()

        dim_field_name: str | None = next(
            (
                name
                for name in Names.sel_layer_field_dim
                if source_layer.fields().lookupField(name) != -1
            ),
            None,
        )

        features_for_excel: list[QgsFeature] = []
        for original_feature in source_layer.getFeatures():
            new_excel_feature = QgsFeature(temporary_table.fields())
            new_excel_feature.setAttribute(
                "ID", original_feature.attribute("original_fid")
            )
            geom = original_feature.geometry()
            length: float = geom.length() if geom and not geom.isEmpty() else 0.0
            new_excel_feature.setAttribute(Names.excel_line_length, length)

            dim_value = (
                original_feature.attribute(dim_field_name) if dim_field_name else None
            )
            new_excel_feature.setAttribute(
                Names.excel_dim, dim_value if isinstance(dim_value, int) else None
            )
            features_for_excel.append(new_excel_feature)

        if not features_for_excel:
            log_debug("No line features to export to Excel.", Qgis.Warning)
            return None

        temporary_table.startEditing()
        temporary_table.addFeatures(features_for_excel)
        if not temporary_table.commitChanges():
            raise_runtime_error("Failed to commit line features to temporary layer.")

        log_debug(f"Prepared {len(features_for_excel)} line features for Excel export.")
        return temporary_table

    def _write_to_plugin_output_file(
        self, layer: QgsVectorLayer, output_path: Path, sheet_name: str
    ) -> None:
        """Write a vector layer to an Excel file using QgsVectorFileWriter.

        Args:
            layer: The layer to write.
            output_path: The path to the output file.
            options: The writer options.
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
