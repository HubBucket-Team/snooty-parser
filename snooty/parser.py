import collections
import docutils.nodes
import logging
import multiprocessing
import os
import pwd
import subprocess
import threading
from functools import partial
from pathlib import Path, PurePath
from typing import Any, Dict, Tuple, Optional, Set, List, Iterable
from typing_extensions import Protocol
import docutils.utils
import watchdog.events
import networkx

from . import gizaparser, rstparser, util
from .gizaparser.nodes import GizaCategory
from .types import (
    Diagnostic,
    SerializableType,
    EmbeddedRstParser,
    Page,
    StaticAsset,
    ProjectConfigError,
    ProjectConfig,
    PendingTask,
    FileId,
    Cache,
)

NO_CHILDREN = {"substitution_reference"}
RST_EXTENSIONS = {".rst", ".txt"}
logger = logging.getLogger(__name__)


class PendingLiteralInclude(PendingTask):
    """Transform a literal-include directive AST node into a code node."""

    def __init__(
        self,
        node: Dict[str, SerializableType],
        asset: StaticAsset,
        options: Dict[str, SerializableType],
    ) -> None:
        super().__init__(node)
        self.asset = asset
        self.options = options

    def __call__(self, diagnostics: List[Diagnostic], cache: Cache) -> None:
        """Load the literalinclude target text into our node."""
        # Use the cached node if our parameters match the cache entry
        options_key = hash(tuple(((k, v) for k, v in self.options.items())))
        entry = cache[(self.asset.fileid, options_key)]
        if entry is not None:
            assert isinstance(entry, dict)
            self.node.update(entry)
            return

        try:
            text = self.asset.path.read_text(encoding="utf-8")
        except OSError as err:
            diagnostics.append(
                self.error(f"Error opening {self.asset.fileid}: {err.strerror}")
            )
            return

        # Split the file into lines, and find our start-after query
        lines = text.split("\n")
        start_after = 0
        end_before = len(lines)
        if "start-after" in self.options:
            start_after_text = self.options["start-after"]
            assert isinstance(start_after_text, str)
            start_after = next(
                (idx for idx, line in enumerate(lines) if start_after_text in line), -1
            )
            if start_after < 0:
                diagnostics.append(
                    self.error(f'"{start_after_text}" not found in {self.asset.path}')
                )
                return

        # ...now find the end-before query
        if "end-before" in self.options:
            end_before_text = self.options["end-before"]
            assert isinstance(end_before_text, str)
            end_before = next(
                (
                    idx
                    for idx, line in enumerate(lines, start=start_after)
                    if end_before_text in line
                ),
                -1,
            )
            if end_before < 0:
                diagnostics.append(
                    self.error(f'"{end_before_text}" not found in {self.asset.path}')
                )
                return
            end_before -= start_after

        # Find the requested lines
        lines = lines[(start_after + 1) : end_before]

        # Deduce a reasonable dedent, if requested.
        if "dedent" in self.options:
            try:
                dedent = min(
                    len(line) - len(line.lstrip())
                    for line in lines
                    if len(line.lstrip()) > 0
                )
            except ValueError:
                # Handle the (unlikely) case where there are no non-empty lines
                dedent = 0
            lines = [line[dedent:] for line in lines]

        self.node.clear()
        lang = (
            self.options["language"]
            if "language" in self.options
            else self.asset.path.suffix.lstrip(".")
        )
        self.node.update(
            {
                "type": "code",
                "lang": lang,
                "copyable": "copyable" in self.options,
                "value": "\n".join(lines),
            }
        )

        if "emphasize_lines" in self.options:
            self.node["emphasize_lines"] = self.options["emphasize_lines"]

        # Update the cache with this node
        cache[(self.asset.fileid, options_key)] = self.node.copy()


