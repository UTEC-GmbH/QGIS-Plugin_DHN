"""A script to automate the plugin release process.

This script provides a single source of truth for the plugin's version number,
reading it from metadata.txt and automatically updating the private repository's
plugins.xml file. It then compiles and packages the plugin.

To use:
1. Update the 'version' in metadata.txt.
2. Run this script from the OSGeo4W Shell: python release.py
"""

import configparser
import logging
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import TypedDict
from urllib.parse import ParseResult, unquote, urlparse
from urllib.request import url2pathname
from xml.etree.ElementTree import Element, ElementTree, SubElement

from defusedxml import ElementTree as DefET


# --- Logger ---
def setup_logging() -> None:
    """Configure the module's logger to print to the console."""
    handler: logging.StreamHandler[logging.TextIO | configparser.Any] = (
        logging.StreamHandler(sys.stdout)
    )
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


logger: logging.Logger = logging.getLogger(__name__)


class PluginMetadata(TypedDict):
    """A dictionary representing the plugin's metadata."""

    # [release] section
    plugin_package_name: str
    files_to_package: list[str]
    dirs_to_package: list[str]
    translation_dir: str
    excluded_dirs: list[str]
    excluded_extensions: list[str]

    # [general] section
    name: str
    version: str
    changelog: str
    description: str
    qgis_minimum_version: str
    author: str
    email: str
    url_base: str


class ReleaseScriptError(Exception):
    """Custom exception for errors during the release process."""


def get_plugin_metadata() -> PluginMetadata:
    """Read plugin metadata from the metadata.txt file.

    Returns:
        A dictionary containing the plugin's core metadata.

    Raises:
        ReleaseScriptError: If the metadata file is not found or is missing keys.
    """
    # The metadata filename is the one constant we need.
    metadata_path = Path("metadata.txt")
    if not metadata_path.exists():
        msg: str = f"Metadata file not found at '{metadata_path}'"
        raise ReleaseScriptError(msg)

    config = configparser.ConfigParser(interpolation=None)
    config.read(metadata_path, encoding="utf-8")
    try:
        metadata: PluginMetadata = {
            # [release] section
            "plugin_package_name": config.get("release", "plugin_package_name"),
            "files_to_package": config.get("release", "files_to_package").split(),
            "dirs_to_package": config.get("release", "dirs_to_package").split(),
            "translation_dir": config.get("release", "translation_dir"),
            "excluded_dirs": config.get("release", "excluded_dirs").split(),
            "excluded_extensions": config.get("release", "excluded_extensions").split(),
            # [general] section
            "name": config.get("general", "name"),
            "version": config.get("general", "version"),
            "changelog": config.get("general", "changelog"),
            "description": config.get("general", "description"),
            "qgis_minimum_version": config.get("general", "qgisMinimumVersion"),
            "author": config.get("general", "author"),
            "email": config.get("general", "email"),
            "url_base": config.get("general", "download_url_base"),
        }
    except configparser.NoSectionError as e:
        msg = f"Could not find required section '[{e.section}]' in {metadata_path}."
        logger.exception("❌ %s", msg)
        raise ReleaseScriptError(msg) from e
    except configparser.NoOptionError as e:
        msg = (
            f"Missing required key '{e.option}' in section '[{e.section}]' "
            f"in {metadata_path}."
        )
        logger.exception("❌ %s", msg)
        raise ReleaseScriptError(msg) from e

    logger.info(
        "✅ Found plugin '%s' version '%s' in %s",
        metadata["name"],
        metadata["version"],
        metadata_path,
    )
    return metadata


def _file_url_to_path(url: str) -> Path:
    """Convert a file URL to a local filesystem Path object.

    Args:
        url (str): The file URL to convert (must use the 'file://' scheme).

    Returns:
        Path: The corresponding local filesystem path.

    Raises:
        ReleaseScriptError: If the URL does not use the 'file://' scheme.
    """
    parsed: ParseResult = urlparse(url)
    if parsed.scheme != "file":
        msg = "`download_url_base` must use the file:// scheme."
        raise ReleaseScriptError(msg)
    path_part: str = url2pathname(unquote(parsed.path))
    if parsed.netloc:
        return Path(f"//{parsed.netloc}{path_part}")
    return Path(path_part)


