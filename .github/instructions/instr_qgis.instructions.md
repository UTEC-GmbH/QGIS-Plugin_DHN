---
description: Guidance for developing and maintaining Python plugins for QGIS.
applyTo: "**/*.py"
---

# QGIS plugin development

- Follow the plugin's existing architecture and conventions. Before changing behavior, inspect the related plugin entry points, modules, tests, and `metadata.txt`; keep changes focused.
- Preserve the plugin's declared QGIS and Python compatibility. Check the supported versions before using newer Python, QGIS, Qt, or PyQt APIs, and use existing compatibility helpers where available.
- Import QGIS APIs from `qgis.core` and `qgis.gui`, and Qt APIs through `qgis.PyQt`. Do not mix direct `PyQt5`/`PyQt6` imports into plugin code.
- Use `QgsProject.instance()` and the provided `QgisInterface` for the active project and application UI. Avoid relying on the current working directory; resolve plugin resources relative to the plugin directory.
- Keep plugin lifecycle code safe to run repeatedly: create actions and connections in `initGui`, and remove actions, disconnect signals, and release owned resources in `unload`.
- Validate layers, providers, geometries, coordinate reference systems, and coordinate transforms before using them. Handle missing or invalid data explicitly and report actionable errors through QGIS logging or the message bar rather than silently ignoring failures. Use the plugin's existing logging and error-handling conventions, and avoid using `print` statements for user-facing messages.
- Keep all UI work on the Qt main thread. Use QGIS task APIs for long-running work, and do not access GUI objects from worker threads.
- Keep business logic separate from widgets and QGIS-dependent code where practical so it can be tested without launching QGIS. Add focused tests for changed behavior; use the repository's QGIS-enabled test setup when a test needs PyQGIS or Qt.
- Keep packaging inputs in sync with plugin code: update metadata, resources, and release configuration when the change requires it. Do not add development-only files to plugin packages.
- Match the repository's formatting, typing, logging, and error-handling conventions. Run the narrowest relevant tests and checks available in the plugin's configured environment.
- For translatable UI text, use `QCoreApplication.translate("Context", "Source text")`.
- Keep static source strings on a single line. Use `# fmt: off` and `# ruff: noqa: E501` only when a single long string would otherwise be wrapped or violate formatting rules, and restore formatting with `# fmt: on`.
- Pass only static text as the translation source string. Construct dynamic output after translation:
  `QCoreApplication.translate("Context", "Found {} items").format(count)`
