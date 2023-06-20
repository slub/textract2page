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
    UnorderedGroupType,
    RegionRefIndexedType,
    RolesType,
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
        self.id = aws_block.get("Id")
        self.geometry = build_aws_geomerty(aws_block.get("Geometry"))
        self.confidence = float(aws_block.get("Confidence"))


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
        textract_words: dict,
    ) -> None:
        super().__init__(aws_block=aws_table_block)
        # an aws table is either structured or semistructured. this is
        # indicated in the values of 'EntityTypes'.
        self.structured = "STRUCTURED_TABLE" in aws_table_block.get(
            "EntityTypes", []
        )
        self.common_cells = []
        self.merged_cells = []

        for block_id in get_ids_of_child_blocks(aws_table_block):
            if block_id in aws_cell_blocks.keys():
                self.common_cells.append(
                    TextractCommonCell(
                        aws_cell_blocks[block_id], self, textract_words
                    )
                )
        for block_id in get_ids_of_child_blocks(aws_table_block):
            if block_id in aws_merged_cell_blocks.keys():
                self.merged_cells.append(
                    TextractMergedCell(
                        aws_cell_blocks[block_id], self, self.common_cells
                    )
                )

        # order cells in reading order (top-left to bottom-right)
        # (apparently, the cells are already ordered correctly as given by textract)
        # ordered_cells = sorted(
        #     self.common_cells,
        #     key=lambda cell: (cell.row_index, cell.column_index),
        # )

        self.ordered_lines = [
            line
            for lines in [cell.child_lines for cell in self.common_cells]
            for line in lines
        ]


class TextractLine(TextractBlock):
    def __init__(self, aws_line_block: dict, words: dict) -> None:
        super().__init__(aws_line_block)
        self.text = aws_line_block.get("Text")
        self.child_words = [
            words.get(id) for id in get_ids_of_child_blocks(aws_line_block)
        ]
        for word in self.child_words:
            word.parent_line = self
        self.parent_cell = None


class TextractCell(TextractBlock):
    """Cell model to handle cells detected by AWS Textract."""

    @abstractmethod
    def __init__(
        self, aws_cell_block: dict, parent_table: TextractTable
    ) -> None:
        super().__init__(aws_block=aws_cell_block)
        self.parent_table = parent_table
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

    def __init__(
        self,
        aws_cell_block: dict,
        parent_table: TextractTable,
        textract_words: dict,
    ) -> None:
        super().__init__(
            aws_cell_block=aws_cell_block, parent_table=parent_table
        )
        self.parent_merged_cell = None
        self.child_words = [
            textract_words.get(id)
            for id in get_ids_of_child_blocks(aws_cell_block)
        ]
        for word in self.child_words:
            word.parent_cell = self
        self.child_lines = []
        for word in self.child_words:
            if not word.parent_line in self.child_lines:
                self.child_lines.append(word.parent_line)
        for line in self.child_lines:
            line.parent_cell = self


class TextractMergedCell(TextractCell):
    """Cell Model for the  AWS Textract table merged cells"""

    def __init__(
        self,
        aws_cell_block: dict,
        partent_table: TextractTable,
        table_cells: dict,
    ) -> None:
        super().__init__(
            aws_cell_block=aws_cell_block, parent_table=partent_table
        )
        child_cell_ids = get_ids_of_child_blocks(aws_cell_block)
        self.child_cells = []
        for cell_block_id, cell in table_cells.items():
            if cell_block_id in child_cell_ids:
                self.child_cells.append(cell)
                cell.parent_merged_cell = self


