"""A script to automate the plugin release process.

This script reads the plugin version from metadata.txt, compiles translations,
packages the plugin, and updates the separate GitHub Pages release repository.

To use:
1. Update the 'version' in metadata.txt.
2. Run this script from the OSGeo4W Shell. With no arguments, an interactive
   menu lets you choose the release mode. Command-line options are also
   available: --output-dir, --commit, and --push.
"""

import argparse
import configparser
import logging
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import TypedDict
from xml.etree.ElementTree import Element, ElementTree, SubElement

try:
    # pyrefly: ignore[untyped-import]
    from defusedxml import ElementTree as DefET
except ImportError:
    print("\nError: The 'defusedxml' library is missing.")
    print("Please install it by running: \npython -m pip install defusedxml\n")
    sys.exit(1)


# --- Logger ---
def setup_logging() -> None:
    """Configure the module's logger to print to the console."""
    handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)
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
    qgis_maximum_version: str
    author: str
    email: str
    download_url_base: str


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
            "qgis_maximum_version": config.get("general", "qgisMaximumVersion"),
            "author": config.get("general", "author"),
            "email": config.get("general", "email"),
            "download_url_base": config.get("general", "download_url_base"),
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


"""

plugins.xml

"""


def _get_repository_path(output_dir: Path) -> Path:
    """Get the local repository path and ensure the directory exists.

    Args:
        output_dir: The local directory where repository files are published.

    Returns:
        The master XML path.

    Raises:
        ReleaseScriptError: If the directory cannot be accessed or created.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg: str = f"Could not access or create repository output directory: {e}"
        raise ReleaseScriptError(msg) from e

    return output_dir / "plugins.xml"


def _load_or_create_xml_tree(xml_path: Path) -> tuple[ElementTree, Element]:
    """Load XML from a file or create a new tree if the file doesn't exist.

    Args:
        xml_path: The path to the plugins.xml file.

    Returns:
        A tuple containing the ElementTree and its root element.

    Raises:
        ReleaseScriptError: If the XML file is malformed.
    """
    if xml_path.exists() and xml_path.stat().st_size > 0:
        logger.info("Reading master repository file: %s", xml_path)
        try:
            tree: ElementTree = DefET.parse(xml_path)
            # pyrefly: ignore[bad-assignment]
            root: Element = tree.getroot()
        except DefET.ParseError as e:
            msg: str = f"Error parsing {xml_path}."
            logger.exception("❌ %s", msg)
            raise ReleaseScriptError(msg) from e
        return tree, root

    if xml_path.exists():
        logger.info("Creating initial XML structure for empty repository file.")
    else:
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
            "qgis_maximum_version",
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
        tag_name: The name of the child tag to find or create.
        value: The text value to set for the tag.
    """
    tag: Element | None = parent_node.find(tag_name)
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
    _update_xml_tag(
        plugin_node, "qgis_maximum_version", metadata["qgis_maximum_version"]
    )
    _update_xml_tag(plugin_node, "author_name", metadata["author"])
    _update_xml_tag(plugin_node, "email", metadata["email"])

    clean_plugin_name: str = plugin_name.replace(" ", "_")
    new_zip_filename: str = f"{clean_plugin_name}.zip"
    _update_xml_tag(plugin_node, "file_name", new_zip_filename)

    new_url: str = f"{metadata['download_url_base'].rstrip('/')}/{new_zip_filename}"
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


def update_repository_file(metadata: PluginMetadata, output_dir: Path) -> None:
    """Update the master plugins.xml file in the local repository clone.

    This function implements a fully automated, multi-plugin-safe workflow by
    orchestrating several helper functions.

    Args:
        metadata: A dictionary containing the plugin's core metadata.
        output_dir: The local directory where repository files are published.

    Raises:
        ReleaseScriptError: If any step in the process fails.
    """

    plugin_name: str = metadata["name"]
    version: str = metadata["version"]
    logger.info("Updating repository file for '%s' version %s...", plugin_name, version)

    try:
        # 1. Get path and ensure directory exists
        master_xml_path: Path = _get_repository_path(output_dir)

        # 2. Load existing XML or create a new one
        tree, root = _load_or_create_xml_tree(master_xml_path)

        # 3. Find this plugin's node or create it
        plugin_node: Element = _find_or_create_plugin_node(root, plugin_name)

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
    new_lines: list[str] = []
    for line in lines:
        if "=" in line and line.split("=", 1)[0].strip() == "name":
            new_lines.append(f"name = {plugin_name}\n")
        else:
            new_lines.append(line)
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
                path_to_archive: Path = file_path

                # If walking the main package dir, make path relative to avoid
                # double-nesting (e.g., MyPlugin/MyPlugin/init.py).
                if dir_path.name == plugin_zip_dir:
                    path_to_archive = file_path.relative_to(dir_path)

                arcname: str = (Path(plugin_zip_dir) / path_to_archive).as_posix()
                zipf.write(file_path, arcname)