class PendingFigure(PendingTask):
    """Add an image's checksum."""

    def __init__(self, node: Dict[str, SerializableType], asset: StaticAsset) -> None:
        super().__init__(node)
        self.asset = asset

    def __call__(self, diagnostics: List[Diagnostic], cache: Cache) -> None:
        """Compute this figure's checksum and store it in our node."""
        # Use the cached checksum if possible. Note that this does not currently
        # update the underlying asset: if the asset is used by the current backend,
        # the image will still have to be read.
        options = self.node.setdefault("options", {})
        assert isinstance(options, dict)
        entry = cache[(self.asset.fileid, 0)]
        if entry is not None:
            assert isinstance(entry, str)
            options["checksum"] = entry
            return

        try:
            checksum = self.asset.get_checksum()
            options["checksum"] = checksum
            cache[(self.asset.fileid, 0)] = checksum
        except OSError as err:
            diagnostics.append(
                self.error(f"Error opening {self.asset.fileid}: {err.strerror}")
            )


class JSONVisitor:
    """Node visitor that creates a JSON-serializable structure."""

    def __init__(
        self, source_path: Path, docpath: PurePath, document: docutils.nodes.document
    ) -> None:
        self.source_path = source_path
        self.docpath = docpath
        self.document = document
        self.state: List[Dict[str, Any]] = []
        self.diagnostics: List[Diagnostic] = []
        self.static_assets: Set[StaticAsset] = set()
        self.pending: List[PendingTask] = []

    def dispatch_visit(self, node: docutils.nodes.Node) -> None:
        node_name = node.__class__.__name__
        if node_name == "system_message":
            level = int(node["level"])
            if level >= 2:
                level = Diagnostic.Level.from_docutils(level)
                msg = node[0].astext()
                self.diagnostics.append(
                    Diagnostic.create(level, msg, util.get_line(node))
                )
            raise docutils.nodes.SkipNode()
        elif node_name in ("definition", "field_list"):
            return

        if node_name == "document":
            self.state.append(
                {"type": "root", "children": [], "position": {"start": {"line": 0}}}
            )
            return

        doc: Dict[str, SerializableType] = {
            "type": node_name,
            "position": {"start": {"line": util.get_line(node)}},
        }

        if node_name == "field":
            key = node.children[0].astext()
            value = node.children[1].astext()
            self.state[-1].setdefault("options", {})[key] = value
            raise docutils.nodes.SkipNode()
        elif node_name == "code":
            doc["type"] = "code"
            doc["lang"] = node["lang"]
            doc["copyable"] = node["copyable"]
            if node["emphasize_lines"]:
                doc["emphasize_lines"] = node["emphasize_lines"]
            doc["value"] = node.astext()
            self.state[-1]["children"].append(doc)
            raise docutils.nodes.SkipNode()

        # We are uninterested in docutils blockquotes: they're too easy to accidentally
        # invoke. Treat them as an error.
        if node_name == "block_quote":
            self.diagnostics.append(
                Diagnostic.error(
                    "Unexpected indentation", util.get_line(node.children[0])
                )
            )
            return

        if node_name == "directive":
            if self.handle_directive(node, doc):
                self.state.append(doc)
            return

        self.state.append(doc)

        if node_name == "Text":
            doc["type"] = "text"
            doc["value"] = str(node)
            return

        if node_name == "role":
            doc["name"] = node["name"]
            if "label" in node:
                doc["label"] = node["label"]
            if "target" in node:
                doc["target"] = node["target"]

            if doc["name"] == "doc":
                self.validate_doc_role(node)
        elif node_name == "target":
            doc["type"] = "target"
            doc["ids"] = node["ids"]
            if "refuri" in node:
                doc["refuri"] = node["refuri"]
        elif node_name == "definition_list":
            doc["type"] = "definitionList"
        elif node_name == "definition_list_item":
            doc["type"] = "definitionListItem"
            doc["term"] = []
        elif node_name == "bullet_list":
            doc["type"] = "list"
            doc["ordered"] = False
        elif node_name == "enumerated_list":
            doc["type"] = "list"
            doc["ordered"] = True
        elif node_name == "list_item":
            doc["type"] = "listItem"
        elif node_name == "title":
            doc["type"] = "heading"
            # Attach an anchor ID to this section
            assert node.parent
            doc["id"] = node.parent["ids"][0]
        elif node_name == "reference":
            for attr_name in ("refuri", "refname"):
                if attr_name in node:
                    doc[attr_name] = node[attr_name]
        elif node_name == "substitution_definition":
            name = node["names"][0]
            doc["name"] = name
        elif node_name == "substitution_reference":
            doc["name"] = node["refname"]
            return

        doc["children"] = []

    def dispatch_departure(self, node: docutils.nodes.Node) -> None:
        node_name = node.__class__.__name__
        if len(self.state) == 1 or node_name == "definition":
            return

        if node_name == "block_quote":
            return

        popped = self.state.pop()

        if popped["type"] == "term":
            self.state[-1]["term"] = popped["children"]
        elif self.state[-1]["type"] not in NO_CHILDREN:
            if "children" not in self.state[-1]:
                print(self.state[-1], popped)
            self.state[-1]["children"].append(popped)

    def handle_directive(
        self, node: docutils.nodes.Node, doc: Dict[str, SerializableType]
    ) -> bool:
        name = node["name"]
        doc["name"] = name

        options = node["options"] or {}
        if (
            node.children
            and node.children[0].__class__.__name__ == "directive_argument"
        ):
            visitor = self.__make_child_visitor()
            node.children[0].walkabout(visitor)
            argument = visitor.state[-1]["children"]
            doc["argument"] = argument
            node.children = node.children[1:]
        else:
            argument = []
            doc["argument"] = argument

        argument_text = None
        try:
            argument_text = argument[0]["value"]
        except (IndexError, KeyError):
            pass

        if name == "todo":
            todo_text = ["TODO"]
            if argument_text:
                todo_text.extend([": ", argument_text])
            self.diagnostics.append(
                Diagnostic.info("".join(todo_text), util.get_line(node))
            )
            return False

        if name in {"figure", "image"}:
            if argument_text is None:
                self.diagnostics.append(
                    Diagnostic.error(
                        f'"{name}" expected a path argument', util.get_line(node)
                    )
                )
                return True

            try:
                static_asset = self.add_static_asset(Path(argument_text), upload=True)
                self.pending.append(PendingFigure(doc, static_asset))
            except OSError as err:
                msg = f'"{name}" could not open "{argument_text}": {os.strerror(err.errno)}'
                self.diagnostics.append(Diagnostic.error(msg, util.get_line(node)))
        elif name == "literalinclude":
            if argument_text is None:
                lineno = util.get_line(node)
                self.diagnostics.append(
                    Diagnostic.error(
                        '"literalinclude" expected a path argument', lineno
                    )
                )
                return True

            try:
                static_asset = self.add_static_asset(Path(argument_text), False)
                self.pending.append(PendingLiteralInclude(doc, static_asset, options))
            except OSError as err:
                msg = '"literalinclude" could not open "{}": {}'.format(
                    argument_text, os.strerror(err.errno)
                )
                self.diagnostics.append(Diagnostic.error(msg, util.get_line(node)))
            except ValueError as err:
                msg = f'Invalid "literalinclude": {err}'
                self.diagnostics.append(Diagnostic.error(msg, util.get_line(node)))
            return True
        elif name == "include":
            if argument_text is None:
                self.diagnostics.append(
                    Diagnostic.error(
                        f'"{name}" expected a path argument', util.get_line(node)
                    )
                )
                return True

            fileid, path = util.reroot_path(
                Path(argument_text), self.docpath, self.source_path
            )

            # Validate if file exists
            if not path.is_file():
                # Check if file is snooty-generated
                if (
                    fileid.match("steps/*.rst")
                    or fileid.match("extracts/*.rst")
                    or fileid.match("release/*.rst")
                    or fileid.match("option/*.rst")
                    or fileid.match("toc/*.rst")
                    or fileid.match("apiargs/*.rst")
                    or fileid == FileId("includes/hash.rst")
                ):
                    pass
                else:
                    msg = f'"{name}" could not open "{argument_text}": No such file exists'
                    self.diagnostics.append(Diagnostic.error(msg, util.get_line(node)))

        if options:
            doc["options"] = options

        doc["children"] = []
        return True

    def validate_doc_role(self, node: docutils.nodes.Node) -> None:
        """Validate target for doc role"""
        target = PurePath(node["target"]).with_suffix(".txt")
        fileid, target_path = util.reroot_path(target, self.docpath, self.source_path)

        if not target_path.is_file():
            msg = (
                f'"{node["name"]}" could not open "{target_path}": No such file exists'
            )
            self.diagnostics.append(Diagnostic.error(msg, util.get_line(node)))

    def add_static_asset(self, path: Path, upload: bool) -> StaticAsset:
        fileid, path = util.reroot_path(path, self.docpath, self.source_path)
        static_asset = StaticAsset.load(fileid, path, upload)
        self.static_assets.add(static_asset)
        return static_asset

    def add_diagnostics(self, diagnostics: Iterable[Diagnostic]) -> None:
        self.diagnostics.extend(diagnostics)

    def __make_child_visitor(self) -> "JSONVisitor":
        visitor = type(self)(self.source_path, self.docpath, self.document)
        visitor.diagnostics = self.diagnostics
        visitor.static_assets = self.static_assets
        visitor.pending = self.pending
        return visitor


