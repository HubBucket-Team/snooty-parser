import logging
import os
import docutils.nodes
import docutils.parsers.rst.directives
import watchdog.events
import watchdog.observers
import watchdog.observers.api
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import (
    cast,
    Any,
    Callable,
    Container,
    Counter,
    Dict,
    Optional,
    Tuple,
    TypeVar,
    Iterator,
    Hashable,
)
from .types import FileId, SerializableType

logger = logging.getLogger(__name__)
_K = TypeVar("_K", bound=Hashable)


def reroot_path(
    filename: PurePath, docpath: PurePath, project_root: Path
) -> Tuple[FileId, Path]:
    """Files within a project may refer to other files. Return a canonical path
       relative to the project root."""
    if filename.is_absolute():
        rel_fn = FileId(*filename.parts[1:])
    else:
        rel_fn = FileId(os.path.normpath(docpath.parent.joinpath(filename)))
    return rel_fn, project_root.joinpath(rel_fn).resolve()


def get_files(root: PurePath, extensions: Container[str]) -> Iterator[Path]:
    """Recursively iterate over files underneath the given root, yielding
       only filenames with the given extensions."""
    for base, dirs, files in os.walk(root):
        for name in files:
            ext = os.path.splitext(name)[1]

            if ext not in extensions:
                continue

            yield Path(os.path.join(base, name))


def get_line(node: docutils.nodes.Node) -> int:
    """Return the first line number we can find in node's ancestry."""

    def line_of_node(node: docutils.nodes.Node) -> Optional[int]:
        """Sometimes you need node['line']. Sometimes you need node.line.
           Sometimes you want to just run away and herd yaks."""
        if isinstance(node, docutils.nodes.Element) and "line" in node:
            return cast(int, node["line"])

        return node.line

    while line_of_node(node) is None:
        if node.parent is None:
            # This is probably a document node
            return 0
        node = node.parent

    return cast(int, line_of_node(node)) - 1


def ast_to_testing_string(ast: Any) -> str:
    value = ast.get("value", "")
    children = ast.get("children", [])
    attr_pairs = [
        (k, v)
        for k, v in ast.items()
        if k not in ("argument", "value", "children", "type", "position", "options")
        and v
    ]
    attr_pairs.extend((k, v) for k, v in ast.get("options", {}).items())
    attrs = " ".join('{}="{}"'.format(k, v) for k, v in attr_pairs)
    contents = (
        value
        if value
        else (
            "".join(ast_to_testing_string(child) for child in children)
            if children
            else ""
        )
    )
    if "argument" in ast:
        contents = (
            "".join(ast_to_testing_string(part) for part in ast["argument"]) + contents
        )
    return "<{}{}>{}</{}>".format(
        ast["type"], " " + attrs if attrs else "", contents, ast["type"]
    )


def ast_dive(ast: Any) -> Iterator[Dict[str, SerializableType]]:
    """Yield each node in an AST in no particular order."""
    children = ast.get("children", []) + ast.get("argument", [])
    yield ast
    for child in children:
        yield from ast_dive(child)


class FileWatcher:
    """A monitor for file changes."""

    class AssetChangedHandler(watchdog.events.FileSystemEventHandler):
        """A filesystem event handler which flags pages as having changed
        after an included asset has changed."""

        def __init__(
            self,
            directories: Dict[Path, "FileWatcher.AssetWatch"],
            on_event: Callable[[watchdog.events.FileSystemEvent], None],
        ) -> None:
            super().__init__()
            self.directories = directories
            self.on_event = on_event

        def dispatch(self, event: watchdog.events.FileSystemEvent) -> None:
            """Delegate filesystem events."""
            path = Path(event.src_path)
            if (
                path.parent in self.directories
                and path.name in self.directories[path.parent].filenames
            ):
                self.on_event(event)

    @dataclass
    class AssetWatch:
        """Track files in a directory to watch. This reflects the underlying interface
           exposed by watchdog."""

        filenames: Counter[str]
        watch_handle: watchdog.observers.api.ObservedWatch

        def __len__(self) -> int:
            return len(self.filenames)

    def __init__(
        self, on_event: Callable[[watchdog.events.FileSystemEvent], None]
    ) -> None:
        self.observer = watchdog.observers.Observer()
        self.directories: Dict[Path, FileWatcher.AssetWatch] = {}
        self.handler = self.AssetChangedHandler(self.directories, on_event)

    def watch_file(self, path: Path) -> None:
        """Start reporting upon changes to a file."""
        directory = path.parent
        logger.debug("Starting watch: %s", path)
        if directory in self.directories:
            self.directories[directory].filenames[path.name] += 1
            return

        watch = self.observer.schedule(self.handler, str(directory))
        self.directories[directory] = self.AssetWatch(Counter({path.name: 1}), watch)

    def end_watch(self, path: Path) -> None:
        """Stop watching a file."""
        directory = path.parent
        if directory not in self.directories:
            return

        watch = self.directories[directory]
        watch.filenames[path.name] -= 1
        if watch.filenames[path.name] <= 0:
            del watch.filenames[path.name]

        # If there are no files remaining in this watch directory, unwatch it.
        if not watch.filenames:
            self.observer.unschedule(watch.watch_handle)
            logger.info("Stopping watch: %s", path)
            del self.directories[directory]

    def start(self) -> None:
        """Start a thread watching for file changes."""
        self.observer.start()

    def stop(self, join: bool = False) -> None:
        """Stop this file watcher."""
        self.observer.stop()
        if join:
            self.observer.join()

    def __enter__(self) -> "FileWatcher":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def __len__(self) -> int:
        return sum(len(w) for w in self.directories.values())


def ast_get_text(ast: Any) -> str:
    """Return pure textual content from a given AST node."""
    if ast.get("type") == "text":
        return cast(str, ast["value"])

    label = ast.get("label", None)
    if label:
        return ast_get_text(label)

    children = ast.get("children", ())
    return "".join(ast_get_text(child) for child in children)


def option_bool(argument: Optional[str]) -> bool:
    """
    Check for a valid boolean option return it. If not argument is given,
    treat it as a flag, and return True.
    """
    if argument and argument.strip():
        return bool(docutils.parsers.rst.directives.choice(argument, ("true", "false")))
    else:
        return True


def option_flag(argument: Optional[str]) -> bool:
    """
    Variant of the docutils flag handler.
    Check for a valid flag option (no argument) and return ``True``.
    (Directive option conversion function.)

    Raise ``ValueError`` if an argument is found.
    """
    if argument and argument.strip():
        raise ValueError('no argument is allowed; "%s" supplied' % argument)
    else:
        return True
