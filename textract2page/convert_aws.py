import json
import math
import sys
from typing import List, Dict
from dataclasses import dataclass
from functools import singledispatch
from datetime import datetime
from PIL import Image

from ocrd_utils import VERSION
from ocrd_models.ocrd_page import (
    PcGtsType,
    PageType,
    MetadataType,
    TextRegionType,
    TextEquivType,
    CoordsType,
    TextLineType,
    WordType,
    ReadingOrderType,
    OrderedGroupType,
    RegionRefIndexedType
)
from ocrd_models.ocrd_page import to_xml


@dataclass
class TextractPoint:
    x: float
    y: float

    def __post_init__(self):
        assert 0 <= self.x <= 1, self
        assert 0 <= self.y <= 1, self


@dataclass
class TextractBoundingBox:
    left: float
    top: float
    width: float
    height: float

    def __init__(self, bbox_dict: Dict[str, float]):
        self.left = bbox_dict.get("Left", -1)
        self.top = bbox_dict.get("Top", -1)
        self.width = bbox_dict.get("Width", -1)
        self.height = bbox_dict.get("Height", -1)
        self.__post_init__()

    def __post_init__(self):
        assert 0 <= self.left <= 1, self
        assert 0 <= self.top <= 1, self
        assert 0 <= self.width <= 1, self
        assert 0 <= self.height <= 1, self
        assert self.width + self.left <= 1, self
        assert self.height + self.top <= 1, self


@dataclass
class TextractPolygon:
    points: List[TextractPoint]

    def __init__(self, polygon: List[Dict[str, float]]):
        self.points = [TextractPoint(point.get("X", -1),
                                     point.get("Y", -1))
                       for point in polygon]
        self.__post_init__()

    def __post_init__(self):
        assert len(self.points) >= 3, len(self.points)

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
    """Convert a Textract geometry into a string of points, which are
    scaled to the image width and height."""

    raise NotImplementedError(
        f"Cannot process this type of data ({type(textract_geom)})"
    )


@points_from_awsgeometry.register
def _(
    textract_geom: TextractBoundingBox, page_width: int, page_height: int
) -> str:
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

    points = " ".join(f"{math.ceil(point.x * page_width)},{math.ceil(point.y * page_height)}"
                      for point in textract_geom.points)

    return points


