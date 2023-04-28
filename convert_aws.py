from datetime import datetime

import os


from PIL import Image

from ocrd_utils import VERSION
from ocrd_models import OcrdExif
from ocrd_models.ocrd_page import (
    PcGtsType,
    PageType,
    MetadataType,
    TextRegionType,
    TextEquivType,
    CoordsType,
    TextLineType,
    WordType,
)

from ocrd_models.ocrd_page import to_xml
import json
import math

from typing import List, Dict
from dataclasses import dataclass
from functools import singledispatch


@dataclass
class TextractPoint:
    x: float
    y: float

    def __post_init__(self):
        if not 0 <= self.x <= 1:
            raise ValueError("x coordinate must be in the interval [0, 1].")
        if not 0 <= self.y <= 1:
            raise ValueError("y coordinate must be in the interval [0, 1].")


@dataclass
class TextractBoundingBox:
    left: float
    top: float
    width: float
    height: float

    def __init__(self, bbox_dict: Dict[str, float]):
        self.left = bbox_dict["Left"]
        self.top = bbox_dict["Top"]
        self.width = bbox_dict["Width"]
        self.height = bbox_dict["Height"]

    def __post_init__(self):
        if not 0 <= self.left <= 1:
            raise ValueError("Left must be in the interval [0, 1].")
        if not 0 <= self.top <= 1:
            raise ValueError("Top must be in the interval [0, 1].")
        if not 0 <= self.width <= 1:
            raise ValueError("Width must be in the interval [0, 1].")
        if not 0 <= self.height <= 1:
            raise ValueError("Height must be in the interval [0, 1].")
        if self.width + self.left > 1:
            raise ValueError("Sum of left and width must not exceed 1.")
        if self.height + self.top > 1:
            raise ValueError("Sum of top and height must not exceed 1.")


@dataclass
class TextractPolygon:
    points: List[TextractPoint]

    def __init__(self, polygon_dict: List[Dict[str, float]]):
        self.points = []
        for point in polygon_dict:
            point = TextractPoint(point["X"], point["Y"])
            self.points.append(point)

    def __post_init__(self):
        if len(self.points) < 3:
            raise ValueError("A polygon must have at least 3 points.")

    def get_bounding_box(self) -> TextractBoundingBox:
        x_coords = [p.x for p in self.points]
        y_coords = [p.y for p in self.points]
        bbox_dict = {
            "Left": min(x_coords),
            "Top": min(y_coords),
            "Width": max(x_coords) - min(x_coords),
            "Height": max(y_coords) - min(y_coords),
        }

        return TextractBoundingBox(bbox_dict)


@singledispatch
def points_from_awsgeometry(textract_geom, page_width, page_height):
    """Convert a Textract geomerty into a string of points, which are
    scaled to the image width and height."""

    raise NotImplementedError(
        f"Cannot process this type of data ({type(textract_geom)})"
    )


@points_from_awsgeometry.register
def _(textract_geom: TextractBoundingBox, page_width: int, page_height: int) -> str:
    """Convert a TextractBoundingBox into a string of points in the order top,left
    top,right bottom,right bottom,left.
    """

    x1 = math.ceil(textract_geom.left * page_width)
    y1 = math.ceil(textract_geom.top * page_height)
    x2 = math.ceil((textract_geom.left + textract_geom.width) * page_width)
    y2 = y1
    x3 = x2
    y3 = math.ceil((textract_geom.top + textract_geom.height) * page_height)
    x4 = x1
    y4 = y3

    points = f"{x1},{y1} {x2},{y2} {x3},{y3} {x4},{y4}"

    return points


@points_from_awsgeometry.register
def _(textract_geom: TextractPolygon, page_width: int, page_height: int) -> str:
    """Convert a TextractPolygon into a string of points."""

    return " ".join(
        [
            f"{math.ceil(p.x*page_width)},{math.ceil(p.y*page_height)}"
            for p in textract_geom.points
        ]
    )


