from pathlib import Path
from os import chdir
from unittest import TestCase, skip, main
from tempfile import NamedTemporaryFile
from ocrd_models.ocrd_page import parseEtree
from ocrd_models.constants import NAMESPACES as NS
from lxml import etree as ET

from textract2page import convert_file

THIS_DIR = Path(__file__).resolve().parent


class TestConvertTextract(TestCase):
    def setUp(self):
        workspace = THIS_DIR / "workspace"
        chdir(str(workspace))

        self.test_path_dict = [
            {
                "aws": Path("textract_responses")
                / f"{filename.name.split('.', 1)[0]}.json",
                "img": Path("images") / filename,
                "xml": Path("reference_page_xml")
                / f"{filename.name.split('.', 1)[0]}.xml",
            }
            for filename in (workspace / "images").iterdir()
        ]

        # self.aws = Path("textract_reponses") / "18xx-Missio-EMU.json"
        # self.img = Path("images") / "18xx-Missio-EMU-0042.jpg"
        # self.xml = Path("page") / "18xx-Missio-EMU-0042.xml"

    def test_api(self):
        for path in self.test_path_dict:
            print(path)
            _, target_tree, _, _ = parseEtree(path["xml"], silence=True)
            with NamedTemporaryFile() as out:
                convert_file(str(path["aws"]), str(path["img"]), out.name)
                _, result_tree, _, _ = parseEtree(out.name, silence=True)
                # remove elements bearing dates (Created, LastChange, Creator/Version)
                for meta in target_tree.xpath(
                    "/page:PcGts/page:Metadata/*", namespaces=NS
                ) + result_tree.xpath("/page:PcGts/page:Metadata/*", namespaces=NS):
                    meta.getparent().remove(meta)
                # remove img path from Page element

                del result_tree.find(".//page:Page", namespaces=NS).attrib[
                    "imageFilename"
                ]
                del target_tree.find(".//page:Page", namespaces=NS).attrib[
                    "imageFilename"
                ]
                assert ET.tostring(target_tree) == ET.tostring(result_tree)