class TextractWord(TextractBlock):
    def __init__(self, aws_word_block: dict) -> None:
        super().__init__(aws_word_block)
        self.text = aws_word_block.get("Text")
        self.parent_line = None
        self.parent_cell = None


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

    # build words
    words = {}
    for word_id, word_block in word_blocks.items():
        words[word_id] = TextractWord(word_block)

    # build lines
    lines = {}
    for line_id, line_block in line_blocks.items():
        lines[line_id] = TextractLine(line_block, words)

    # build tables

    tables = {}
    for table_id, table_block in table_blocks.items():
        tables[table_id] = TextractTable(
            table_block,
            cell_blocks,
            merged_cell_blocks,
            table_title_blocks,
            table_footer_blocks,
            words,
        )

    # build PRIMAPageXML
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

    reading_order = None
    ordered_group = None
    unordered_group = None
    if preserve_reading_order:
        unordered_group = UnorderedGroupType(id="reading_order")
        # set up ReadingOrder

        ordered_group = OrderedGroupType(
            id="line_reading_order",
            comments="Reading order of lines as defined by Textract.",
        )
        unordered_group.add_OrderedGroup(ordered_group)

    tables_metadata = {}
    for table_id, table in tables.items():
        pagexml_table_region = TableRegionType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    table.geometry, pil_img.width, pil_img.height
                )
            ),
            id=f"table-region-{table_id}",
        )
        pagexml_page.add_TableRegion(pagexml_table_region)
        tables_metadata[table_id] = {
            "page_xml_representation": pagexml_table_region,
            "reading_order_index_of_last_line_before_table": None,
        }

    # build pageXML lines
    # line reading order is given by order of line keys in dict
    reading_order_index = 0

    for line_id, line in lines.items():
        # wrap lines in separate TextRegions to preserve reading order
        # (ReadingOrder references TextRegions)
        line_region_id = f"line-region-{line_id}"
        pagexml_text_region_line = TextRegionType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    line.geometry, pil_img.width, pil_img.height
                )
            ),
            id=line_region_id,
        )

        # if line has parent cell, it is part of a table
        if line.parent_cell:
            table = line.parent_cell.parent_table
            tables_metadata[table.id]["page_xml_representation"].add_TextRegion(
                pagexml_text_region_line
            )

            pagexml_table_cell_role = TableCellRoleType(
                rowIndex=line.parent_cell.row_index,
                columnIndex=line.parent_cell.column_index,
                rowSpan=line.parent_cell.row_span,
                colSpan=line.parent_cell.column_span,
                header=line.parent_cell.column_header,
            )
            pagexml_roles_type = RolesType(
                TableCellRole=pagexml_table_cell_role
            )
            pagexml_text_region_line.set_Roles(pagexml_roles_type)
            # store reading order index of last line when encountering a table
            # the first time
            if not tables_metadata[table.id][
                "reading_order_index_of_last_line_before_table"
            ]:
                tables_metadata[table.id][
                    "reading_order_index_of_last_line_before_table"
                ] = (reading_order_index - 1)
        else:
            pagexml_page.add_TextRegion(pagexml_text_region_line)

        # append lines to text regions
        pagexml_text_line = TextLineType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    line.geometry, pil_img.width, pil_img.height
                )
            ),
            id=f'line-{line_block["Id"]}',
        )
        if line.text:
            pagexml_text_line.add_TextEquiv(TextEquivType(Unicode=line.text))
        pagexml_text_region_line.add_TextLine(pagexml_text_line)

        if preserve_reading_order and not line.parent_cell:
            # store reading order
            ordered_group.add_RegionRefIndexed(
                RegionRefIndexedType(
                    index=reading_order_index, regionRef=line_region_id
                )
            )
            reading_order_index += 1

        # build pagexml words
        for word in line.child_words:
            pagexml_word = WordType(
                Coords=CoordsType(
                    points=points_from_aws_geometry(
                        word.geometry, pil_img.width, pil_img.height
                    )
                ),
                id=f'word-{word_block["Id"]}',
            )
            if word.text:
                pagexml_word.add_TextEquiv(TextEquivType(Unicode=word.text))
            pagexml_text_line.add_Word(pagexml_word)

    if preserve_reading_order:
        for table_id, table in tables.items():
            table_ordered_group = OrderedGroupType(
                id=f"table_reading_order_{table_id}",
                comments="Reading order of table.",
            )
            table_reading_order_index = 0
            for line in table.ordered_lines:
                table_ordered_group.add_RegionRefIndexed(
                    RegionRefIndexedType(
                        index=table_reading_order_index,
                        regionRef=f"line-region-{line.id}",
                    )
                )
                table_reading_order_index += 1

            unordered_group.add_OrderedGroup(table_ordered_group)

    if preserve_reading_order:
        reading_order = ReadingOrderType(UnorderedGroup=unordered_group)
        pagexml_page.set_ReadingOrder(reading_order)
    result = to_xml(page_content_type)
    if not out_path:
        sys.stdout.write(result)
        return

    with open(out_path, "w") as f:
        f.write(result)
