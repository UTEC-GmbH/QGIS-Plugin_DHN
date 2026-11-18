"""Module: excel_exporter.py

This module contains the ExcelExporter class for exporting results.
"""

import copy
import os
import posixpath
import re
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsVariantUtils,
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


@dataclass(frozen=True)
class _SummaryData:
    pipe_runs: tuple[tuple[str, float], ...]
    fitting_counts: tuple[tuple[str, int], ...]
    detailed_fittings: tuple[tuple[str, int] | None, ...]
    questionable_points: tuple[str, int] | None = None


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

        summary_data: _SummaryData = self._create_summary_data(
            fittings_layer, pipe_layer
        )

        # --- Export point features (fittings) to plugin output file ---
        sheet_name: str = QCoreApplication.translate("XlsxExport", "Fittings")
        self._write_to_plugin_output_file(fittings_layer, output_path, sheet_name)

        # --- Export line features (pipe runs) to plugin output file ---
        sheet_name: str = QCoreApplication.translate("XlsxExport", "Pipe Runs")
        self._write_to_plugin_output_file(pipe_layer, output_path, sheet_name)

        self._write_summary_sheet(output_path, summary_data)

    def _create_summary_data(
        self, fittings_layer: QgsVectorLayer, pipe_layer: QgsVectorLayer
    ) -> _SummaryData:
        """Aggregate the three summary tables from the exported layers."""
        fitting_indices = {
            field: self._field_index(fittings_layer, field.field_name)
            for field in (
                NewPointLayerFields.type,
                NewPointLayerFields.dim_1,
                NewPointLayerFields.dim_2,
                NewPointLayerFields.angle,
            )
        }
        pipe_indices = {
            field: self._field_index(pipe_layer, field.field_name)
            for field in (NewLineLayerFields.dim, NewLineLayerFields.length)
        }

        fitting_counts: dict[tuple[str, str], int] = defaultdict(int)
        fitting_type_counts: dict[str, int] = defaultdict(int)
        questionable_type: str = FittingType.QUESTIONABLE.translated
        questionable_count: int = 0
        bend_type: str = FittingType.BEND.translated
        for feature in fittings_layer.getFeatures():
            fitting_type_value = self._attribute_value(
                feature, fitting_indices[NewPointLayerFields.type]
            )
            fitting_type: str = str(fitting_type_value or "")

            if fitting_type == questionable_type:
                questionable_count += 1
                continue

            fitting_type_counts[fitting_type] += 1

            dim_1 = self._attribute_value(
                feature, fitting_indices[NewPointLayerFields.dim_1]
            )
            dim_2 = self._attribute_value(
                feature, fitting_indices[NewPointLayerFields.dim_2]
            )
            angle = self._attribute_value(
                feature, fitting_indices[NewPointLayerFields.angle]
            )
            category: str = self._fitting_category(
                dim_1, dim_2, angle if fitting_type == bend_type else None
            )
            fitting_counts[(fitting_type, category)] += 1

        pipe_lengths: dict[int | None, float] = defaultdict(float)
        for feature in pipe_layer.getFeatures():
            diameter_value = self._attribute_value(
                feature, pipe_indices[NewLineLayerFields.dim]
            )
            diameter: int | None = (
                int(diameter_value) if diameter_value is not None else None
            )
            length_value = self._attribute_value(
                feature, pipe_indices[NewLineLayerFields.length]
            )
            if length_value is not None:
                pipe_lengths[diameter] += float(length_value)

        pipe_rows = tuple(
            (f"DN {diameter}" if diameter is not None else "", length)
            for diameter, length in sorted(
                pipe_lengths.items(),
                key=lambda item: (
                    item[0] is None,
                    item[0] if item[0] is not None else 0,
                ),
            )
        )
        fitting_rows = tuple(sorted(fitting_type_counts.items()))
        detailed_rows: list[tuple[str, int] | None] = []
        previous_fitting_type: str | None = None
        for (fitting_type, category), count in sorted(fitting_counts.items()):
            if (
                previous_fitting_type is not None
                and fitting_type != previous_fitting_type
            ):
                detailed_rows.append(None)
            label = f"{fitting_type} / {category}" if category else fitting_type
            detailed_rows.append((label, count))
            previous_fitting_type = fitting_type
        return _SummaryData(
            pipe_rows,
            fitting_rows,
            tuple(detailed_rows),
            (questionable_type, questionable_count) if questionable_count > 0 else None,
        )

    @staticmethod
    def _field_index(layer: QgsVectorLayer, field_name: str) -> int:
        """Return a required field index or report a clear export error."""
        index: int = layer.fields().indexFromName(field_name)
        if index < 0:
            raise_runtime_error(
                f"Required field '{field_name}' not found in layer '{layer.name()}'."
            )
        return index

    @staticmethod
    def _attribute_value(feature: QgsFeature, index: int) -> object | None:
        """Return a field value as Python data, normalizing Qt null variants."""
        value = feature.attribute(index)
        return None if QgsVariantUtils.isNull(value) else value

    @staticmethod
    def _fitting_category(dim_1: object, dim_2: object, angle: object) -> str:
        """Format dimensions and an optional bend-angle band for the summary."""
        category_parts: list[str] = [
            f"DN {int(dimension)}"
            for dimension in (dim_1, dim_2)
            if dimension is not None
        ]

        if angle is not None:
            angle_degrees: int = int(angle)
            upper_bound: int = max(45, ((angle_degrees + 44) // 45) * 45)
            lower_bound: int = max(0, upper_bound - 45)
            category_parts.append(f"{lower_bound}° - {upper_bound}°")

        return " / ".join(category_parts)

    def _write_summary_sheet(
        self, output_path: Path, summary_data: _SummaryData
    ) -> None:
        """Insert the three formatted summary tables into the workbook."""
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

                pipe_sheet_name: str = QCoreApplication.translate(
                    "XlsxExport", "Pipe Runs"
                )
                pipe_sheet_aliases = {
                    pipe_sheet_name,
                    "Pipe Runs",
                    "Leitungstrassen",
                }
                pipe_sheet = next(
                    (
                        sheet
                        for sheet in sheets_element
                        if sheet.get("name") in pipe_sheet_aliases
                    ),
                    None,
                )
                if pipe_sheet is None:
                    raise_runtime_error(
                        f"Could not locate the Excel sheet '{pipe_sheet_name}'."
                    )
                pipe_relationship_id = pipe_sheet.get(f"{{{_DOCUMENT_REL_NS}}}id")
                pipe_sheet_path = sheet_paths.get(pipe_relationship_id or "", "")
                if not pipe_sheet_path:
                    raise_runtime_error(
                        f"Could not locate the Excel sheet '{pipe_sheet_name}'."
                    )

                (
                    bold_style,
                    header_left_style,
                    header_right_style,
                    length_style,
                    bold_length_style,
                    amount_style,
                    bold_amount_style,
                ) = self._add_excel_styles(styles_root)
                pipe_sheet_root = ET.fromstring(source.read(pipe_sheet_path))
                self._apply_pipe_length_format(
                    pipe_sheet_root,
                    shared_strings,
                    NewLineLayerFields.length.field_name,
                    length_style,
                )

                replacements = {
                    "xl/workbook.xml": self._xml_bytes(workbook_root, _MAIN_NS),
                    "xl/_rels/workbook.xml.rels": self._xml_bytes(
                        relationships_root, _PACKAGE_REL_NS
                    ),
                    "[Content_Types].xml": self._xml_bytes(
                        content_types_root, _CONTENT_TYPES_NS
                    ),
                    "xl/styles.xml": self._xml_bytes(styles_root, _MAIN_NS),
                    summary_sheet_path: self._summary_worksheet_xml(
                        summary_data,
                        bold_style,
                        header_left_style,
                        header_right_style,
                        length_style,
                        bold_length_style,
                        amount_style,
                        bold_amount_style,
                    ),
                }

                for path in sheet_paths.values():
                    if path == summary_sheet_path:
                        continue
                    sheet_root = (
                        pipe_sheet_root
                        if path == pipe_sheet_path
                        else ET.fromstring(source.read(path))
                    )
                    self._hide_grid_lines(sheet_root)
                    replacements[path] = self._xml_bytes(sheet_root, _MAIN_NS)

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
    ) -> tuple[int, int, int, int, int, int, int]:
        """Add summary font and number formats and return their style IDs."""
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
            bold: bool,
            number_format: bool,
            horizontal: str | None = None,
        ) -> int:
            attributes = {
                "numFmtId": str(num_fmt_id),
                "fontId": str(font_id),
                "fillId": "0",
                "borderId": "0",
                "xfId": "0",
                "applyFont": "1" if bold else None,
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

        bold_style = find_or_add_style(
            cell_xfs, bold_font_id, 164, bold=True, number_format=False
        )
        header_left_style = find_or_add_style(
            cell_xfs,
            header_font_id,
            164,
            bold=True,
            number_format=False,
            horizontal="left",
        )
        header_right_style = find_or_add_style(
            cell_xfs,
            header_font_id,
            164,
            bold=True,
            number_format=False,
            horizontal="right",
        )
        length_style = find_or_add_style(
            cell_xfs, 0, format_id, bold=False, number_format=True
        )
        bold_length_style = find_or_add_style(
            cell_xfs, bold_font_id, format_id, bold=True, number_format=True
        )
        amount_style = find_or_add_style(cell_xfs, 0, 3, bold=False, number_format=True)
        bold_amount_style = find_or_add_style(
            cell_xfs, bold_font_id, 3, bold=True, number_format=True
        )
        return (
            bold_style,
            header_left_style,
            header_right_style,
            length_style,
            bold_length_style,
            amount_style,
            bold_amount_style,
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
    def _hide_grid_lines(cls, worksheet: ET.Element) -> None:
        """Ensure the worksheet view hides grid lines."""
        views = worksheet.find(cls._tag(_MAIN_NS, "sheetViews"))
        if views is None:
            worksheet_element_order = (
                "sheetPr",
                "dimension",
                "sheetViews",
                "sheetFormatPr",
                "cols",
                "sheetData",
            )
            insertion_index = next(
                (
                    index
                    for index, element in enumerate(worksheet)
                    if element.tag.rsplit("}", 1)[-1] in worksheet_element_order
                    and worksheet_element_order.index(element.tag.rsplit("}", 1)[-1])
                    > worksheet_element_order.index("sheetViews")
                ),
                len(worksheet),
            )
            views = ET.Element(cls._tag(_MAIN_NS, "sheetViews"))
            worksheet.insert(insertion_index, views)

        view = views.find(cls._tag(_MAIN_NS, "sheetView"))
        if view is None:
            view = ET.SubElement(
                views,
                cls._tag(_MAIN_NS, "sheetView"),
                {"workbookViewId": "0"},
            )
        view.set("showGridLines", "0")

    @classmethod
    def _summary_worksheet_xml(
        cls,
        summary_data: _SummaryData,
        bold_style: int,
        header_left_style: int,
        header_right_style: int,
        length_style: int,
        bold_length_style: int,
        amount_style: int,
        bold_amount_style: int,
    ) -> bytes:
        """Create the summary worksheet."""
        root = ET.Element(cls._tag(_MAIN_NS, "worksheet"))
        pipe_table_rows = len(summary_data.pipe_runs) + 2
        fittings_table_rows = len(summary_data.fitting_counts) + (
            4 if summary_data.questionable_points is not None else 2
        )
        detailed_table_rows = len(summary_data.detailed_fittings) + 2
        row_count = max(pipe_table_rows, fittings_table_rows, detailed_table_rows)
        last_row: int = 3 + row_count
        ET.SubElement(root, cls._tag(_MAIN_NS, "dimension"), {"ref": f"B3:I{last_row}"})
        views = ET.SubElement(root, cls._tag(_MAIN_NS, "sheetViews"))
        ET.SubElement(
            views,
            cls._tag(_MAIN_NS, "sheetView"),
            {"workbookViewId": "0", "showGridLines": "0"},
        )
        ET.SubElement(
            root,
            cls._tag(_MAIN_NS, "sheetFormatPr"),
            {"defaultRowHeight": "15"},
        )
        columns = ET.SubElement(root, cls._tag(_MAIN_NS, "cols"))
        widths = (15, 15, 15, 20, 17, 8, 8, 28, 8)
        for column, width in enumerate(widths, start=1):
            ET.SubElement(
                columns,
                cls._tag(_MAIN_NS, "col"),
                {
                    "min": str(column),
                    "max": str(column),
                    "width": str(width),
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

        total_label: str = QCoreApplication.translate("XlsxExport", "Total")
        for index in range(row_count):
            row_number: int = index + 4
            row = ET.SubElement(
                sheet_data,
                cls._tag(_MAIN_NS, "row"),
                {"r": str(row_number)},
            )
            cls._add_summary_cells(
                row,
                "B",
                "C",
                summary_data.pipe_runs,
                index,
                total_label,
                sum(length for _, length in summary_data.pipe_runs),
                bold_style,
                length_style,
                bold_length_style,
            )
            cls._add_summary_cells(
                row,
                "E",
                "F",
                summary_data.fitting_counts,
                index,
                total_label,
                sum(count for _, count in summary_data.fitting_counts),
                bold_style,
                amount_style,
                bold_amount_style,
                extra_item=summary_data.questionable_points,
            )
            cls._add_summary_cells(
                row,
                "H",
                "I",
                summary_data.detailed_fittings,
                index,
                total_label,
                sum(
                    item[1]
                    for item in summary_data.detailed_fittings
                    if item is not None
                ),
                bold_style,
                amount_style,
                bold_amount_style,
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

    @classmethod
    def _add_summary_cells(
        cls,
        row: ET.Element,
        label_column: str,
        value_column: str,
        values: tuple[tuple[str, float | int] | None, ...],
        index: int,
        total_label: str,
        total_value: float,
        bold_style: int,
        value_style: int | None = None,
        bold_value_style: int | None = None,
        extra_item: tuple[str, int] | None = None,
    ) -> None:
        row_number: str = row.get("r", "")
        if index < len(values):
            item = values[index]
            if item is not None:
                label, value = item
                cls._add_text_cell(row, f"{label_column}{row_number}", label)
                cls._add_number_cell(
                    row, f"{value_column}{row_number}", value, value_style
                )
        elif index == len(values) + 1:
            cls._add_text_cell(
                row, f"{label_column}{row_number}", total_label, bold_style
            )
            cls._add_number_cell(
                row,
                f"{value_column}{row_number}",
                total_value,
                bold_value_style or bold_style,
            )
        elif extra_item is not None and index == len(values) + 3:
            label, value = extra_item
            cls._add_text_cell(row, f"{label_column}{row_number}", label)
            cls._add_number_cell(row, f"{value_column}{row_number}", value, value_style)

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
    def _add_number_cell(
        row: ET.Element,
        reference: str,
        value: float,
        style_id: int | None = None,
    ) -> None:
        attributes = {"r": reference}
        if style_id is not None:
            attributes["s"] = str(style_id)
        cell = ET.SubElement(row, ExcelExporter._tag(_MAIN_NS, "c"), attributes)
        number = ET.SubElement(cell, ExcelExporter._tag(_MAIN_NS, "v"))
        number.text = str(value)

    @staticmethod
    def _tag(namespace: str, local_name: str) -> str:
        return f"{{{namespace}}}{local_name}"

    @staticmethod
    def _xml_bytes(root: ET.Element, default_namespace: str) -> bytes:
        ET.register_namespace("", default_namespace)
        ET.register_namespace("r", _DOCUMENT_REL_NS)
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
