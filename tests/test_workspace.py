from pathlib import Path
from os import chdir
from difflib import unified_diff
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

    def test_api(self):
        for path in self.test_path_dict:
            print(path)
            _, target_tree, _, _ = parseEtree(path["xml"], silence=True)
            with NamedTemporaryFile() as out:
                convert_file(str(path["aws"]), str(path["img"]), out.name)
                _, result_tree, _, _ = parseEtree(out.name, silence=True)
                # remove elements bearing dates (Created, LastChange, Creator/Version)
                for meta in target_tree.xpath(
                    "/pc:PcGts/pc:Metadata/*",
                    namespaces={
                        "pc": "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"
                    },
                ) + result_tree.xpath(
                    "/pc:PcGts/pc:Metadata/*",
                    namespaces={
                        "pc": "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"
                    },
                ):
                    meta.getparent().remove(meta)
                # remove img path from Page element

                res_img_path_elem = result_tree.find(
                    "pc:Page",
                    namespaces={
                        "pc": "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"
                    },
                )
                del res_img_path_elem.attrib["imageFilename"]
                tar_img_path_elem = target_tree.find(
                    "pc:Page",
                    namespaces={
                        "pc": "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"
                    },
                )
                del tar_img_path_elem.attrib["imageFilename"]
                target_xml = ET.tostring(target_tree, pretty_print=True, encoding='UTF-8').decode('utf-8')
                result_xml = ET.tostring(result_tree, pretty_print=True, encoding='UTF-8').decode('utf-8')
                assert target_xml == result_xml