"""

plugins.xml

"""


def _get_repository_path(metadata: PluginMetadata) -> Path:
    """Get repository path and ensure the directory exists.

    Args:
        metadata: The plugin's metadata.

    Returns:
        The master XML path.

    Raises:
        ReleaseScriptError: If the directory cannot be accessed or created.
    """
    url_base: str = metadata["url_base"]
    shared_repo_path: Path = _file_url_to_path(url_base)
    master_xml_path: Path = shared_repo_path / "plugins.xml"

    try:
        shared_repo_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg: str = f"Could not access or create shared repository directory: {e}"
        raise ReleaseScriptError(msg) from e

    return master_xml_path


def _load_or_create_xml_tree(xml_path: Path) -> tuple[ElementTree, Element]:
    """Load XML from a file or create a new tree if the file doesn't exist.

    Args:
        xml_path: The path to the plugins.xml file.

    Returns:
        A tuple containing the ElementTree and its root element.

    Raises:
        ReleaseScriptError: If the XML file is malformed.
    """
    if xml_path.exists():
        logger.info("Reading master repository file: %s", xml_path)
        try:
            tree: ElementTree = DefET.parse(xml_path)
            root: Element = tree.getroot()  # pyright: ignore[reportAssignmentType]
        except DefET.ParseError as e:
            msg: str = f"Error parsing {xml_path}."
            logger.exception("❌ %s", msg)
            raise ReleaseScriptError(msg) from e
        return tree, root

    logger.warning(
        "⚠️ Master repository file not found at '%s'. "
        "This is expected if it's the first plugin release.",
        xml_path,
    )
    logger.info("Creating a new XML structure in memory.")
    root = Element("plugins")
    tree = ElementTree(root)

    return tree, root


def _find_or_create_plugin_node(root: Element, plugin_name: str) -> Element:
    """Find an existing plugin node in the XML tree or create a new one.

    Args:
        root: The root element of the XML tree.
        plugin_name: The name of the plugin.

    Returns:
        The XML Element for the plugin.
    """
    plugin_node: Element | None = next(
        (
            node
            for node in root.findall("pyqgis_plugin")
            if node.get("name") == plugin_name
        ),
        None,
    )

    if plugin_node is None:
        logger.info("Plugin '%s' not found. Creating new entry.", plugin_name)
        plugin_node = SubElement(root, "pyqgis_plugin", name=plugin_name)
        # Pre-populate essential child tags so they can be found later
        for tag in [
            "version",
            "changelog",
            "description",
            "qgis_minimum_version",
            "author_name",
            "email",
            "file_name",
            "download_url",
        ]:
            SubElement(plugin_node, tag)
    else:
        logger.info("Found existing entry for '%s'. Updating...", plugin_name)

    return plugin_node


def _update_xml_tag(parent_node: Element, tag_name: str, value: str) -> None:
    """Find a child tag and update its text, creating it if it doesn't exist.

    Args:
        parent_node: The parent XML element.
        tag_name: The name of the tag to update or create.
        value: The text value to set.
    """
    tag: Element[str] | None = parent_node.find(tag_name)
    if tag is None:
        tag = SubElement(parent_node, tag_name)
    tag.text = value