def convert_textract(img_path: str, json_path: str, out_path: str) -> str:
    """Convert an AWS-Textract-JSON to PAGE-XML. Requires the original
    input image of AWS-OCR to get absolute image coordinates.

    Amazon Documentation: https://docs.aws.amazon.com/textract/latest/dg/how-it-works-document-layout.html


    AWS PAGE block is mapped to to TextRegion.
    AWS LINE block is mapped to to TextLine.
    AWS WORD block is mapped to to Word.

    Arguments:
        img_path (str): path to JPEG file
        json_path (str): path to JSON file
        out_path (str): path to output file (<path>/<filename>.xml)

    """

    pil_img = Image.open(img_path)
    exif = OcrdExif(pil_img)
    pil_img.close()

    width, height = exif.width, exif.height
    now = datetime.now()
    pc_gts_type = PcGtsType(
        Metadata=MetadataType(
            Creator="OCR-D/core %s" % VERSION, Created=now, LastChange=now
        )
    )
    pagexml_page = PageType(
        imageWidth=width,
        imageHeight=height,
        imageFilename=f"images/{os.path.basename(img_path)}",
    )
    pc_gts_type.set_Page(pagexml_page)

    json_file = open(json_path, "r")
    aws_json = json.load(json_file)
    json_file.close()

    page_block, line_blocks, word_blocks = {}, {}, {}

    for block in aws_json["Blocks"]:
        if block["BlockType"] == "PAGE":
            page_block = block
        if block["BlockType"] == "LINE":
            line_blocks[block["Id"]] = block
        if block["BlockType"] == "WORD":
            word_blocks[block["Id"]] = block

    poly = TextractPolygon(page_block["Geometry"]["Polygon"])
    print(poly)
    print(points_from_awsgeometry(poly, width, height))
    bbox = poly.get_bounding_box()
    print(bbox)
    print(points_from_awsgeometry(bbox, width, height))

    # TextRegion from PAGE-block
    pagexml_text_region = TextRegionType(
        TextEquiv=[TextEquivType(Unicode=page_block["childText"])],
        Coords=CoordsType(
            points=points_from_awsgeometry(
                TextractBoundingBox(page_block["Geometry"]["BoundingBox"]),
                width,
                height,
            )
        ),
        id=f'page-xml-{page_block["Id"]}',
    )
    pagexml_page.insert_TextRegion_at(0, pagexml_text_region)

    # AWS-Documentation: PAGE, LINE, and WORD blocks are related to each
    # other in a  parent-to-child relationship.

    # TextLine from LINE blocks that are listed in the PAGE-block's
    # child relationships
    for i, line_block_id in enumerate(
        [rel["Ids"] for rel in page_block["Relationships"] if rel["Type"] == "CHILD"][0]
    ):
        line_block = line_blocks[line_block_id]
        pagexml_text_line = TextLineType(
            TextEquiv=[TextEquivType(Unicode=line_block["childText"])],
            Coords=CoordsType(
                points=points_from_awsgeometry(
                    TextractBoundingBox(line_block["Geometry"]["BoundingBox"]),
                    width,
                    height,
                )
            ),
            id=f'page-xml-{line_block["Id"]}',
        )
        pagexml_text_region.insert_TextLine_at(i, pagexml_text_line)

        # Word from WORD blocks that are listed in the LINE-block's
        # child relationships
        for i, word_block_id in enumerate(
            [
                rel["Ids"]
                for rel in line_block["Relationships"]
                if rel["Type"] == "CHILD"
            ][0]
        ):
            word_block = word_blocks[word_block_id]
            pagexml_word = WordType(
                TextEquiv=[TextEquivType(Unicode=word_block["Text"])],
                Coords=CoordsType(
                    points=points_from_awsgeometry(
                        TextractBoundingBox(word_block["Geometry"]["BoundingBox"]),
                        width,
                        height,
                    )
                ),
                id=f'page-xml-{word_block["Id"]}',
            )
            pagexml_text_line.insert_Word_at(i, pagexml_word)

    with open(out_path, "w") as f:
        f.write(to_xml(pc_gts_type))


page_xml = convert_textract(
    "workspace/images/18xx-Missio-EMU-0042.jpg",
    "workspace/textract/18xx-Missio-EMU.json",
    "workspace/page/18xx-Missio-EMU-0042.xml",
)
