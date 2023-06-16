import json
import math
import sys
from typing import List, Dict, Tuple
from dataclasses import dataclass
from functools import singledispatch
from datetime import datetime
from PIL import Image
from abc import ABC, abstractmethod


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
    RegionRefIndexedType,
    TableRegionType,
    TableCellRoleType,
)
from ocrd_models.ocrd_page import to_xml


class TextractGeometry(ABC):
    pass


@dataclass
class TextractPoint(TextractGeometry):
    x: float
    y: float

    def __post_init__(self):
        assert 0 <= self.x <= 1, self
        assert 0 <= self.y <= 1, self


@dataclass
class TextractBoundingBox(TextractGeometry):
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
class TextractPolygon(TextractGeometry):
    points: List[TextractPoint]

    def __init__(self, polygon: List[Dict[str, float]]):
        self.points = [
            TextractPoint(point.get("X", -1), point.get("Y", -1))
            for point in polygon
        ]
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


class TextractBlock(ABC):
    """Generic Textract BLOCK"""

    @abstractmethod
    def __init__(self, aws_block: dict) -> None:
        self.id = aws_block["Id"]
        self.geometry = build_aws_geomerty(aws_block["Geometry"])
        self.confidence = float(aws_block["Confidence"])

    def get_id(self) -> str:
        return self.id

    def get_geometry(self) -> TextractGeometry:
        return self.geometry

    def get_confidence(self) -> float:
        return self.confidence


class TextractTable(TextractBlock):
    """Table model to handle tables detected by AWS Textract.

    AWS table model:
    TABLE --> CELLS --> WORDS
    TABLE --> MERGED_CELLS --> CELLS --> WORDS
    TABLE --> TABLE_TITLE --> WORDS
    TABLE --> TABLE_FOOTER --> WORDS

    The CELLS that are childs of MERGED_CELLS are also childs
    of the TABLE. So they appear twice.

    Addionally, each WORD is part of a LINE. However, LINES are
    only present in the overall AWS model:

    PAGE --> LINES --> WORDS
    PAGE --> TABLES --> <...>
    """

    def __init__(
        self,
        aws_table_block: dict,
        aws_cell_blocks: dict,
        aws_merged_cell_blocks: dict,
        aws_table_title_blocks: dict,
        aws_table_footer_blocks: dict,
        aws_line_blocks: dict,
    ) -> None:
        super().__init__(aws_block=aws_table_block)
        # an aws table is either structured or semistructured. this is
        # indicated in the values of 'EntityTypes'.
        self.structured = "STRUCTURED_TABLE" in aws_table_block.get(
            "EntityTypes", []
        )
        (self.common_cells, self.merged_cells) = ([], [])

        for block_id in get_ids_of_child_blocks(aws_table_block):
            if block_id in aws_cell_blocks.keys():
                self.common_cells.append(
                    TextractCommonCell(
                        aws_cell_blocks[block_id], aws_line_blocks
                    )
                )
        for block_id in get_ids_of_child_blocks(aws_table_block):
            if block_id in aws_merged_cell_blocks.keys():
                self.merged_cells.append(
                    TextractMergedCell(
                        aws_cell_blocks[block_id], self.common_cells
                    )
                )

        # order cells in reading order (top-left to bottom-right)
        # (apparently, the cell are already ordered correctly as given by textract)
        # ordered_cells = sorted(
        #     self.common_cells,
        #     key=lambda cell: (cell.row_index, cell.column_index),
        # )

        self.ordered_line_ids = [
            line
            for lines in [cell.get_line_ids() for cell in self.common_cells]
            for line in lines
        ]
        print(self.ordered_line_ids)

    def get_ordered_line_ids(self) -> List[str]:
        return self.ordered_line_ids


class TextractCell(TextractBlock):
    """Cell model to handle cells detected by AWS Textract."""

    @abstractmethod
    def __init__(self, aws_cell_block: dict) -> None:
        super().__init__(aws_block=aws_cell_block)
        self.row_index = int(aws_cell_block["RowIndex"])
        self.column_index = int(aws_cell_block["ColumnIndex"])
        self.row_span = int(aws_cell_block["RowSpan"])
        self.column_span = int(aws_cell_block["ColumnSpan"])
        self.column_header = "COLUMN_HEADER" in aws_cell_block.get(
            "EntityTypes", []
        )
        self.table_title = "TABLE_TITLE" in aws_cell_block.get(
            "EntityTypes", []
        )
        self.table_footer = "TABLE_FOOTER" in aws_cell_block.get(
            "EntityTypes", []
        )
        self.table_section_title = "TABLE_SECTION_TITLE" in aws_cell_block.get(
            "EntityTypes", []
        )
        self.table_summary = "TABLE_SUMMARY" in aws_cell_block.get(
            "EntityTypes", []
        )

    def get_row_index(self) -> int:
        return self.row_index

    def get_col_index(self) -> int:
        return self.column_index

    def get_cell_types(self) -> List[str]:
        types = []
        if self.table_footer:
            types.append("table_footer")
        if self.table_title:
            types.append("table_title")
        if self.table_section_title:
            types.append("section_title")
        if self.table_summary:
            types.append("table_summary")
        if self.column_header:
            types.append("column_header")
        return types