def _update_plugin_node_details(plugin_node: Element, metadata: PluginMetadata) -> None:
    """Populate the plugin's XML node with details from metadata.

    Args:
        plugin_node: The XML element for the plugin.
        metadata: The plugin's metadata.
    """
    version: str = metadata["version"]
    plugin_name: str = metadata["name"]

    plugin_node.set("version", version)

    _update_xml_tag(plugin_node, "version", version)
    _update_xml_tag(plugin_node, "description", metadata["description"])
    _update_xml_tag(plugin_node, "changelog", metadata["changelog"])
    _update_xml_tag(
        plugin_node, "qgis_minimum_version", metadata["qgis_minimum_version"]
    )
    _update_xml_tag(plugin_node, "author_name", metadata["author"])
    _update_xml_tag(plugin_node, "email", metadata["email"])

    clean_plugin_name: str = plugin_name.replace(" ", "_")
    new_zip_filename: str = f"{clean_plugin_name}.zip"
    _update_xml_tag(plugin_node, "file_name", new_zip_filename)

    new_url: str = f"{metadata['url_base'].rstrip('/')}/{new_zip_filename}"
    _update_xml_tag(plugin_node, "download_url", new_url)


def _write_plugin_xml(tree: ElementTree, destination_path: Path) -> None:
    """Write the XML tree to a file atomically.

    Args:
        tree: The XML ElementTree to write.
        destination_path: The final path for the XML file.

    Raises:
        ReleaseScriptError: If the file cannot be written.
    """
    repo_path: Path = destination_path.parent
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(repo_path),
        prefix="plugins.xml.",
        suffix=".tmp",
    )
    os.close(tmp_fd)
    try:
        tree.write(tmp_name, encoding="utf-8", xml_declaration=True)
        Path(tmp_name).replace(destination_path)
    except OSError as e:
        msg: str = (
            f"Failed to write/update `{destination_path}`. "
            f"Check permissions on `{repo_path}`: {e}"
        )
        raise ReleaseScriptError(msg) from e
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def update_repository_file(metadata: PluginMetadata) -> None:
    # sourcery skip: extract-method
    """Update the master plugins.xml file directly in the shared repository.

    This function implements a fully automated, multi-plugin-safe workflow by
    orchestrating several helper functions.

    Args:
        metadata: A dictionary containing the plugin's core metadata.

    Raises:
        ReleaseScriptError: If any step in the process fails.
    """

    plugin_name: str = metadata["name"]
    version: str = metadata["version"]
    logger.info("Updating repository file for '%s' version %s...", plugin_name, version)

    try:
        # 1. Get path and ensure directory exists
        master_xml_path: Path = _get_repository_path(metadata)

        # 2. Load existing XML or create a new one
        tree, root = _load_or_create_xml_tree(master_xml_path)

        # 3. Find this plugin's node or create it
        plugin_node: Element[str] = _find_or_create_plugin_node(root, plugin_name)

        # 4. Populate the node with current metadata
        _update_plugin_node_details(plugin_node, metadata)

        # 5. Write the changes back safely
        _write_plugin_xml(tree, master_xml_path)

        logger.info("✅ Successfully updated repository file: %s", master_xml_path)

    except DefET.ParseError as e:
        msg = "Error processing repository XML."
        logger.exception("❌ %s", msg)
        raise ReleaseScriptError(msg) from e


"""

PACKAGING

"""


def _get_clean_metadata_content(plugin_name: str) -> str:
    """Create a clean metadata.txt content in memory for packaging.

    This ensures the released plugin doesn't contain any development markers
    like "(dev)" in its name.

    Args:
        plugin_name: The clean name of the plugin for the release.

    Returns:
        The content of the cleaned metadata.txt file as a string.
    """
    with Path("metadata.txt").open(encoding="utf-8") as f:
        original_content: str = f.read()

    lines: list[str] = original_content.splitlines(keepends=True)
    new_lines: list[str] = [
        f"name={plugin_name}\n" if line.strip().startswith("name=") else line
        for line in lines
    ]
    return "".join(new_lines)