class InlineJSONVisitor(JSONVisitor):
    """A JSONVisitor subclass which does not emit block nodes."""

    def dispatch_visit(self, node: docutils.nodes.Node) -> None:
        if isinstance(node, docutils.nodes.Body):
            return

        JSONVisitor.dispatch_visit(self, node)

    def dispatch_departure(self, node: docutils.nodes.Node) -> None:
        if isinstance(node, docutils.nodes.Body):
            return

        JSONVisitor.dispatch_departure(self, node)


def parse_rst(
    parser: rstparser.Parser[JSONVisitor], path: Path, text: Optional[str] = None
) -> Tuple[Page, List[Diagnostic]]:
    visitor, text = parser.parse(path, text)

    return (
        Page(path, text, visitor.state[-1], visitor.static_assets, visitor.pending),
        visitor.diagnostics,
    )


def make_embedded_rst_parser(
    project_config: ProjectConfig, page: Page, diagnostics: List[Diagnostic]
) -> EmbeddedRstParser:
    def parse_embedded_rst(
        rst: str, lineno: int, inline: bool
    ) -> List[SerializableType]:
        # Crudely make docutils line numbers match
        text = "\n" * lineno + rst.strip()
        visitor_class = InlineJSONVisitor if inline else JSONVisitor
        parser = rstparser.Parser(project_config, visitor_class)
        visitor, _ = parser.parse(page.source_path, text)
        children: List[SerializableType] = visitor.state[-1]["children"]

        diagnostics.extend(visitor.diagnostics)
        page.static_assets.update(visitor.static_assets)
        page.pending_tasks.extend(visitor.pending)

        return children

    return parse_embedded_rst