def package_plugin(metadata: PluginMetadata, output_dir: Path) -> None:
    # sourcery skip: extract-method
    """Create a zip archive of the plugin in the release repository directory.

    This function orchestrates packaging by collecting files and creating a zip
    archive in the local repository directory.

    Args:
        metadata: The plugin's metadata, used to determine the zip file name.
        output_dir: The local directory where the package is written.

    Raises:
        ReleaseScriptError: If the packaging process fails at any step.
    """
    plugin_name: str = metadata["name"]
    clean_plugin_name: str = plugin_name.replace(" ", "_")
    logger.info("\n▶️ Packaging '%s'...", plugin_name)

    # 1. Define packaging configuration
    plugin_zip_dir: str = metadata["plugin_package_name"]

    # Validate that the hardcoded name matches the metadata.
    if plugin_zip_dir != clean_plugin_name:
        msg: str = (
            "Name mismatch: The 'plugin_package_name' in metadata.txt "
            f"('{plugin_zip_dir}') "
            f"must match the 'name' from 'metadata.txt' with spaces replaced "
            f"by underscores ('{clean_plugin_name}')."
        )
        raise ReleaseScriptError(msg)

    # 2. Prepare content and paths
    clean_metadata_content: str = _get_clean_metadata_content(plugin_name)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg: str = f"Could not access or create release output directory: {e}"
        raise ReleaseScriptError(msg) from e

    zip_path: Path = output_dir / f"{clean_plugin_name}.zip"
    logger.info("Creating zip archive at: %s", zip_path)

    # 3. Create the zip archive
    with zipfile.ZipFile(
        zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zipf:
        _add_files_to_zip(
            zipf,
            metadata["files_to_package"],
            plugin_zip_dir,
            clean_metadata_content,
        )
        _add_directories_to_zip(
            zipf,
            metadata["dirs_to_package"],
            plugin_zip_dir,
            metadata["excluded_dirs"],
            metadata["excluded_extensions"],
        )

    logger.info(
        "✅ Successfully created plugin package in release repository: %s", zip_path
    )


"""

RUN

"""


def run_command(command: list[str], *, shell: bool = False) -> None:
    """Run a command in a subprocess and checks for errors.

    Args:
        command: The command to run as a list of strings.
        shell: Whether to run the command in a shell. Defaults to False.

    Raises:
        ReleaseScriptError: If the command fails (non-zero exit code).
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


def _run_git_command(
    command: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Git command and report failures as release errors."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *command],  # noqa: S607
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        msg = "Git was not found. Install Git or add it to PATH to commit releases."
        raise ReleaseScriptError(msg) from e
    except OSError as e:
        msg = f"Could not run Git in '{cwd}': {e}"
        raise ReleaseScriptError(msg) from e

    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        msg = f"Git command 'git {' '.join(command)}' failed."
        if detail:
            msg = f"{msg} {detail}"
        raise ReleaseScriptError(msg)

    return result


def _get_git_repository_paths(output_dir: Path) -> tuple[Path, Path]:
    """Return the repository root and output path relative to it."""
    output_dir = output_dir.resolve()
    if not output_dir.is_dir():
        msg = f"Release output directory does not exist: {output_dir}"
        raise ReleaseScriptError(msg)

    result = _run_git_command(
        ["rev-parse", "--show-toplevel"],
        output_dir,
        check=False,
    )
    if result.returncode != 0:
        msg = f"Release output directory '{output_dir}' is not inside a Git repository."
        raise ReleaseScriptError(msg)

    repository_root = Path(result.stdout.strip()).resolve()
    try:
        output_relative_path = output_dir.relative_to(repository_root)
    except ValueError as e:
        msg = (
            f"Release output directory '{output_dir}' is not inside its Git "
            f"repository '{repository_root}'."
        )
        raise ReleaseScriptError(msg) from e

    return repository_root, output_relative_path


def push_release_changes(output_dir: Path) -> None:
    """Push the current branch to its configured upstream."""
    repository_root, _ = _get_git_repository_paths(output_dir)
    branch_result = _run_git_command(
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        repository_root,
        check=False,
    )
    branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or not branch:
        msg = "Cannot push release: the release repository is in a detached HEAD state."
        raise ReleaseScriptError(msg)

    upstream_result = _run_git_command(
        [
            "for-each-ref",
            "--format=%(upstream:remotename)%00%(upstream:remoteref)",
            f"refs/heads/{branch}",
        ],
        repository_root,
    )
    upstream = upstream_result.stdout.strip()
    if not upstream or "\0" not in upstream:
        msg = (
            f"Cannot push release branch '{branch}': it has no configured upstream. "
            "Set one with 'git push --set-upstream <remote> <branch>'."
        )
        raise ReleaseScriptError(msg)

    remote, remote_ref = upstream.split("\0", 1)
    if not remote or remote == "." or not remote_ref:
        msg = f"Cannot determine a push destination for release branch '{branch}'."
        raise ReleaseScriptError(msg)

    logger.info("\n▶️ Pushing release branch '%s' to '%s'...", branch, remote)
    _run_git_command(["push", remote, f"HEAD:{remote_ref}"], repository_root)
    logger.info("✅ Pushed release branch '%s' to '%s'.", branch, remote)


def commit_release_changes(
    metadata: PluginMetadata,
    output_dir: Path,
    *,
    push: bool = False,
) -> None:
    """Commit only generated files and optionally push the current branch."""
    repository_root, output_relative_path = _get_git_repository_paths(output_dir)

    zip_name = f"{metadata['name'].replace(' ', '_')}.zip"
    release_files = [
        (output_relative_path / zip_name).as_posix(),
        (output_relative_path / "plugins.xml").as_posix(),
    ]

    logger.info("\n▶️ Committing generated release files...")
    _run_git_command(["add", "--", *release_files], repository_root)

    staged_changes = _run_git_command(
        ["diff", "--cached", "--quiet", "--", *release_files],
        repository_root,
        check=False,
    )
    if staged_changes.returncode == 0:
        logger.info("No changes to generated release files; skipping commit.")
        if push:
            push_release_changes(output_dir)
        return
    if staged_changes.returncode != 1:
        detail = staged_changes.stderr.strip()
        msg = "Could not check staged release files for changes."
        if detail:
            msg = f"{msg} {detail}"
        raise ReleaseScriptError(msg)

    commit_message = f"Release {metadata['name']} {metadata['version']}"
    _run_git_command(
        ["commit", "--only", "-m", commit_message, "--", *release_files],
        repository_root,
    )
    logger.info("✅ Committed generated release files: %s", commit_message)
    if push:
        push_release_changes(output_dir)


def run_release_process(
    output_dir: Path,
    *,
    commit: bool = False,
    push: bool = False,
) -> None:
    """Automate the plugin release process.

    This main function orchestrates the entire release process:
    1. Reads metadata.
    2. Compiles translations.
    3. Packages the plugin into the local repository directory.
    4. Updates plugins.xml with the public download URL.
    5. Optionally commits the generated files and pushes the current branch.

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

    compile_translations(metadata)
    package_plugin(metadata, output_dir)
    update_repository_file(metadata, output_dir)
    if commit or push:
        commit_release_changes(metadata, output_dir, push=push)

    logger.info("\n🎉 --- Release process complete! --- 🎉")
    logger.info(
        "✅ Plugin package and repository file are ready in: %s",
        output_dir,
    )


def _prompt_for_arguments() -> list[str] | None:
    """Display the release menu and return the selected command-line arguments."""
    while True:
        print(
            "\nSelect a release option:"
            "\n  1. Run release (default output directory)"
            "\n  2. Run release with a custom output directory"
            "\n  3. Run release and commit"
            "\n  4. Run release, commit, and push"
            "\n  Q. Quit"
        )
        choice = input("Choice: ").strip().lower()

        if choice == "1":
            return []
        if choice == "2":
            while True:
                output_dir = input("Output directory: ").strip()
                if output_dir:
                    if output_dir.startswith('"') and output_dir.endswith('"'):
                        output_dir = output_dir[1:-1]
                    return ["--output-dir", output_dir]
                print("Please enter an output directory.")
        if choice == "3":
            return ["--commit"]
        if choice == "4":
            return ["--push"]
        if choice in {"q", "quit"}:
            return None

        print("Invalid choice. Enter 1, 2, 3, 4, or Q.")


def main() -> int:
    """CLI entry point. Sets up logging and runs the release process.

    The output directory must be the docs folder of the cloned
    QGIS-Plugin-Releases repository, which is published via GitHub Pages.

    Returns:
        An exit code: 0 for success, 1 for failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(r"C:\Github_Repos\QGIS-Plugin-Releases\docs"),
        help="Local docs directory of the GitHub Pages release repository",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit the generated ZIP and plugins.xml in the release repository",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Commit generated files and push the release branch to its upstream",
    )
    cli_arguments = sys.argv[1:]
    if not cli_arguments:
        selected_arguments = _prompt_for_arguments()
        if selected_arguments is None:
            return 0
        cli_arguments = selected_arguments
    args = parser.parse_args(cli_arguments)

    setup_logging()
    try:
        run_release_process(args.output_dir, commit=args.commit, push=args.push)
    except ReleaseScriptError as e:
        logger.critical("❌ A critical error occurred: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