def _add_files_to_zip(
    zipf: zipfile.ZipFile,
    files: list[str],
    plugin_zip_dir: str,
    clean_metadata_content: str,
) -> None:
    """Add individual files to the zip archive.

    Args:
        zipf: The ZipFile object.
        files: A list of file paths to add.
        plugin_zip_dir: The root directory name inside the zip file.
        clean_metadata_content: The cleaned content for metadata.txt.
    """
    for file_str in files:
        if file_str == "metadata.txt":
            arcname: str = (Path(plugin_zip_dir) / "metadata.txt").as_posix()
            zipf.writestr(arcname, clean_metadata_content.encode("utf-8"))
            logger.info("Writing cleaned metadata.txt to zip archive.")
            continue

        file_path = Path(file_str)
        if file_path.exists():
            arcname = (Path(plugin_zip_dir) / file_path).as_posix()
            zipf.write(file_path, arcname)
        else:
            logger.warning("⚠️ File '%s' not found, skipping.", file_path)


def _add_directories_to_zip(
    zipf: zipfile.ZipFile,
    dirs: list[str],
    plugin_zip_dir: str,
    excluded_dirs: list[str],
    excluded_extensions: list[str],
) -> None:
    """Recursively add directories to the zip archive.

    Args:
        zipf: The ZipFile object.
        dirs: A list of directory paths to add.
        plugin_zip_dir: The root directory name inside the zip file.
        excluded_dirs: A list of directory names to exclude.
        excluded_extensions: A list of file extensions to exclude.
    """
    for dir_str in dirs:
        dir_path = Path(dir_str)
        if not dir_path.is_dir():
            logger.warning("⚠️ Directory '%s' not found, skipping.", dir_path)
            continue

        for root, _, files in os.walk(dir_path):
            if any(excluded in root for excluded in excluded_dirs):
                continue
            for file in files:
                if any(file.endswith(ext) for ext in excluded_extensions):
                    continue
                file_path: Path = Path(root) / file
                arcname: str = (Path(plugin_zip_dir) / file_path).as_posix()
                zipf.write(file_path, arcname)