def get_giza_category(path: PurePath) -> str:
    """Infer the Giza category of a YAML file."""
    return path.name.split("-", 1)[0]


class ProjectBackend(Protocol):
    def on_progress(self, progress: int, total: int, message: str) -> None:
        ...

    def on_diagnostics(self, path: FileId, diagnostics: List[Diagnostic]) -> None:
        ...

    def on_update(self, prefix: List[str], page_id: FileId, page: Page) -> None:
        ...

    def on_delete(self, page_id: FileId) -> None:
        ...


class _Project:
    """Internal representation of a Snooty project with no data locking."""

    def __init__(
        self, root: Path, backend: ProjectBackend, filesystem_watcher: util.FileWatcher
    ) -> None:
        root = root.resolve(strict=True)
        self.config, config_diagnostics = ProjectConfig.open(root)

        if config_diagnostics:
            backend.on_diagnostics(
                self.get_fileid(self.config.config_path), config_diagnostics
            )
            raise ProjectConfigError()

        self.root = self.config.source_path
        self.parser = rstparser.Parser(self.config, JSONVisitor)
        self.backend = backend
        self.filesystem_watcher = filesystem_watcher

        self.yaml_mapping: Dict[str, GizaCategory[Any]] = {
            "steps": gizaparser.steps.GizaStepsCategory(self.config),
            "extracts": gizaparser.extracts.GizaExtractsCategory(self.config),
            "release": gizaparser.release.GizaReleaseSpecificationCategory(self.config),
        }

        username = pwd.getpwuid(os.getuid()).pw_name
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, encoding="utf-8"
        ).strip()
        self.prefix = [self.config.name, username, branch]

        self.pages: Dict[FileId, Page] = {}

        self.asset_dg: "networkx.DiGraph[FileId]" = networkx.DiGraph()
        self._expensive_operation_cache = Cache()

    def get_fileid(self, path: PurePath) -> FileId:
        return FileId(path.relative_to(self.root))

    def get_full_path(self, fileid: FileId) -> Path:
        return self.root.joinpath(fileid)

    def update(self, path: Path, optional_text: Optional[str] = None) -> None:
        diagnostics: Dict[PurePath, List[Diagnostic]] = {path: []}
        prefix = get_giza_category(path)
        _, ext = os.path.splitext(path)
        pages: List[Page] = []
        if ext in RST_EXTENSIONS:
            page, page_diagnostics = parse_rst(self.parser, path, optional_text)
            pages.append(page)
            diagnostics[path] = page_diagnostics
        elif ext == ".yaml" and prefix in self.yaml_mapping:
            file_id = os.path.basename(path)
            giza_category = self.yaml_mapping[prefix]
            needs_rebuild = set((file_id,)).union(
                *(
                    category.dg.predecessors(file_id)
                    for category in self.yaml_mapping.values()
                )
            )
            logger.debug("needs_rebuild: %s", ",".join(needs_rebuild))
            for file_id in needs_rebuild:
                file_diagnostics: List[Diagnostic] = []
                try:
                    giza_node = giza_category.reify_file_id(file_id, diagnostics)
                except KeyError:
                    logging.warn("No file found in registry: %s", file_id)
                    continue

                steps, text, parse_diagnostics = giza_category.parse(
                    path, optional_text
                )
                file_diagnostics.extend(parse_diagnostics)

                def create_page() -> Tuple[Page, EmbeddedRstParser]:
                    page = Page(giza_node.path, text, {})
                    return (
                        page,
                        make_embedded_rst_parser(self.config, page, file_diagnostics),
                    )

                giza_category.add(path, text, steps)
                pages = giza_category.to_pages(create_page, giza_node.data)
                path = giza_node.path
                diagnostics.setdefault(path).extend(file_diagnostics)
        else:
            raise ValueError("Unknown file type: " + str(path))

        for source_path, diagnostic_list in diagnostics.items():
            self.backend.on_diagnostics(self.get_fileid(source_path), diagnostic_list)

        for page in pages:
            self._page_updated(page, diagnostic_list)

    def delete(self, path: PurePath) -> None:
        file_id = os.path.basename(path)
        for giza_category in self.yaml_mapping.values():
            del giza_category[file_id]

        self.backend.on_delete(self.get_fileid(path))

    def build(self) -> None:
        all_yaml_diagnostics: Dict[PurePath, List[Diagnostic]] = {}
        with multiprocessing.Pool() as pool:
            paths = util.get_files(self.root, RST_EXTENSIONS)
            logger.debug("Processing rst files")
            results = pool.imap_unordered(partial(parse_rst, self.parser), paths)
            for page, diagnostics in results:
                self._page_updated(page, diagnostics)

        # Categorize our YAML files
        logger.debug("Categorizing YAML files")
        categorized: Dict[str, List[Path]] = collections.defaultdict(list)
        for path in util.get_files(self.root, (".yaml",)):
            prefix = get_giza_category(path)
            if prefix in self.yaml_mapping:
                categorized[prefix].append(path)

        # Initialize our YAML file registry
        for prefix, giza_category in self.yaml_mapping.items():
            logger.debug("Parsing %s YAML", prefix)
            for path in categorized[prefix]:
                steps, text, diagnostics = giza_category.parse(path)
                all_yaml_diagnostics[path] = diagnostics
                giza_category.add(path, text, steps)

        # Now that all of our YAML files are loaded, generate a page for each one
        for prefix, giza_category in self.yaml_mapping.items():
            logger.debug("Processing %s YAML: %d nodes", prefix, len(giza_category))
            for file_id, giza_node in giza_category.reify_all_files(
                all_yaml_diagnostics
            ):

                def create_page() -> Tuple[Page, EmbeddedRstParser]:
                    page = Page(giza_node.path, giza_node.text, {})
                    return (
                        page,
                        make_embedded_rst_parser(
                            self.config,
                            page,
                            all_yaml_diagnostics.setdefault(giza_node.path, []),
                        ),
                    )

                for page in giza_category.to_pages(create_page, giza_node.data):
                    self._page_updated(
                        page, all_yaml_diagnostics.get(page.source_path, [])
                    )

    def _page_updated(self, page: Page, diagnostics: List[Diagnostic]) -> None:
        """Update any state associated with a parsed page."""
        # Finish any pending tasks
        page.finish(diagnostics, self._expensive_operation_cache)

        # Synchronize our asset watching
        old_assets: Set[StaticAsset] = set()
        removed_assets: Set[StaticAsset] = set()
        fileid = self.get_fileid(page.fake_full_path())

        logger.debug("Updated: %s", fileid)

        if fileid in self.pages:
            old_page = self.pages[fileid]
            old_assets = old_page.static_assets
            removed_assets = old_page.static_assets.difference(page.static_assets)

        new_assets = page.static_assets.difference(old_assets)
        for asset in new_assets:
            try:
                self.filesystem_watcher.watch_file(asset.path)
            except OSError as err:
                # Missing static asset directory: don't process it. We've already raised a
                # diagnostic to the user.
                logger.debug(f"Failed to set up watch: {err}")
                page.static_assets.remove(asset)
        for asset in removed_assets:
            self.filesystem_watcher.end_watch(asset.path)

        # Update dependents
        try:
            self.asset_dg.remove_node(self.get_fileid(page.source_path))
        except networkx.exception.NetworkXError:
            pass
        self.asset_dg.add_edges_from(
            (self.get_fileid(page.source_path), self.get_fileid(asset.path))
            for asset in page.static_assets
        )

        # Report to our backend
        self.pages[fileid] = page
        self.backend.on_update(self.prefix, fileid, page)
        self.backend.on_diagnostics(self.get_fileid(page.source_path), diagnostics)

    def on_asset_event(self, ev: watchdog.events.FileSystemEvent) -> None:
        asset_path = self.get_fileid(Path(ev.src_path))

        # Revoke any caching that might have been performed on this file
        try:
            del self._expensive_operation_cache[asset_path]
        except KeyError:
            pass

        # Rebuild any pages depending on this asset
        for page_id in list(self.asset_dg.predecessors(asset_path)):
            self.update(self.pages[page_id].source_path)


