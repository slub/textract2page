from pathlib import Path
from os import chdir
from difflib import unified_diff
from pytest import fixture
from lxml import etree as ET
from PIL import Image

from ocrd_utils import pushd_popd
from ocrd import Resolver
from ocrd_models.ocrd_page import parseEtree
from ocrd_models.constants import NAMESPACES as NS

from textract2page import convert_file, convert_file_without_image

THIS_DIR = Path(__file__).resolve().parent

@fixture
def workspace_path(tmpdir):
    workspace = str(THIS_DIR / "workspace" / "mets.xml")
    workspace = Resolver().workspace_from_url(workspace, dst_dir=tmpdir, download=True)
    with pushd_popd(tmpdir):
        yield tmpdir

def test_api(workspace_path, tmpdir):
    test_path_dict = [
        {
            "aws": Path("textract_responses") / f"{filename.name.split('.', 1)[0]}.json",
            "img": Path("images") / filename.name,
            "xml": Path("reference_page_xml") / f"{filename.name.split('.', 1)[0]}.xml",
        }
        for filename in Path("images").iterdir()
    ]
    for path in test_path_dict:
        _, target_tree, _, _ = parseEtree(path["xml"], silence=True)
        convert_file(str(path["aws"]), str(path["img"]), str(tmpdir/path["xml"]))
        _, result_tree, _, _ = parseEtree(tmpdir/path["xml"], silence=True)
        # remove elements bearing dates (Created, LastChange, Creator/Version)
        for meta in target_tree.xpath(
            "/page:PcGts/page:Metadata/*",
            namespaces=NS,
        ) + result_tree.xpath(
            "/page:PcGts/page:Metadata/*",
            namespaces=NS,
        ):
            meta.getparent().remove(meta)
        # remove img path from Page element

        res_img_path_elem = result_tree.find(
            "page:Page",
            namespaces=NS,
        )
        del res_img_path_elem.attrib["imageFilename"]
        tar_img_path_elem = target_tree.find(
            "page:Page",
            namespaces=NS,
        )
        del tar_img_path_elem.attrib["imageFilename"]
        target_xml = ET.tostring(target_tree, pretty_print=True, encoding='UTF-8').decode('utf-8')
        result_xml = ET.tostring(result_tree, pretty_print=True, encoding='UTF-8').decode('utf-8')
        assert result_xml == target_xml, path

def test_api_without_image(workspace_path, tmpdir):
    test_path_dict = [
        {
            "aws": Path("textract_responses") / f"{filename.name.split('.', 1)[0]}.json",
            "img": Path("images") / filename.name,
            "xml": Path("reference_page_xml") / f"{filename.name.split('.', 1)[0]}.xml",
        }
        for filename in Path("images").iterdir()
    ]
    for path in test_path_dict:
        _, target_tree, _, _ = parseEtree(path["xml"], silence=True)
        with Image.open(str(path["img"])) as img:
            convert_file_without_image(str(path["aws"]), str(path["img"]),
                                       img.width, img.height, str(tmpdir/path["xml"]))
        _, result_tree, _, _ = parseEtree(tmpdir/path["xml"], silence=True)
        # remove elements bearing dates (Created, LastChange, Creator/Version)
        for meta in target_tree.xpath(
            "/page:PcGts/page:Metadata/*",
            namespaces=NS,
        ) + result_tree.xpath(
            "/page:PcGts/page:Metadata/*",
            namespaces=NS,
        ):
            meta.getparent().remove(meta)
        # remove img path from Page element

        res_img_path_elem = result_tree.find(
            "page:Page",
            namespaces=NS,
        )
        del res_img_path_elem.attrib["imageFilename"]
        tar_img_path_elem = target_tree.find(
            "page:Page",
            namespaces=NS,
        )
        del tar_img_path_elem.attrib["imageFilename"]
        target_xml = ET.tostring(target_tree, pretty_print=True, encoding='UTF-8').decode('utf-8')
        result_xml = ET.tostring(result_tree, pretty_print=True, encoding='UTF-8').decode('utf-8')
        assert result_xml == target_xml, path