def convert_file(json_path: str, img_path: str, out_path: str, preserve_reading_order: bool = True) -> None:
    """Convert an AWS-Textract-JSON file to a PAGE-XML file.

    Also requires the original input image of AWS OCR to get absolute image coordinates.
    Output file will reference the image file under `Page/@imageFilename`
    with its full path. (So you may want to use a relative path.)

    Amazon Documentation: https://docs.aws.amazon.com/textract/latest/dg/how-it-works-document-layout.html


    AWS PAGE block is mapped to to TextRegion.
    AWS LINE block is mapped to to TextLine.
    AWS WORD block is mapped to to Word.

    Arguments:
        json_path (str): path to input JSON file
        img_path (str): path to input JPEG file
        out_path (str): path to output XML file
        preserve_reading_order (boolean): preserve reading order  of lines as indicated by Textract
    """

    pil_img = Image.open(img_path)
    now = datetime.now()
    pc_gts_type = PcGtsType(
        Metadata=MetadataType(
            Creator="OCR-D/core %s" % VERSION, Created=now, LastChange=now
        )
    )
    pagexml_page = PageType(
        imageWidth=pil_img.width,
        imageHeight=pil_img.height,
        imageFilename=img_path,
    )
    pc_gts_type.set_Page(pagexml_page)

    ordered_group = None
    if preserve_reading_order:
        # set up ReadingOrder
        reading_order = ReadingOrderType(
            OrderedGroup=OrderedGroupType(
                id="textract_reading_order",
                comments="Reading order of lines as defined by Textract."
            )
        )
        ordered_group = reading_order.get_OrderedGroup()
        pagexml_page.set_ReadingOrder(reading_order)

    json_file = open(json_path, "r")
    aws_json = json.load(json_file)
    json_file.close()

    page_block, line_blocks, word_blocks = {}, {}, {}

    for block in aws_json["Blocks"]:
        if block["BlockType"] == "PAGE":
            assert not page_block, "page must not have more than 1 PAGE block"
            page_block = block
        if block["BlockType"] == "LINE":
            line_blocks[block["Id"]] = block
        if block["BlockType"] == "WORD":
            word_blocks[block["Id"]] = block

    if "Polygon" in page_block["Geometry"]:
        awsgeometry = TextractPolygon(page_block["Geometry"]["Polygon"])
    else:
        awsgeometry = TextractBoundingBox(page_block["Geometry"]["BoundingBox"])
    # TextRegion from PAGE-block
    # pagexml_text_region = TextRegionType(
    #     Coords=CoordsType(
    #         points=points_from_awsgeometry(awsgeometry,
    #                                        pil_img.width,
    #                                        pil_img.height)
    #     ),
    #     id=f'region-{page_block["Id"]}',
    # )
    # if "Text" in page_block:
    #     pagexml_text_region.add_TextEquiv(TextEquivType(Unicode=page_block["Text"]))
    # pagexml_page.add_TextRegion(pagexml_text_region)

    # AWS-Documentation: PAGE, LINE, and WORD blocks are related to each
    # other in a  parent-to-child relationship.

    # TextLine from LINE blocks that are listed in the PAGE-block's
    # child relationships
    reading_order_index = 0
    for line_block_id in next((rel.get("Ids", [])
                               for rel in page_block.get("Relationships", [])
                               if rel["Type"] == "CHILD"), []):
        if line_block_id not in line_blocks:
            continue
        line_block = line_blocks[line_block_id]
        if "Polygon" in line_block["Geometry"]:
            awsgeometry = TextractPolygon(line_block["Geometry"]["Polygon"])
        else:
            awsgeometry = TextractBoundingBox(line_block["Geometry"]["BoundingBox"])

        # wrap lines in separate TextRegions to preserve reading order
        # (ReadingOrder references TextRegions)
        line_region_id = f'line-region-{line_block["Id"]}'
        pagexml_text_region_line = TextRegionType(
            Coords=CoordsType(
                points=points_from_awsgeometry(awsgeometry,
                                               pil_img.width,
                                               pil_img.height)
            ),
            id=line_region_id,
        )
        pagexml_page.add_TextRegion(pagexml_text_region_line)

        # append lines to text regions
        pagexml_text_line = TextLineType(
            Coords=CoordsType(
                points=points_from_awsgeometry(awsgeometry,
                                               pil_img.width,
                                               pil_img.height)
            ),
            id=f'line-{line_block["Id"]}',
        )
        if "Text" in line_block:
            pagexml_text_line.add_TextEquiv(TextEquivType(Unicode=line_block["Text"]))
        pagexml_text_region_line.add_TextLine(pagexml_text_line)

        if ordered_group:
            # store reading order
            ordered_group.add_RegionRefIndexed(
                RegionRefIndexedType(
                    index=reading_order_index,
                    regionRef=line_region_id
                )
            )
            reading_order_index += 1

        # Word from WORD blocks that are listed in the LINE-block's
        # child relationships
        for word_block_id in next((rel.get("Ids", [])
                                   for rel in line_block.get("Relationships", [])
                                   if rel["Type"] == "CHILD"), []):
            if word_block_id not in word_blocks:
                continue
            word_block = word_blocks[word_block_id]
            if "Polygon" in word_block["Geometry"]:
                awsgeometry = TextractPolygon(word_block["Geometry"]["Polygon"])
            else:
                awsgeometry = TextractBoundingBox(word_block["Geometry"]["BoundingBox"])
            pagexml_word = WordType(
                Coords=CoordsType(
                    points=points_from_awsgeometry(awsgeometry,
                                                   pil_img.width,
                                                   pil_img.height)
                ),
                id=f'word-{word_block["Id"]}',
            )
            if "Text" in word_block:
                pagexml_word.add_TextEquiv(TextEquivType(Unicode=word_block["Text"]))
            pagexml_text_line.add_Word(pagexml_word)

    result = to_xml(pc_gts_type)
    if not out_path:
        sys.stdout.write(result)
        return

    with open(out_path, "w") as f:
        f.write(result)