class TextractCommonCell(TextractCell):
    """Cell Model for the  AWS Textract table cells"""

    def __init__(self, aws_cell_block: dict, aws_line_blocks: dict) -> None:
        super().__init__(aws_cell_block=aws_cell_block)
        self.child_word_ids = get_ids_of_child_blocks(aws_cell_block)
        self.child_line_ids = []
        for line_block_id, line_block in aws_line_blocks.items():
            if not set(self.child_word_ids).isdisjoint(
                get_ids_of_child_blocks(line_block)
            ):
                self.child_line_ids.append(line_block_id)

    def get_line_ids(self) -> List[str]:
        return self.child_line_ids


class TextractMergedCell(TextractCell):
    """Cell Model for the  AWS Textract table merged cells"""

    def __init__(self, aws_cell_block: dict, table_cells: dict) -> None:
        super().__init__(aws_cell_block=aws_cell_block)
        child_cell_ids = get_ids_of_child_blocks(aws_cell_block)
        self.child_cells = []
        for cell_block_id, cell in table_cells.items():
            if cell_block_id in child_cell_ids:
                self.child_cells.append(cell)


@singledispatch
def points_from_aws_geometry(textract_geom, page_width, page_height):
    """Convert a Textract geometry into a string of points, which are
    scaled to the image width and height."""

    raise NotImplementedError(
        f"Cannot process this type of data ({type(textract_geom)})"
    )


@points_from_aws_geometry.register
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


@points_from_aws_geometry.register
def _(textract_geom: TextractPolygon, page_width: int, page_height: int) -> str:
    """Convert a TextractPolygon into a string of points."""

    points = " ".join(
        f"{math.ceil(point.x * page_width)},{math.ceil(point.y * page_height)}"
        for point in textract_geom.points
    )

    return points


def build_aws_geomerty(aws_block_geometry: dict) -> TextractGeometry:
    geometry = None
    if "Polygon" in aws_block_geometry:
        geometry = TextractPolygon(aws_block_geometry["Polygon"])
    else:
        geometry = TextractBoundingBox(aws_block_geometry["BoundingBox"])
    return geometry


def get_ids_of_child_blocks(aws_block: dict) -> List[str]:
    """Searches a AWS-Textract-BLOCK for Relationsships of the type CHILD
    and returns a list of the CHILD-Ids, or an empty list otherwise.

    Arguments:
        aws_block (dict): following AWS-Textract-BLOCK structure (https://docs.aws.amazon.com/textract/latest/dg/API_Block.html)
    Returns:
        A list of AWS-Textract-BLOCK Ids (can be empty).
    """
    if not any(
        rel.get("Type") == "CHILD" for rel in aws_block.get("Relationships", [])
    ):
        return []

    child_block_ids = [
        rel.get("Ids", [])
        for rel in aws_block.get("Relationships", [])
        if rel["Type"] == "CHILD"
    ][0]
    return child_block_ids


