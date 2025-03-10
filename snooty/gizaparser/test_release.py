from pathlib import Path, PurePath
from typing import Dict, Tuple, List
from .release import GizaReleaseSpecificationCategory
from ..types import Diagnostic, Page, EmbeddedRstParser, ProjectConfig
from ..parser import make_embedded_rst_parser
from ..util import ast_to_testing_string


def test_release_specification() -> None:
    project_config = ProjectConfig(Path("test_data"), "")
    project_config.constants["version"] = "3.4"
    category = GizaReleaseSpecificationCategory(project_config)
    path = Path("test_data/release-specifications.yaml")
    parent_path = Path("test_data/release-base.yaml")

    def add_main_file() -> List[Diagnostic]:
        extracts, text, parse_diagnostics = category.parse(path)
        category.add(path, text, extracts)
        assert len(parse_diagnostics) == 0
        assert len(extracts) == 2
        return parse_diagnostics

    def add_parent_file() -> List[Diagnostic]:
        extracts, text, parse_diagnostics = category.parse(parent_path)
        category.add(parent_path, text, extracts)
        assert len(parse_diagnostics) == 0
        assert len(extracts) == 2
        return parse_diagnostics

    all_diagnostics: Dict[PurePath, List[Diagnostic]] = {}
    all_diagnostics[path] = add_main_file()
    all_diagnostics[parent_path] = add_parent_file()

    assert len(category) == 2
    file_id, giza_node = next(category.reify_all_files(all_diagnostics))

    def create_page() -> Tuple[Page, EmbeddedRstParser]:
        page = Page(path, "", {})
        return (
            page,
            make_embedded_rst_parser(project_config, page, all_diagnostics[path]),
        )

    pages = category.to_pages(create_page, giza_node.data)
    assert [str(page.fake_full_path()) for page in pages] == [
        "test_data/release/untar-release-osx-x86_64",
        "test_data/release/install-ent-windows-default",
    ]

    assert all((not diagnostics for diagnostics in all_diagnostics.values()))

    print(ast_to_testing_string(pages[0].ast))
    assert ast_to_testing_string(pages[0].ast) == "".join(
        (
            '<directive name="release_specification">',
            '<code lang="sh" copyable="True">',
            "tar -zxvf mongodb-macos-x86_64-3.4.tgz\n" "</code>",
            "</directive>",
        )
    )

    print(ast_to_testing_string(pages[1].ast))
    assert ast_to_testing_string(pages[1].ast) == "".join(
        (
            '<directive name="release_specification">',
            '<code lang="bat" copyable="True">',
            "msiexec.exe /l*v mdbinstall.log  /qb /i ",
            "mongodb-win32-x86_64-enterprise-windows-64-3.4-signed.msi\n",
            "</code>",
            "</directive>",
        )
    )
