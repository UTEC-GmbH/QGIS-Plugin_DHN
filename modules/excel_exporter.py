"""Module: excel_exporter.py

This module contains the ExcelExporter class for exporting results.
"""

import copy
import os
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from .constants import (
    FittingType,
    Names,
    NewLineLayerFields,
    NewPointLayerFields,
)
from .context import PluginContext
from .logs_and_errors import log_debug, lvl, raise_runtime_error

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_DYNAMIC_ARRAY_NS = (
    "http://schemas.microsoft.com/office/spreadsheetml/2017/dynamicarray"
)


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

        self._write_summary_sheet(
            output_path,
            tuple(field.name() for field in fittings_layer.fields()),
            tuple(field.name() for field in pipe_layer.fields()),
        )

    def _write_summary_sheet(
        self,
        output_path: Path,
        fittings_headers: tuple[str, ...],
        pipe_headers: tuple[str, ...],
    ) -> None:
        """Insert formula-driven summary tables into the workbook."""
        temp_path: Path | None = None
        try:
            with zipfile.ZipFile(output_path, "r") as source:
                names: set[str] = set(source.namelist())
                workbook_root = ET.fromstring(source.read("xl/workbook.xml"))
                relationships_root = ET.fromstring(
                    source.read("xl/_rels/workbook.xml.rels")
                )
                content_types_root = ET.fromstring(source.read("[Content_Types].xml"))
                styles_root = ET.fromstring(source.read("xl/styles.xml"))
                shared_strings = (
                    self._read_shared_strings(source)
                    if "xl/sharedStrings.xml" in names
                    else []
                )

                sheets_element = workbook_root.find(self._tag(_MAIN_NS, "sheets"))
                if sheets_element is None:
                    raise_runtime_error("The Excel workbook has no sheets section.")
                sheet_paths = self._worksheet_paths(sheets_element, relationships_root)

                fittings_sheet_name: str = QCoreApplication.translate(
                    "XlsxExport", "Fittings"
                )
                fittings_sheet = next(
                    (
                        sheet
                        for sheet in sheets_element
                        if sheet.get("name") == fittings_sheet_name
                    ),
                    None,
                )
                pipe_sheet_name: str = QCoreApplication.translate(
                    "XlsxExport", "Pipe Runs"
                )
                pipe_sheet = next(
                    (
                        sheet
                        for sheet in sheets_element
                        if sheet.get("name") == pipe_sheet_name
                    ),
                    None,
                )
                if fittings_sheet is None or pipe_sheet is None:
                    raise_runtime_error(
                        "Could not locate the Excel fittings or pipe-run sheet."
                    )

                fittings_relationship_id = fittings_sheet.get(
                    f"{{{_DOCUMENT_REL_NS}}}id"
                )
                fittings_sheet_path = sheet_paths.get(
                    fittings_relationship_id or "", ""
                )
                pipe_relationship_id = pipe_sheet.get(f"{{{_DOCUMENT_REL_NS}}}id")
                pipe_sheet_path = sheet_paths.get(pipe_relationship_id or "", "")
                if not fittings_sheet_path or not pipe_sheet_path:
                    raise_runtime_error(
                        "Could not locate the Excel fittings or pipe-run sheet."
                    )

                summary_name: str = QCoreApplication.translate("XlsxExport", "Summary")
                summary_aliases = {
                    summary_name,
                    "Summary",
                    "Zusammenfassung",
                }
                summary_sheet = next(
                    (
                        sheet
                        for sheet in sheets_element
                        if sheet.get("name") in summary_aliases
                    ),
                    None,
                )

                additions: dict[str, bytes] = {}
                if summary_sheet is None:
                    summary_sheet, summary_sheet_path = self._add_summary_sheet(
                        sheets_element,
                        relationships_root,
                        content_types_root,
                        sheet_paths,
                        summary_name,
                    )
                    additions[summary_sheet_path] = b""
                else:
                    relationship_id = summary_sheet.get(f"{{{_DOCUMENT_REL_NS}}}id")
                    summary_sheet_path = sheet_paths.get(relationship_id or "", "")
                    if not summary_sheet_path:
                        raise_runtime_error(
                            "Could not locate the existing Excel summary sheet."
                        )
                    summary_sheet.set("name", summary_name)
                    sheets_element.remove(summary_sheet)
                    sheets_element.insert(0, summary_sheet)

                (
                    header_left_style,
                    header_right_style,
                    length_style,
                    amount_style,
                    total_dxf_id,
                    separator_dxf_id,
                ) = self._add_excel_styles(styles_root)
                fittings_sheet_root = ET.fromstring(source.read(fittings_sheet_path))
                pipe_sheet_root = ET.fromstring(source.read(pipe_sheet_path))
                self._ensure_worksheet_headers(fittings_sheet_root, fittings_headers)
                self._ensure_worksheet_headers(pipe_sheet_root, pipe_headers)
                self._apply_pipe_length_format(
                    pipe_sheet_root,
                    shared_strings,
                    NewLineLayerFields.length.field_name,
                    length_style,
                )

                fittings_columns = self._worksheet_columns(
                    fittings_sheet_root, shared_strings
                )
                pipe_columns = self._worksheet_columns(pipe_sheet_root, shared_strings)
                removed_parts = {
                    name
                    for name in names
                    if re.fullmatch(r"xl/tables/table\d+\.xml", name)
                }
                worksheet_relationship_replacements: dict[str, bytes] = {}
                for worksheet_root, worksheet_path in (
                    (fittings_sheet_root, fittings_sheet_path),
                    (pipe_sheet_root, pipe_sheet_path),
                ):
                    for table_parts in worksheet_root.findall(
                        self._tag(_MAIN_NS, "tableParts")
                    ):
                        worksheet_root.remove(table_parts)
                    relationship_path = self._worksheet_relationships_path(
                        worksheet_path
                    )
                    if relationship_path in names:
                        worksheet_relationships = ET.fromstring(
                            source.read(relationship_path)
                        )
                        for relationship in list(
                            worksheet_relationships.findall(
                                self._tag(_PACKAGE_REL_NS, "Relationship")
                            )
                        ):
                            if not relationship.get("Type", "").endswith("/table"):
                                continue
                            target = relationship.get("Target")
                            if target is not None:
                                removed_parts.add(
                                    posixpath.normpath(
                                        posixpath.join(
                                            posixpath.dirname(worksheet_path), target
                                        )
                                    )
                                )
                            worksheet_relationships.remove(relationship)
                        if len(worksheet_relationships):
                            worksheet_relationship_replacements[relationship_path] = (
                                self._xml_bytes(
                                    worksheet_relationships, _PACKAGE_REL_NS
                                )
                            )
                        else:
                            removed_parts.add(relationship_path)

                formulas = self._summary_formulas(
                    fittings_columns,
                    pipe_columns,
                    fittings_sheet_name,
                    pipe_sheet_name,
                )
                self._add_dynamic_array_metadata(
                    workbook_root,
                    relationships_root,
                    content_types_root,
                )
                for content_type in list(
                    content_types_root.findall(self._tag(_CONTENT_TYPES_NS, "Override"))
                ):
                    if content_type.get("PartName", "").startswith("/xl/tables/"):
                        content_types_root.remove(content_type)

                replacements = {
                    "xl/workbook.xml": self._xml_bytes(workbook_root, _MAIN_NS),
                    "xl/_rels/workbook.xml.rels": self._xml_bytes(
                        relationships_root, _PACKAGE_REL_NS
                    ),
                    "[Content_Types].xml": self._xml_bytes(
                        content_types_root, _CONTENT_TYPES_NS
                    ),
                    "xl/styles.xml": self._xml_bytes(styles_root, _MAIN_NS),
                    fittings_sheet_path: self._xml_bytes(fittings_sheet_root, _MAIN_NS),
                    pipe_sheet_path: self._xml_bytes(pipe_sheet_root, _MAIN_NS),
                    summary_sheet_path: self._summary_worksheet_xml(
                        formulas,
                        header_left_style,
                        header_right_style,
                        length_style,
                        amount_style,
                        total_dxf_id,
                        separator_dxf_id,
                    ),
                }
                replacements.update(worksheet_relationship_replacements)
                metadata_path = "xl/metadata.xml"
                metadata_bytes = self._dynamic_array_metadata_xml()
                if metadata_path in names:
                    replacements[metadata_path] = metadata_bytes
                else:
                    additions[metadata_path] = metadata_bytes
                if summary_sheet_path in additions:
                    additions[summary_sheet_path] = replacements.pop(summary_sheet_path)

                with tempfile.NamedTemporaryFile(
                    dir=output_path.parent,
                    prefix=f"{output_path.stem}_",
                    suffix=".xlsx",
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)

                with zipfile.ZipFile(
                    temp_path, "w", compression=zipfile.ZIP_DEFLATED
                ) as destination:
                    destination.comment = source.comment
                    for item in source.infolist():
                        if item.filename in removed_parts:
                            continue
                        data = replacements.pop(item.filename, None)
                        if data is None:
                            data = source.read(item.filename)
                        destination.writestr(item, data)
                    for name, data in additions.items():
                        destination.writestr(
                            name, data, compress_type=zipfile.ZIP_DEFLATED
                        )

            os.replace(temp_path, output_path)
            temp_path = None
        except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as error:
            raise_runtime_error(f"Could not format Excel workbook: {error}")
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _worksheet_paths(
        sheets_element: ET.Element, relationships_root: ET.Element
    ) -> dict[str, str]:
        """Map workbook sheet relationship IDs to their worksheet paths."""
        relationships = {
            relationship.get("Id"): relationship.get("Target")
            for relationship in relationships_root.findall(
                ExcelExporter._tag(_PACKAGE_REL_NS, "Relationship")
            )
        }
        paths: dict[str, str] = {}
        for sheet in sheets_element:
            relationship_id = sheet.get(f"{{{_DOCUMENT_REL_NS}}}id")
            target = relationships.get(relationship_id)
            if relationship_id is None or target is None:
                continue
            path = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target))
            )
            paths[relationship_id] = path
        return paths

    @staticmethod
    def _worksheet_relationships_path(worksheet_path: str) -> str:
        directory, filename = posixpath.split(worksheet_path)
        return posixpath.join(directory, "_rels", f"{filename}.rels")

    @staticmethod
    def _worksheet_columns(
        worksheet: ET.Element, shared_strings: list[str]
    ) -> tuple[str, ...]:
        """Return the raw worksheet headers."""
        sheet_data = worksheet.find(ExcelExporter._tag(_MAIN_NS, "sheetData"))
        if sheet_data is None:
            raise_runtime_error("An Excel data sheet has no cell data.")

        header_row = next((row for row in sheet_data if row.get("r") == "1"), None)
        if header_row is None:
            raise_runtime_error("An Excel data sheet has no header row.")

        headers: list[str] = []
        for cell in header_row.findall(ExcelExporter._tag(_MAIN_NS, "c")):
            header = ExcelExporter._cell_text(cell, shared_strings)
            match = re.fullmatch(r"([A-Z]+)1", cell.get("r", ""))
            if not header or match is None:
                raise_runtime_error("An Excel data sheet has an invalid column header.")
            headers.append(header)

        if not headers or len(set(headers)) != len(headers):
            raise_runtime_error("Excel data sheet column headers must be unique.")
        return tuple(headers)

    @staticmethod
    def _ensure_worksheet_headers(
        worksheet: ET.Element, headers: tuple[str, ...]
    ) -> None:
        """Restore field headers omitted by the XLSX driver for empty layers."""
        sheet_data = worksheet.find(ExcelExporter._tag(_MAIN_NS, "sheetData"))
        if sheet_data is None:
            raise_runtime_error("An Excel data sheet has no cell data.")

        if any(row.get("r") == "1" for row in sheet_data):
            return
        if len(sheet_data) > 0:
            raise_runtime_error("An Excel data sheet has no header row.")
        if not headers or len(set(headers)) != len(headers):
            raise_runtime_error("Excel data sheet column headers must be unique.")

        header_row = ET.Element(ExcelExporter._tag(_MAIN_NS, "row"), {"r": "1"})
        for index, header in enumerate(headers, start=1):
            cell = ET.SubElement(
                header_row,
                ExcelExporter._tag(_MAIN_NS, "c"),
                {
                    "r": f"{ExcelExporter._excel_column_name(index)}1",
                    "t": "inlineStr",
                },
            )
            inline_string = ET.SubElement(cell, ExcelExporter._tag(_MAIN_NS, "is"))
            text = ET.SubElement(inline_string, ExcelExporter._tag(_MAIN_NS, "t"))
            text.text = header
        sheet_data.append(header_row)

        dimension = worksheet.find(ExcelExporter._tag(_MAIN_NS, "dimension"))
        if dimension is None:
            dimension = ET.Element(
                ExcelExporter._tag(_MAIN_NS, "dimension"),
                {"ref": f"A1:{ExcelExporter._excel_column_name(len(headers))}1"},
            )
            sheet_views = worksheet.find(ExcelExporter._tag(_MAIN_NS, "sheetViews"))
            worksheet.insert(
                list(worksheet).index(sheet_views) if sheet_views is not None else 0,
                dimension,
            )
        else:
            dimension.set(
                "ref", f"A1:{ExcelExporter._excel_column_name(len(headers))}1"
            )

    @staticmethod
    def _excel_column_name(index: int) -> str:
        """Convert a one-based column index to its Excel column name."""
        column_name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            column_name = chr(ord("A") + remainder) + column_name
        return column_name

    @staticmethod
    def _excel_string(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    @staticmethod
    def _prepare_dynamic_formula(formula: str) -> str:
        """Prefix dynamic-array functions as required by the XLSX format."""
        functions = {
            "CHOOSECOLS": "_xlfn.CHOOSECOLS",
            "FILTER": "_xlfn._xlws.FILTER",
            "HSTACK": "_xlfn.HSTACK",
            "SORT": "_xlfn._xlws.SORT",
            "UNIQUE": "_xlfn.UNIQUE",
            "VSTACK": "_xlfn.VSTACK",
        }
        for function, prefix in functions.items():
            formula = re.sub(rf"\b{function}\(", f"{prefix}(", formula)
        return formula

    @classmethod
    def _summary_formulas(
        cls,
        fitting_columns: tuple[str, ...],
        pipe_columns: tuple[str, ...],
        fittings_sheet_name: str,
        pipe_sheet_name: str,
    ) -> dict[str, str]:
        """Build dynamic formulas for the three summary tables."""
        fittings_sheet = cls._excel_sheet_reference(fittings_sheet_name)
        pipe_sheet = cls._excel_sheet_reference(pipe_sheet_name)
        fitting_type = cls._worksheet_column_reference(
            fittings_sheet, fitting_columns, NewPointLayerFields.type.field_name
        )
        fitting_dim_1 = cls._worksheet_column_reference(
            fittings_sheet, fitting_columns, NewPointLayerFields.dim_1.field_name
        )
        fitting_dim_2 = cls._worksheet_column_reference(
            fittings_sheet, fitting_columns, NewPointLayerFields.dim_2.field_name
        )
        fitting_angle = cls._worksheet_column_reference(
            fittings_sheet, fitting_columns, NewPointLayerFields.angle.field_name
        )
        pipe_diameter = cls._worksheet_column_reference(
            pipe_sheet, pipe_columns, NewLineLayerFields.dim.field_name
        )
        pipe_length = cls._worksheet_column_reference(
            pipe_sheet, pipe_columns, NewLineLayerFields.length.field_name
        )
        questionable = cls._excel_string(FittingType.QUESTIONABLE.translated)
        bend = cls._excel_string(FittingType.BEND.translated)
        total = cls._excel_string(QCoreApplication.translate("XlsxExport", "Total"))
        diameter_prefix = cls._excel_string(f"{Names.dim_prefix} ")
        valid_fittings = f'({fitting_type}<>"")*({fitting_type}<>{questionable})'

        pipe_diameters = f'SORT(UNIQUE(FILTER({pipe_diameter},{pipe_length}<>"")))'
        pipe_formula = (
            f'=IF(COUNTIF({pipe_length},"<>")=0,HSTACK({total},0),'
            f'VSTACK(HSTACK({diameter_prefix}&TEXT({pipe_diameters},"0"),'
            f"SUMIF({pipe_diameter},{pipe_diameters},{pipe_length})),"
            f"HSTACK({total},SUM({pipe_length}))))"
        )

        fitting_types = f'SORT(UNIQUE(FILTER({fitting_type},{fitting_type}<>"")))'
        fittings_formula = (
            f'=IF(COUNTIF({fitting_type},"<>")=0,HSTACK({total},0),'
            f"VSTACK(HSTACK({fitting_types},COUNTIF({fitting_type},{fitting_types})),"
            f'HSTACK({total},COUNTIF({fitting_type},"<>"))))'
        )

        filtered_types = f"FILTER({fitting_type},{valid_fittings})"
        filtered_dim_1 = (
            f'FILTER(IF({fitting_dim_1}=0,"",{fitting_dim_1}),{valid_fittings})'
        )
        filtered_dim_2 = (
            f'FILTER(IF({fitting_dim_2}=0,"",{fitting_dim_2}),{valid_fittings})'
        )
        filtered_upper = (
            f"FILTER(IF(({fitting_type}={bend})*ISNUMBER({fitting_angle}),"
            f"IF(CEILING({fitting_angle},45)<45,45,"
            f'CEILING({fitting_angle},45)),""),{valid_fittings})'
        )
        filtered_lower = (
            f'IF({filtered_upper}<>"",'
            f'IF({filtered_upper}-45<0,0,{filtered_upper}-45),"")'
        )
        detailed_sort_key = (
            f'{filtered_types}&"|"&TEXT({filtered_lower},"00000")&"|"&'
            f'TEXT({filtered_upper},"00000")&"|"&TEXT({filtered_dim_1},"00000")&'
            f'"|"&TEXT({filtered_dim_2},"00000")'
        )
        detailed_keys = (
            f"SORT(UNIQUE(HSTACK({detailed_sort_key},{filtered_types},"
            f"{filtered_dim_1},{filtered_dim_2},{filtered_lower},{filtered_upper})))"
        )
        detail_last_row = f"MAX(4,COUNTA({fittings_sheet}!$A:$A)+3)"
        details_key_reference = (
            f"FILTER($X$4:INDEX($AC:$AC,{detail_last_row}),"
            f'$X$4:INDEX($X:$X,{detail_last_row})<>"")'
        )
        detail_type = f"CHOOSECOLS({details_key_reference},2)"
        detail_dim_1 = f"CHOOSECOLS({details_key_reference},3)"
        detail_dim_2 = f"CHOOSECOLS({details_key_reference},4)"
        detail_lower = f"CHOOSECOLS({details_key_reference},5)"
        detail_upper = f"CHOOSECOLS({details_key_reference},6)"
        detail_labels = (
            f'{detail_type}&IF({detail_dim_1}<>""," / DN "&'
            f'TEXT({detail_dim_1},"0"),"")&IF({detail_dim_2}<>"",'
            f'" / DN "&TEXT({detail_dim_2},"0"),"")&IF({detail_upper}<>'
            f'""," / "&TEXT({detail_lower},"0")&"° - "&'
            f'TEXT({detail_upper},"0")&"°","")'
        )
        detail_counts = (
            f'IF({detail_type}={bend},IF({detail_upper}<>"",'
            f"COUNTIFS({fitting_type},{detail_type},{fitting_dim_1},{detail_dim_1},"
            f'{fitting_dim_2},{detail_dim_2},{fitting_angle},">="&{detail_lower},'
            f'{fitting_angle},"<="&{detail_upper}),'
            f"COUNTIFS({fitting_type},{detail_type},{fitting_dim_1},{detail_dim_1},"
            f'{fitting_dim_2},{detail_dim_2},{fitting_angle},"")),'
            f"COUNTIFS({fitting_type},{detail_type},{fitting_dim_1},{detail_dim_1},"
            f"{fitting_dim_2},{detail_dim_2}))"
        )
        fitting_total_count = (
            f'COUNTIFS({fitting_type},"<>",{fitting_type},"<>"&{questionable})'
        )
        detailed_formula = (
            f"=IF({fitting_total_count}=0,HSTACK({total},0),"
            f"VSTACK(HSTACK({detail_labels},{detail_counts}),"
            f"HSTACK({total},SUM({detail_counts}))))"
        )
        detailed_keys_formula = (
            f'=IF({fitting_total_count}=0,HSTACK("","","","","",""),{detailed_keys})'
        )
        return {
            "pipe": cls._prepare_dynamic_formula(pipe_formula),
            "fittings": cls._prepare_dynamic_formula(fittings_formula),
            "detailed": cls._prepare_dynamic_formula(detailed_formula),
            "detailed_keys": cls._prepare_dynamic_formula(detailed_keys_formula),
        }

    @classmethod
    def _worksheet_column_reference(
        cls, sheet_name: str, columns: tuple[str, ...], field_name: str
    ) -> str:
        """Create a column range that expands as the worksheet gains rows."""
        if field_name not in columns:
            raise_runtime_error(
                f"Could not find the '{field_name}' column in the Excel sheet."
            )
        column_index = columns.index(field_name) + 1
        column_name = cls._excel_column_name(column_index)
        key_column = cls._excel_column_name(columns.index(columns[0]) + 1)
        return (
            f"{sheet_name}!${column_name}$2:"
            f"INDEX({sheet_name}!${column_name}:${column_name},"
            f"MAX(2,COUNTA({sheet_name}!${key_column}:${key_column})))"
        )

    @staticmethod
    def _excel_sheet_reference(sheet_name: str) -> str:
        """Quote a worksheet name for use in formulas."""
        return "'" + sheet_name.replace("'", "''") + "'"

    @staticmethod
    def _add_dynamic_array_metadata(
        workbook_root: ET.Element,
        relationships_root: ET.Element,
        content_types_root: ET.Element,
    ) -> None:
        """Register dynamic-array metadata and request recalculation in Excel."""
        relationship = next(
            (
                item
                for item in relationships_root.findall(
                    ExcelExporter._tag(_PACKAGE_REL_NS, "Relationship")
                )
                if item.get("Type", "").endswith("/sheetMetadata")
            ),
            None,
        )
        if relationship is None:
            relationship_ids = [
                int(match.group(1))
                for item in relationships_root.findall(
                    ExcelExporter._tag(_PACKAGE_REL_NS, "Relationship")
                )
                if (match := re.fullmatch(r"rId(\d+)", item.get("Id", "")))
            ]
            relationship = ET.SubElement(
                relationships_root,
                ExcelExporter._tag(_PACKAGE_REL_NS, "Relationship"),
                {
                    "Id": f"rId{max(relationship_ids, default=0) + 1}",
                    "Type": (
                        "http://schemas.openxmlformats.org/officeDocument/2006/"
                        "relationships/sheetMetadata"
                    ),
                    "Target": "metadata.xml",
                },
            )
        else:
            relationship.set("Target", "metadata.xml")

        metadata_part = "/xl/metadata.xml"
        metadata_override = next(
            (
                item
                for item in content_types_root.findall(
                    ExcelExporter._tag(_CONTENT_TYPES_NS, "Override")
                )
                if item.get("PartName") == metadata_part
            ),
            None,
        )
        if metadata_override is None:
            ET.SubElement(
                content_types_root,
                ExcelExporter._tag(_CONTENT_TYPES_NS, "Override"),
                {
                    "PartName": metadata_part,
                    "ContentType": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheetMetadata+xml"
                    ),
                },
            )

        calculation = workbook_root.find(ExcelExporter._tag(_MAIN_NS, "calcPr"))
        if calculation is None:
            calculation = ET.Element(ExcelExporter._tag(_MAIN_NS, "calcPr"))
            extension_list = workbook_root.find(ExcelExporter._tag(_MAIN_NS, "extLst"))
            workbook_root.insert(
                list(workbook_root).index(extension_list)
                if extension_list is not None
                else len(workbook_root),
                calculation,
            )
        calculation.set("calcMode", "auto")
        calculation.set("fullCalcOnLoad", "1")
        calculation.set("forceFullCalc", "1")

    @staticmethod
    def _dynamic_array_metadata_xml() -> bytes:
        """Build the metadata required for dynamic-array formula cells."""
        root = ET.Element(ExcelExporter._tag(_MAIN_NS, "metadata"))
        types = ET.SubElement(
            root,
            ExcelExporter._tag(_MAIN_NS, "metadataTypes"),
            {"count": "1"},
        )
        ET.SubElement(
            types,
            ExcelExporter._tag(_MAIN_NS, "metadataType"),
            {
                "name": "XLDAPR",
                "minSupportedVersion": "120000",
                "copy": "1",
                "pasteAll": "1",
                "pasteValues": "1",
                "merge": "1",
                "splitFirst": "1",
                "rowColShift": "1",
                "clearFormats": "1",
                "clearComments": "1",
                "assign": "1",
                "coerce": "1",
                "cellMeta": "1",
            },
        )
        future_metadata = ET.SubElement(
            root,
            ExcelExporter._tag(_MAIN_NS, "futureMetadata"),
            {"name": "XLDAPR", "count": "1"},
        )
        bookmark = ET.SubElement(future_metadata, ExcelExporter._tag(_MAIN_NS, "bk"))
        extension_list = ET.SubElement(bookmark, ExcelExporter._tag(_MAIN_NS, "extLst"))
        extension = ET.SubElement(
            extension_list,
            ExcelExporter._tag(_MAIN_NS, "ext"),
            {"uri": "{bdbb8cdc-fa1e-496e-a857-3c3f30c029c3}"},
        )
        ET.SubElement(
            extension,
            ExcelExporter._tag(_DYNAMIC_ARRAY_NS, "dynamicArrayProperties"),
            {"fDynamic": "1", "fCollapsed": "0"},
        )
        cell_metadata = ET.SubElement(
            root,
            ExcelExporter._tag(_MAIN_NS, "cellMetadata"),
            {"count": "1"},
        )
        cell_bookmark = ET.SubElement(cell_metadata, ExcelExporter._tag(_MAIN_NS, "bk"))
        ET.SubElement(
            cell_bookmark,
            ExcelExporter._tag(_MAIN_NS, "rc"),
            {"t": "1", "v": "0"},
        )
        return ExcelExporter._xml_bytes(root, _MAIN_NS)

    @staticmethod
    def _add_summary_sheet(
        sheets_element: ET.Element,
        relationships_root: ET.Element,
        content_types_root: ET.Element,
        sheet_paths: dict[str, str],
        sheet_name: str,
    ) -> tuple[ET.Element, str]:
        """Register a new summary worksheet as the workbook's first sheet."""
        sheet_numbers = [
            int(match.group(1))
            for path in sheet_paths.values()
            if (match := re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", path))
        ]
        sheet_number: int = max(sheet_numbers, default=0) + 1
        sheet_path: str = f"xl/worksheets/sheet{sheet_number}.xml"

        relationship_ids = [
            int(match.group(1))
            for relation in relationships_root.findall(
                ExcelExporter._tag(_PACKAGE_REL_NS, "Relationship")
            )
            if (match := re.fullmatch(r"rId(\d+)", relation.get("Id", "")))
        ]
        relationship_id: str = f"rId{max(relationship_ids, default=0) + 1}"
        ET.SubElement(
            relationships_root,
            ExcelExporter._tag(_PACKAGE_REL_NS, "Relationship"),
            {
                "Id": relationship_id,
                "Type": f"{_DOCUMENT_REL_NS}/worksheet",
                "Target": f"worksheets/sheet{sheet_number}.xml",
            },
        )

        sheet_ids = [int(sheet.get("sheetId", "0")) for sheet in sheets_element]
        sheet = ET.Element(
            ExcelExporter._tag(_MAIN_NS, "sheet"),
            {
                "name": sheet_name,
                "sheetId": str(max(sheet_ids, default=0) + 1),
                "state": "visible",
                f"{{{_DOCUMENT_REL_NS}}}id": relationship_id,
            },
        )
        sheets_element.insert(0, sheet)

        part_name = f"/{sheet_path}"
        if not any(
            item.get("PartName") == part_name
            for item in content_types_root.findall(
                ExcelExporter._tag(_CONTENT_TYPES_NS, "Override")
            )
        ):
            ET.SubElement(
                content_types_root,
                ExcelExporter._tag(_CONTENT_TYPES_NS, "Override"),
                {
                    "PartName": part_name,
                    "ContentType": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.worksheet+xml"
                    ),
                },
            )
        return sheet, sheet_path

    @staticmethod
    def _read_shared_strings(source: zipfile.ZipFile) -> list[str]:
        """Load the workbook shared-string table."""
        root = ET.fromstring(source.read("xl/sharedStrings.xml"))
        return [
            "".join(
                text.text or "" for text in item.iter(ExcelExporter._tag(_MAIN_NS, "t"))
            )
            for item in root.findall(ExcelExporter._tag(_MAIN_NS, "si"))
        ]

    @staticmethod
    def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str | None:
        """Read a shared-string or inline-string cell."""
        value = cell.find(ExcelExporter._tag(_MAIN_NS, "v"))
        if cell.get("t") == "s" and value is not None and value.text is not None:
            return shared_strings[int(value.text)]
        if cell.get("t") == "inlineStr":
            return "".join(
                text.text or "" for text in cell.iter(ExcelExporter._tag(_MAIN_NS, "t"))
            )
        return None

    @staticmethod
    def _add_excel_styles(
        styles_root: ET.Element,
    ) -> tuple[int, int, int, int, int, int]:
        """Add summary styles and return header, number, and total style IDs."""
        num_fmts = styles_root.find(ExcelExporter._tag(_MAIN_NS, "numFmts"))
        if num_fmts is None:
            num_fmts = ET.Element(ExcelExporter._tag(_MAIN_NS, "numFmts"))
            styles_root.insert(0, num_fmts)
        num_format = next(
            (
                element
                for element in num_fmts.findall(ExcelExporter._tag(_MAIN_NS, "numFmt"))
                if element.get("formatCode") == '#,##0.0 "m"'
            ),
            None,
        )
        if num_format is None:
            used_format_ids = [
                int(element.get("numFmtId", "0"))
                for element in num_fmts.findall(ExcelExporter._tag(_MAIN_NS, "numFmt"))
            ]
            format_id: int = max(163, *used_format_ids) + 1
            num_format = ET.SubElement(
                num_fmts,
                ExcelExporter._tag(_MAIN_NS, "numFmt"),
                {"numFmtId": str(format_id), "formatCode": '#,##0.0 "m"'},
            )
        else:
            format_id = int(num_format.get("numFmtId", "0"))
        num_fmts.set("count", str(len(num_fmts)))

        fonts = styles_root.find(ExcelExporter._tag(_MAIN_NS, "fonts"))
        cell_xfs = styles_root.find(ExcelExporter._tag(_MAIN_NS, "cellXfs"))
        if fonts is None:
            raise_runtime_error("The Excel workbook has incomplete style definitions.")
        if cell_xfs is None:
            raise_runtime_error("The Excel workbook has incomplete style definitions.")
        if len(fonts) == 0:
            raise_runtime_error("The Excel workbook has incomplete style definitions.")

        def add_font_flag(font: ET.Element, flag: str) -> None:
            flag_tag = ExcelExporter._tag(_MAIN_NS, flag)
            if font.find(flag_tag) is not None:
                return

            font_element_order = (
                "name",
                "charset",
                "family",
                "b",
                "i",
                "strike",
                "outline",
                "shadow",
                "condense",
                "extend",
                "color",
                "sz",
                "u",
                "vertAlign",
                "scheme",
            )
            flag_order = font_element_order.index(flag)
            insertion_index = next(
                (
                    index
                    for index, element in enumerate(font)
                    if element.tag.rsplit("}", 1)[-1] in font_element_order
                    and font_element_order.index(element.tag.rsplit("}", 1)[-1])
                    > flag_order
                ),
                len(font),
            )
            font.insert(insertion_index, ET.Element(flag_tag))

        bold_font_id = next(
            (
                index
                for index, font in enumerate(fonts)
                if font.find(ExcelExporter._tag(_MAIN_NS, "b")) is not None
                and font.find(ExcelExporter._tag(_MAIN_NS, "u")) is None
            ),
            None,
        )
        if bold_font_id is None:
            bold_font = copy.deepcopy(fonts[0])
            add_font_flag(bold_font, "b")
            bold_font_id = len(fonts)
            fonts.append(bold_font)
            fonts.set("count", str(len(fonts)))

        header_font_id = next(
            (
                index
                for index, font in enumerate(fonts)
                if font.find(ExcelExporter._tag(_MAIN_NS, "b")) is not None
                and font.find(ExcelExporter._tag(_MAIN_NS, "u")) is not None
            ),
            None,
        )
        if header_font_id is None:
            header_font = copy.deepcopy(fonts[bold_font_id])
            add_font_flag(header_font, "u")
            header_font_id = len(fonts)
            fonts.append(header_font)
            fonts.set("count", str(len(fonts)))

        def find_or_add_style(
            cell_styles: ET.Element,
            font_id: int,
            num_fmt_id: int,
            *,
            number_format: bool,
            horizontal: str | None = None,
        ) -> int:
            attributes = {
                "numFmtId": str(num_fmt_id),
                "fontId": str(font_id),
                "fillId": "0",
                "borderId": "0",
                "xfId": "0",
                "applyNumberFormat": "1" if number_format else None,
                "applyAlignment": "1" if horizontal is not None else None,
            }
            for index, style in enumerate(cell_styles):
                alignment = style.find(ExcelExporter._tag(_MAIN_NS, "alignment"))
                alignment_value = (
                    alignment.get("horizontal") if alignment is not None else None
                )
                if (
                    all(
                        style.get(key) == value
                        for key, value in attributes.items()
                        if value is not None
                    )
                    and alignment_value == horizontal
                ):
                    return index
            style = {
                key: value for key, value in attributes.items() if value is not None
            }
            style_id = len(cell_styles)
            cell_style = ET.SubElement(
                cell_styles, ExcelExporter._tag(_MAIN_NS, "xf"), style
            )
            if horizontal is not None:
                ET.SubElement(
                    cell_style,
                    ExcelExporter._tag(_MAIN_NS, "alignment"),
                    {"horizontal": horizontal},
                )
            cell_styles.set("count", str(len(cell_styles)))
            return style_id

        header_left_style = find_or_add_style(
            cell_xfs,
            header_font_id,
            164,
            number_format=False,
            horizontal="left",
        )
        header_right_style = find_or_add_style(
            cell_xfs,
            header_font_id,
            164,
            number_format=False,
            horizontal="right",
        )
        length_style = find_or_add_style(
            cell_xfs,
            0,
            format_id,
            number_format=True,
            horizontal="right",
        )
        amount_style = find_or_add_style(
            cell_xfs, 0, 3, number_format=True, horizontal="right"
        )

        differential_styles = styles_root.find(ExcelExporter._tag(_MAIN_NS, "dxfs"))
        if differential_styles is None:
            differential_styles = ET.Element(ExcelExporter._tag(_MAIN_NS, "dxfs"))
            insert_before = next(
                (
                    index
                    for index, element in enumerate(styles_root)
                    if element.tag
                    in {
                        ExcelExporter._tag(_MAIN_NS, "tableStyles"),
                        ExcelExporter._tag(_MAIN_NS, "colors"),
                        ExcelExporter._tag(_MAIN_NS, "extLst"),
                    }
                ),
                len(styles_root),
            )
            styles_root.insert(insert_before, differential_styles)

        total_dxf_id = next(
            (
                index
                for index, differential_style in enumerate(differential_styles)
                if (
                    (
                        font := differential_style.find(
                            ExcelExporter._tag(_MAIN_NS, "font")
                        )
                    )
                    is not None
                    and font.find(ExcelExporter._tag(_MAIN_NS, "b")) is not None
                )
            ),
            None,
        )
        if total_dxf_id is None:
            differential_style = ET.SubElement(
                differential_styles, ExcelExporter._tag(_MAIN_NS, "dxf")
            )
            font = ET.SubElement(
                differential_style, ExcelExporter._tag(_MAIN_NS, "font")
            )
            ET.SubElement(font, ExcelExporter._tag(_MAIN_NS, "b"))
            total_dxf_id = len(differential_styles) - 1

        separator_dxf_id = next(
            (
                index
                for index, differential_style in enumerate(differential_styles)
                if (
                    (
                        border := differential_style.find(
                            ExcelExporter._tag(_MAIN_NS, "border")
                        )
                    )
                    is not None
                    and len(differential_style) == 1
                    and len(border) == 1
                    and (bottom := border.find(ExcelExporter._tag(_MAIN_NS, "bottom")))
                    is not None
                    and bottom.get("style") == "thin"
                )
            ),
            None,
        )
        if separator_dxf_id is None:
            differential_style = ET.SubElement(
                differential_styles, ExcelExporter._tag(_MAIN_NS, "dxf")
            )
            border = ET.SubElement(
                differential_style, ExcelExporter._tag(_MAIN_NS, "border")
            )
            bottom = ET.SubElement(
                border,
                ExcelExporter._tag(_MAIN_NS, "bottom"),
                {"style": "thin"},
            )
            ET.SubElement(
                bottom,
                ExcelExporter._tag(_MAIN_NS, "color"),
                {"auto": "1"},
            )
            separator_dxf_id = len(differential_styles) - 1
        differential_styles.set("count", str(len(differential_styles)))

        return (
            header_left_style,
            header_right_style,
            length_style,
            amount_style,
            total_dxf_id,
            separator_dxf_id,
        )

    @staticmethod
    def _apply_pipe_length_format(
        worksheet: ET.Element,
        shared_strings: list[str],
        length_field_name: str,
        style_id: int,
    ) -> None:
        """Apply the metre number format to numeric route-length cells."""
        sheet_data = worksheet.find(ExcelExporter._tag(_MAIN_NS, "sheetData"))
        if sheet_data is None:
            raise_runtime_error("The Excel pipe-run sheet has no cell data.")

        header_row = next((row for row in sheet_data if row.get("r") == "1"), None)
        if header_row is None:
            raise_runtime_error("The Excel pipe-run sheet has no header row.")
        length_column: str | None = None
        for cell in header_row.findall(ExcelExporter._tag(_MAIN_NS, "c")):
            if ExcelExporter._cell_text(cell, shared_strings) == length_field_name:
                match = re.match(r"[A-Z]+", cell.get("r", ""))
                if match is not None:
                    length_column = match.group(0)
                    break
        if length_column is None:
            raise_runtime_error(
                f"Could not find the '{length_field_name}' "
                "column in the pipe-run sheet."
            )

        for row in sheet_data:
            if row.get("r") == "1":
                continue
            for cell in row.findall(ExcelExporter._tag(_MAIN_NS, "c")):
                cell_column = re.match(r"[A-Z]+", cell.get("r", ""))
                if cell_column is None or cell_column.group(0) != length_column:
                    continue
                if cell.get("t") in {"s", "inlineStr", "str"}:
                    continue
                if cell.find(ExcelExporter._tag(_MAIN_NS, "v")) is not None:
                    cell.set("s", str(style_id))

    @classmethod
    def _summary_worksheet_xml(
        cls,
        formulas: dict[str, str],
        header_left_style: int,
        header_right_style: int,
        length_style: int,
        amount_style: int,
        total_dxf_id: int,
        separator_dxf_id: int,
    ) -> bytes:
        """Create the formula-driven summary sheet at the requested positions."""
        root = ET.Element(cls._tag(_MAIN_NS, "worksheet"))
        ET.SubElement(root, cls._tag(_MAIN_NS, "dimension"), {"ref": "B3:AC4"})
        views = ET.SubElement(root, cls._tag(_MAIN_NS, "sheetViews"))
        ET.SubElement(views, cls._tag(_MAIN_NS, "sheetView"), {"workbookViewId": "0"})
        ET.SubElement(
            root,
            cls._tag(_MAIN_NS, "sheetFormatPr"),
            {"defaultRowHeight": "15"},
        )
        columns = ET.SubElement(root, cls._tag(_MAIN_NS, "cols"))
        widths = (15, 20, 18, 15, 24, 12, 7, 26, 12)
        for column, width in enumerate(widths, start=1):
            attributes = {
                "min": str(column),
                "max": str(column),
                "width": str(width),
                "customWidth": "1",
            }
            if column == 3:
                attributes["style"] = str(length_style)
            elif column in {6, 9}:
                attributes["style"] = str(amount_style)
            ET.SubElement(columns, cls._tag(_MAIN_NS, "col"), attributes)
        ET.SubElement(
            columns,
            cls._tag(_MAIN_NS, "col"),
            {
                "min": "24",
                "max": "29",
                "width": "0",
                "hidden": "1",
                "customWidth": "1",
            },
        )

        sheet_data = ET.SubElement(root, cls._tag(_MAIN_NS, "sheetData"))
        header = ET.SubElement(sheet_data, cls._tag(_MAIN_NS, "row"), {"r": "3"})
        for column, label in (
            ("B", QCoreApplication.translate("XlsxExport", "Pipe Runs")),
            ("C", QCoreApplication.translate("XlsxExport", "Pipe run length")),
            ("E", QCoreApplication.translate("XlsxExport", "Fittings")),
            ("F", QCoreApplication.translate("XlsxExport", "Amount")),
            ("H", QCoreApplication.translate("XlsxExport", "Fittings")),
            ("I", QCoreApplication.translate("XlsxExport", "Amount")),
        ):
            style = (
                header_right_style if column in {"C", "F", "I"} else header_left_style
            )
            cls._add_text_cell(header, f"{column}3", label, style)

        total_label = QCoreApplication.translate("XlsxExport", "Total")
        formula_row = ET.SubElement(sheet_data, cls._tag(_MAIN_NS, "row"), {"r": "4"})
        for column, formula in (
            ("B", formulas["pipe"]),
            ("E", formulas["fittings"]),
            ("H", formulas["detailed"]),
            ("X", formulas["detailed_keys"]),
        ):
            cls._add_dynamic_array_formula_cell(formula_row, f"{column}4", formula)

        for label_column, value_column, priority in (
            ("B", "C", 1),
            ("E", "F", 2),
            ("H", "I", 3),
        ):
            conditional_formatting = ET.SubElement(
                root,
                cls._tag(_MAIN_NS, "conditionalFormatting"),
                {"sqref": f"{label_column}4:{value_column}1048576"},
            )
            rule = ET.SubElement(
                conditional_formatting,
                cls._tag(_MAIN_NS, "cfRule"),
                {
                    "type": "expression",
                    "dxfId": str(total_dxf_id),
                    "priority": str(priority),
                },
            )
            formula = ET.SubElement(rule, cls._tag(_MAIN_NS, "formula"))
            formula.text = f"${label_column}4={cls._excel_string(total_label)}"

        separator_formatting = ET.SubElement(
            root,
            cls._tag(_MAIN_NS, "conditionalFormatting"),
            {"sqref": "H4:I1048575"},
        )
        separator_rule = ET.SubElement(
            separator_formatting,
            cls._tag(_MAIN_NS, "cfRule"),
            {
                "type": "expression",
                "dxfId": str(separator_dxf_id),
                "priority": "4",
            },
        )
        separator_formula = ET.SubElement(separator_rule, cls._tag(_MAIN_NS, "formula"))
        separator_formula.text = (
            f'AND($H4<>"",$H4<>{cls._excel_string(total_label)},'
            f'$H5<>"",$H5<>{cls._excel_string(total_label)},'
            'IFERROR(LEFT($H4,FIND(" / ",$H4&" / ")-1),$H4)'
            '<>IFERROR(LEFT($H5,FIND(" / ",$H5&" / ")-1),$H5))'
        )

        ET.SubElement(
            root,
            cls._tag(_MAIN_NS, "pageMargins"),
            {
                "left": "0.7",
                "right": "0.7",
                "top": "0.75",
                "bottom": "0.75",
                "header": "0.3",
                "footer": "0.3",
            },
        )
        return cls._xml_bytes(root, _MAIN_NS)

    @staticmethod
    def _add_dynamic_array_formula_cell(
        row: ET.Element, reference: str, formula: str
    ) -> None:
        cell = ET.SubElement(
            row,
            ExcelExporter._tag(_MAIN_NS, "c"),
            {"r": reference, "cm": "1"},
        )
        formula_element = ET.SubElement(
            cell,
            ExcelExporter._tag(_MAIN_NS, "f"),
            {"t": "array", "ref": reference},
        )
        formula_element.text = formula.lstrip("=")
        ET.SubElement(cell, ExcelExporter._tag(_MAIN_NS, "v")).text = "0"

    @staticmethod
    def _add_text_cell(
        row: ET.Element, reference: str, value: str, style_id: int | None = None
    ) -> None:
        attributes = {"r": reference, "t": "inlineStr"}
        if style_id is not None:
            attributes["s"] = str(style_id)
        cell = ET.SubElement(row, ExcelExporter._tag(_MAIN_NS, "c"), attributes)
        inline_string = ET.SubElement(cell, ExcelExporter._tag(_MAIN_NS, "is"))
        text = ET.SubElement(inline_string, ExcelExporter._tag(_MAIN_NS, "t"))
        text.text = value

    @staticmethod
    def _tag(namespace: str, local_name: str) -> str:
        return f"{{{namespace}}}{local_name}"

    @staticmethod
    def _xml_bytes(root: ET.Element, default_namespace: str) -> bytes:
        ET.register_namespace("", default_namespace)
        ET.register_namespace("r", _DOCUMENT_REL_NS)
        ET.register_namespace("xda", _DYNAMIC_ARRAY_NS)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _write_to_plugin_output_file(
        self, layer: QgsVectorLayer, output_path: Path, sheet_name: str
    ) -> None:
        """Write a vector layer to an Excel file using QgsVectorFileWriter.

        Args:
            layer: The layer to write.
            output_path: The path to the output file.
            sheet_name: The name of the sheet in the Excel file.
        """

        log_debug(f"Exporting Excel sheet '{sheet_name}'...")
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
            log_debug(f"Excel file saved to \n{output_path}", lvl.Success)
        else:
            raise_runtime_error(
                f"Could not write to file \n{output_path}\n({write_layer[1]})"
            )