class Project:
    """A Snooty project, providing high-level operations on a project such as
       requesting a rebuild, and updating a file based on new contents.

       This class's public methods are thread-safe."""

    __slots__ = ("_project", "_lock", "_filesystem_watcher")

    def __init__(self, root: Path, backend: ProjectBackend) -> None:
        self._filesystem_watcher = util.FileWatcher(self._on_asset_event)
        self._project = _Project(root, backend, self._filesystem_watcher)
        self._lock = threading.Lock()
        self._filesystem_watcher.start()

    @property
    def config(self) -> ProjectConfig:
        return self._project.config

    def get_fileid(self, path: PurePath) -> FileId:
        """Create a FileId from a path."""
        # We don't need to obtain a lock because this method only operates on
        # _Project.root, which never changes after creation.
        return self._project.get_fileid(path)

    def get_full_path(self, fileid: FileId) -> Path:
        # We don't need to obtain a lock because this method only operates on
        # _Project.root, which never changes after creation.
        return self._project.get_full_path(fileid)

    def update(self, path: Path, optional_text: Optional[str] = None) -> None:
        """Re-parse a file, optionally using the provided text rather than reading the file."""
        with self._lock:
            self._project.update(path, optional_text)

    def delete(self, path: PurePath) -> None:
        """Mark a path as having been deleted."""
        with self._lock:
            self._project.delete(path)

    def build(self) -> None:
        """Build the full project."""
        with self._lock:
            self._project.build()

    def stop_monitoring(self) -> None:
        """Stop the filesystem monitoring thread associated with this project."""
        self._filesystem_watcher.stop(join=True)

    def _on_asset_event(self, ev: watchdog.events.FileSystemEvent) -> None:
        with self._lock:
            self._project.on_asset_event(ev)

    def __enter__(self) -> "Project":
        return self

    def __exit__(self, *args: object) -> None:
        self.stop_monitoring()