def convert_file(
    json_path: str,
    img_path: str,
    out_path: str,
    preserve_reading_order: bool = True,
) -> None:
    """Convert an AWS-Textract-JSON file to a PAGE-XML file.

    Also requires the original input image of AWS OCR to get absolute image coordinates.
    Output file will reference the image file under `Page/@imageFilename`
    with its full path. (So you may want to use a relative path.)

    Amazon Documentation: https://docs.aws.amazon.com/textract/latest/dg/how-it-works-document-layout.html


    AWS PAGE BLOCK is mapped to to TextRegion.
    AWS LINE BLOCK is mapped to to TextLine.
    AWS WORD BLOCK is mapped to to Word.

    Arguments:
        json_path (str): path to input JSON file
        img_path (str): path to input JPEG file
        out_path (str): path to output XML file
        preserve_reading_order (boolean): preserve reading order  of lines as indicated by Textract
    """

    pil_img = Image.open(img_path)
    now = datetime.now()
    page_content_type = PcGtsType(
        Metadata=MetadataType(
            Creator="OCR-D/core %s" % VERSION, Created=now, LastChange=now
        )
    )
    pagexml_page = PageType(
        imageWidth=pil_img.width,
        imageHeight=pil_img.height,
        imageFilename=img_path,
    )
    page_content_type.set_Page(pagexml_page)

    ordered_group = None
    if preserve_reading_order:
        # set up ReadingOrder
        reading_order = ReadingOrderType(
            OrderedGroup=OrderedGroupType(
                id="textract_reading_order",
                comments="Reading order of lines as defined by Textract.",
            )
        )
        ordered_group = reading_order.get_OrderedGroup()
        pagexml_page.set_ReadingOrder(reading_order)

    json_file = open(json_path, "r")
    aws_json = json.load(json_file)
    json_file.close()

    # build dicts for different textract block types

    (
        page_block,
        line_blocks,
        word_blocks,
        table_blocks,
        cell_blocks,
        merged_cell_blocks,
        table_title_blocks,
        table_footer_blocks,
    ) = (
        {},
        {},
        {},
        {},
        {},
        {},
        {},
        {},
    )
    for block in aws_json["Blocks"]:
        if block["BlockType"] == "PAGE":
            assert not page_block, "page must not have more than 1 PAGE block"
            page_block = block
        if block["BlockType"] == "LINE":
            line_blocks[block["Id"]] = block
        if block["BlockType"] == "WORD":
            word_blocks[block["Id"]] = block
        if block["BlockType"] == "TABLE":
            table_blocks[block["Id"]] = block
        if block["BlockType"] == "CELL":
            cell_blocks[block["Id"]] = block
        if block["BlockType"] == "MERGED_CELL":
            merged_cell_blocks[block["Id"]] = block
        if block["BlockType"] == "TABLE_TITLE":
            table_title_blocks[block["Id"]] = block
        if block["BlockType"] == "TABLE_FOOTER":
            table_footer_blocks[block["Id"]] = block

    # build tables
    tables = []
    table_id_pagexml_references = {}
    for table_block in table_blocks.values():
        table = TextractTable(
            table_block,
            cell_blocks,
            merged_cell_blocks,
            table_title_blocks,
            table_footer_blocks,
            line_blocks,
        )
        tables.append(table)

        pagexml_table_region = TableRegionType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    table.get_geometry(), pil_img.width, pil_img.height
                )
            ),
            id=f"table-region-{table.get_id()}",
        )
        pagexml_page.add_TableRegion(pagexml_table_region)
        table_id_pagexml_references[table.get_id()] = pagexml_table_region

    reading_order_index = 0
    for line_block_id in line_blocks.keys():
        line_block = line_blocks[line_block_id]
        line_geometry = build_aws_geomerty(line_block["Geometry"])

        # wrap lines in separate TextRegions to preserve reading order
        # (ReadingOrder references TextRegions)
        line_region_id = f'line-region-{line_block["Id"]}'
        pagexml_text_region_line = TextRegionType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    line_geometry, pil_img.width, pil_img.height
                )
            ),
            id=line_region_id,
        )
        line_in_table = any(
            line_block_id in table.get_ordered_line_ids() for table in tables
        )
        if line_in_table:
            table_id_pagexml_references[table.get_id()].add_TextRegion(
                pagexml_text_region_line
            )
        else:
            pagexml_page.add_TextRegion(pagexml_text_region_line)

        # append lines to text regions
        pagexml_text_line = TextLineType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    line_geometry, pil_img.width, pil_img.height
                )
            ),
            id=f'line-{line_block["Id"]}',
        )
        if "Text" in line_block:
            pagexml_text_line.add_TextEquiv(
                TextEquivType(Unicode=line_block["Text"])
            )
        pagexml_text_region_line.add_TextLine(pagexml_text_line)

        if ordered_group:
            # store reading order
            ordered_group.add_RegionRefIndexed(
                RegionRefIndexedType(
                    index=reading_order_index, regionRef=line_region_id
                )
            )
            reading_order_index += 1

        # Word from WORD blocks that are listed in the LINE-block's
        # child relationships
        for word_block_id in get_ids_of_child_blocks(line_block):
            if word_block_id not in word_blocks:
                continue
            word_block = word_blocks[word_block_id]
            word_geometry = build_aws_geomerty(word_block["Geometry"])

            pagexml_word = WordType(
                Coords=CoordsType(
                    points=points_from_aws_geometry(
                        word_geometry, pil_img.width, pil_img.height
                    )
                ),
                id=f'word-{word_block["Id"]}',
            )
            if "Text" in word_block:
                pagexml_word.add_TextEquiv(
                    TextEquivType(Unicode=word_block["Text"])
                )
            pagexml_text_line.add_Word(pagexml_word)

    result = to_xml(page_content_type)
    if not out_path:
        sys.stdout.write(result)
        return

    with open(out_path, "w") as f:
        f.write(result)