def package_plugin(metadata: PluginMetadata) -> None:
    # sourcery skip: extract-method
    """Create a zip archive of the plugin directly in the shared repository.

    This function orchestrates the packaging process by reading configuration,
    collecting files, and creating a zip archive in the shared repository.

    Args:
        metadata: The plugin's metadata, used to determine the output path
                  and zip file name.

    Raises:
        ReleaseScriptError: If the packaging process fails at any step.
    """
    plugin_name: str = metadata["name"]
    clean_plugin_name: str = plugin_name.replace(" ", "_")
    logger.info("\n▶️ Packaging '%s'...", plugin_name)

    # 1. Define packaging configuration
    plugin_zip_dir: str = metadata["plugin_package_name"]
    files_to_zip: list[str] = metadata["files_to_package"]
    dirs_to_zip: list[str] = metadata["dirs_to_package"]

    # Validate that the hardcoded name matches the metadata.
    if plugin_zip_dir != clean_plugin_name:
        msg: str = (
            f"Name mismatch: The hardcoded 'plugin_zip_dir' ('{plugin_zip_dir}') "
            f"must match the 'name' from 'metadata.txt' with spaces replaced "
            f"by underscores ('{clean_plugin_name}')."
        )
        raise ReleaseScriptError(msg)

    # 2. Prepare content and paths
    clean_metadata_content: str = _get_clean_metadata_content(plugin_name)
    shared_repo_path: Path = _file_url_to_path(metadata["url_base"])
    zip_path: Path = shared_repo_path / f"{clean_plugin_name}.zip"
    logger.info("Creating zip archive at: %s", zip_path)

    # 3. Create the zip archive
    with zipfile.ZipFile(
        zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zipf:
        _add_files_to_zip(
            zipf,
            files_to_zip,
            plugin_zip_dir,
            clean_metadata_content,
        )
        _add_directories_to_zip(
            zipf,
            dirs_to_zip,
            plugin_zip_dir,
            metadata["excluded_dirs"],
            metadata["excluded_extensions"],
        )

    logger.info(
        "✅ Successfully created plugin package in shared repository: %s", zip_path
    )


"""

RUN

"""


def run_command(command: list[str], *, shell: bool = False) -> None:
    """Run a command in a subprocess and checks for errors.

    Args:
        command: The command to run as a list of strings.
        shell: Whether to run the command in a shell. Defaults to False.
    """
    logger.info("\n▶️ Running command: %s", " ".join(command))
    try:
        env: dict[str, str] = os.environ.copy()

        python_bin_dir = str(Path(sys.executable).parent)
        if "PATH" in env:
            # os.pathsep is ';' on Windows and ':' on Linux/macOS
            if python_bin_dir not in env["PATH"].split(os.pathsep):
                env["PATH"] = f"{python_bin_dir}{os.pathsep}{env['PATH']}"
        else:
            env["PATH"] = python_bin_dir

        result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
            command,
            check=True,
            capture_output=True,
            text=True,
            shell=shell,
            env=env,
        )
        if result.stdout:
            logger.info(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        logger.exception("❌ Error running command: %s", " ".join(command))
        # Stderr is often the most useful part of a subprocess error
        if e.stderr:
            logger.exception("Stderr: %s", e.stderr.strip())
        msg: str = f"Command '{' '.join(command)}' failed."
        raise ReleaseScriptError(msg) from e


def compile_translations(metadata: PluginMetadata) -> None:
    """Find and compile Qt translation files (.ts to .qm).

    This function finds all .ts files in the 'i18n' directory and compiles them
    into binary .qm files using the 'lrelease' tool, which must be in the
    system's PATH (usually available in an OSGeo4W shell).

    Raises:
        ReleaseScriptError: If the 'i18n' directory doesn't exist or if the
                            'lrelease' command fails.
    """
    logger.info("\n▶️ Compiling translation files...")
    i18n_dir = Path(metadata["translation_dir"])
    if not i18n_dir.is_dir():
        logger.warning(
            "⚠️ Translation directory %s not found, skipping compilation.", i18n_dir
        )
        return

    ts_files: list[Path] = list(i18n_dir.glob("*.ts"))
    if not ts_files:
        logger.info(
            "No .ts files found in Translation directory %s directory.", i18n_dir
        )
        return

    for ts_file in ts_files:
        logger.info("Compiling %s...", ts_file)
        try:
            # The command is static, so shell=False is safer.
            run_command(["lrelease", str(ts_file)])
        except ReleaseScriptError as e:
            # Re-raise with a more specific message
            msg: str = f"Failed to compile '{ts_file}'. Is 'lrelease' in your PATH?"
            raise ReleaseScriptError(msg) from e


def run_release_process() -> None:
    """Automate the plugin release process.

    This main function orchestrates the entire release process:
    1. Reads metadata.
    2. Updates the repository XML directly on the shared drive.
    3. Compiles resources and translations.
    4. Packages the plugin into a zip file directly on the shared drive.

    Raises:
        ReleaseScriptError: If any step in the release process fails.
    """
    metadata: PluginMetadata = get_plugin_metadata()
    original_name: str = metadata["name"]
    release_name: str = original_name.replace("(dev)", "").strip()

    if not release_name:
        msg = "Plugin name cannot be empty after removing '(dev)' marker."
        raise ReleaseScriptError(msg)

    if original_name != release_name:
        logger.info(
            "Note: Development marker '(dev)' found. Releasing with clean name: '%s'",
            release_name,
        )
        metadata["name"] = release_name

    update_repository_file(metadata)

    compile_translations(metadata)
    package_plugin(metadata)

    logger.info("\n🎉 --- Release process complete! --- 🎉")
    shared_repo_path: Path = _file_url_to_path(metadata["url_base"])

    logger.info(
        "✅ Plugin successfully released directly to the shared repository: %s",
        shared_repo_path,
    )


def main() -> int:
    """CLI entry point. Sets up logging and runs the release process.

    Returns:
        An exit code: 0 for success, 1 for failure.
    """
    setup_logging()
    try:
        run_release_process()
    except ReleaseScriptError as e:
        logger.critical("❌ A critical error occurred: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
