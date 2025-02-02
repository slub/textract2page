"""Convert an AWS Textract response to PRIMA Page XML."""

import json
import math
import sys
import warnings
from typing import List, Dict
from dataclasses import dataclass
from functools import singledispatch
from datetime import datetime
from abc import ABC, abstractmethod
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
    OrderedGroupIndexedType,
    UnorderedGroupIndexedType,
    RegionRefIndexedType,
    RegionRefType,
    RolesType,
    TableRegionType,
    TableCellRoleType,
    ImageRegionType,
)
from ocrd_models.ocrd_page import to_xml


TEXT_TYPE_MAP = {"PRINTED": "printed", "HANDWRITING": "handwritten-cursive"}
LAYOUT_TYPE_MAP = {
    "LAYOUT_TITLE": "heading",
    "LAYOUT_HEADER": "header",
    "LAYOUT_FOOTER": "footer",
    "LAYOUT_SECTION_HEADER": "heading",
    "LAYOUT_PAGE_NUMBER": "page-number",
    "LAYOUT_LIST": "other",
    "LAYOUT_FIGURE": "other",
    "LAYOUT_TABLE": "other",
    "LAYOUT_KEY_VALUE_SET": "other",
    "LAYOUT_TEXT": "paragraph",
}


class TextractGeometry(ABC):
    """Abstract geometry class."""


@dataclass
class TextractPoint(TextractGeometry):
    """Point class for creation of geometries."""

    x: float
    y: float

    def __post_init__(self):
        assert 0 <= self.x <= 1, self
        assert 0 <= self.y <= 1, self


@dataclass
class TextractBoundingBox(TextractGeometry):
    """Bounding box class to handle bounding box geometries detected by AWS Textract."""

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
    """Polygon class to handle polygon geometries detected by AWS Textract."""

    points: List[TextractPoint]

    def __init__(self, polygon: List[Dict[str, float]]):
        self.points = [
            TextractPoint(point.get("X", -1), point.get("Y", -1)) for point in polygon
        ]
        self.__post_init__()

    def __post_init__(self):
        assert len(self.points) >= 3, len(self.points)

    def get_bounding_box(self) -> TextractBoundingBox:
        """Return a TextractBoundingBox object for this polygon geometry."""
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
    """Generic Textract block"""

    @abstractmethod
    def __init__(self, aws_block: Dict) -> None:
        self.id = aws_block.get("Id")
        self.prefix = "textract"
        self.geometry = build_aws_geometry(aws_block.get("Geometry"))
        self.confidence = float(aws_block.get("Confidence")) / 100


class TextractLayout(TextractBlock):
    """Base class for Textract Layout objects.

    From AmazonTextractDocs:
    https://docs.aws.amazon.com/textract/latest/dg/layoutresponse.html

    Each element returns two key pieces of information. First is the bounding
    box of the layout element, which shows its location. Second, the element
    contains a list of IDs. These IDs point to the components of the layout
    element, often lines of text represented by LINE objects. Layout elements
    can also point to different objects, such as TABLE objects, Key-Value pairs,
    or LAYOUT_TEXT elements in the case of LAYOUT_LIST.

    Elements are returned in implied reading order. This means layout elements
    will be returned by document analysis left to right, top to bottom. For
    multicolumn pages, elements are returned from the top of the leftmost column,
    moving left to  right until the bottom of the column is reached. Then, the
    elements from the next leftmost column are returned in the same way.
    """

    def __init__(
        self, aws_layout_block: Dict,
        aws_top_blocks: Dict,
        textract_words: Dict,
        textract_lines: Dict,
    ) -> None:
        super().__init__(aws_block=aws_layout_block)
        # Textract layout types -> Page layout types

        self.page_layout_type = LAYOUT_TYPE_MAP.get(aws_layout_block["BlockType"], 'floating')
        self.textract_layout_type = aws_layout_block["BlockType"]
        self.prefix = (
            f"{self.prefix}-{self.textract_layout_type.lower().replace('_','-')}"
        )

        child_words = [
            textract_words.get(id)
            for id in get_ids_of_child_blocks(aws_layout_block)
            if textract_words.get(id)
        ]
        for word in child_words:
            word.parent_layout = self

        self.child_lines = [
            textract_lines.get(id)
            for id in get_ids_of_child_blocks(aws_layout_block)
            if textract_lines.get(id)
        ]
        for word in child_words:
            if not word.parent_line in self.child_lines:
                self.child_lines.append(word.parent_line)

        for line in self.child_lines:
            line.parent_layout = self

        self.child_regions = [
            aws_top_blocks.get(id)
            for id in get_ids_of_child_blocks(aws_layout_block)
            if aws_top_blocks.get(id)
        ]
        # layout child blocks must be replaced by child instances later
        # child instances must be connected to parent later
        self.parent_layout = None


class TextractTable(TextractBlock):
    """Table class to handle tables detected by AWS Textract.

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
        aws_table_block: Dict,
        aws_cell_blocks: Dict,
        aws_merged_cell_blocks: Dict,
        aws_table_title_blocks: Dict,
        aws_table_footer_blocks: Dict,
        aws_selection_element_blocks: Dict,
        textract_words: Dict,
    ) -> None:
        super().__init__(aws_block=aws_table_block)
        # an aws table is either structured or semistructured. this is
        # indicated in the values of 'EntityTypes'.

        self.prefix = f"{self.prefix}-table"
        self.structured = "STRUCTURED_TABLE" in aws_table_block.get("EntityTypes", [])
        self.common_cells = []
        self.merged_cells = []

        self.common_cells = [
            TextractCommonCell(
                aws_cell_blocks[id],
                self,
                aws_selection_element_blocks,
                textract_words,
            )
            for id in get_ids_of_child_blocks(aws_table_block)
            if aws_cell_blocks.get(id)
        ]
        self.merged_cells = [
            TextractMergedCell(
                aws_merged_cell_blocks[id],
                self,
            )
            for id in get_ids_of_child_blocks(aws_table_block)
            if aws_merged_cell_blocks.get(id)
        ]

        self.ordered_lines = [
            line
            for lines in [cell.child_lines for cell in self.common_cells]
            for line in lines
        ]

        # store row and col nb
        row_indices = []
        col_indices = []
        for cell in self.common_cells:
            row_indices.append(cell.row_index)
            col_indices.append(cell.column_index)
        self.rows = max(row_indices) + 1
        self.columns = max(col_indices) + 1
        self.parent_layout = None


class TextractLine(TextractBlock):
    """Line class to handle lines detected by AWS Textract."""

    def __init__(
        self,
        aws_line_block: Dict,
        textract_words: Dict,
    ) -> None:
        super().__init__(aws_block=aws_line_block)
        self.prefix = f"{self.prefix}-line"
        self.text = aws_line_block.get("Text")
        self.child_words = [
            textract_words.get(id) for id in get_ids_of_child_blocks(aws_line_block)
        ]
        for word in self.child_words:
            word.parent_line = self
        self.parent_cell = None
        self.parent_layout = None
        self.parent_value = None
        self.parent_key = None


class TextractCell(TextractBlock):
    """Cell class to handle cells detected by AWS Textract."""

    @abstractmethod
    def __init__(self, aws_cell_block: Dict, parent_table: TextractTable) -> None:
        super().__init__(aws_block=aws_cell_block)
        self.parent_table = parent_table
        self.row_index = int(aws_cell_block["RowIndex"]) - 1
        self.column_index = int(aws_cell_block["ColumnIndex"]) - 1
        self.row_span = int(aws_cell_block["RowSpan"])
        self.column_span = int(aws_cell_block["ColumnSpan"])
        self.column_header = "COLUMN_HEADER" in aws_cell_block.get("EntityTypes", [])
        self.table_title = "TABLE_TITLE" in aws_cell_block.get("EntityTypes", [])
        self.table_footer = "TABLE_FOOTER" in aws_cell_block.get("EntityTypes", [])
        self.table_section_title = "TABLE_SECTION_TITLE" in aws_cell_block.get(
            "EntityTypes", []
        )
        self.table_summary = "TABLE_SUMMARY" in aws_cell_block.get("EntityTypes", [])

    def get_cell_types(self) -> List[str]:
        """Get all types of this cell as a list. Possible types: table title,
        table footer, section title, table summery, and column header."""
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
    """Cell class for the  AWS Textract table cells."""

    def __init__(
        self,
        aws_cell_block: Dict,
        parent_table: TextractTable,
        aws_selection_element_blocks: Dict,
        textract_words: Dict,
    ) -> None:
        super().__init__(aws_cell_block=aws_cell_block, parent_table=parent_table)
        self.prefix = f"{self.prefix}-cell"
        self.parent_merged_cell = None

        # build child-parent relationships
        self.child_words = [
            textract_words.get(id)
            for id in get_ids_of_child_blocks(aws_cell_block)
            if textract_words.get(id)
        ]
        for word in self.child_words:
            word.parent_cell = self

        self.child_lines = []
        for word in self.child_words:
            if not word.parent_line in self.child_lines:
                self.child_lines.append(word.parent_line)
        for line in self.child_lines:
            line.parent_cell = self

        # this is probably 1 element at max. Docs don't state
        # this clearly though.
        self.child_selection_elements = [
            TextractSelectionElement(
                aws_selection_element_blocks.get(id), parent_cell=self
            )
            for id in get_ids_of_child_blocks(aws_cell_block)
            if aws_selection_element_blocks.get(id)
        ]


class TextractMergedCell(TextractCell):
    """Cell class for the  AWS Textract table merged cells."""

    def __init__(
        self,
        aws_cell_block: Dict,
        parent_table: TextractTable,
    ) -> None:
        super().__init__(aws_cell_block=aws_cell_block, parent_table=parent_table)
        self.prefix = f"{self.prefix}-merged-cell"
        child_cell_ids = get_ids_of_child_blocks(aws_cell_block)

        self.child_cells = []
        for cell_block_id in child_cell_ids:
            for cell in parent_table.common_cells:
                if cell.id == cell_block_id:
                    self.child_cells.append(cell)
                    cell.parent_merged_cell = self

        self.child_words = [
            word for child_cell in self.child_cells for word in child_cell.child_words
        ]

        self.child_lines = [
            line for child_cell in self.child_cells for line in child_cell.child_lines
        ]

        self.child_selection_elements = [
            selection_element
            for child_cell in self.child_cells
            for selection_element in child_cell.child_selection_elements
        ]


class TextractWord(TextractBlock):
    """Word class for the  AWS Textract words."""

    def __init__(
        self,
        aws_word_block: Dict,
    ) -> None:
        super().__init__(aws_block=aws_word_block)
        self.prefix = f"{self.prefix}-word"
        self.text = aws_word_block.get("Text")
        self.text_type = TEXT_TYPE_MAP.get(aws_word_block.get("TextType"))
        self.parent_line = None
        self.parent_cell = None
        self.parent_layout = None
        self.parent_value = None
        self.parent_key = None


class TextractValue(TextractBlock):
    """
    Both Textract Key and Textract Value are modeled as a KEY_VALUE_SET-
    BlockType in the AWS Textract JSON response. The differentiation is
    done via the value in EntityTypes.

    https://docs.aws.amazon.com/textract/latest/dg/how-it-works-kvp.html"""

    def __init__(
        self,
        aws_key_value_set_block: Dict,
        aws_selection_element_blocks: Dict,
        textract_words: Dict,
    ) -> None:
        super().__init__(aws_block=aws_key_value_set_block)
        if not "VALUE" in aws_key_value_set_block.get("EntityTypes", []):
            raise ValueError("The provided textract block is no VALUE block.")
        self.prefix = f"{self.prefix}-value"

        # this is probably 1 element at max. Docs don't state
        # this clearly though.
        self.child_selection_elements = [
            TextractSelectionElement(
                aws_selection_element_blocks.get(id), parent_value=self
            )
            for id in get_ids_of_child_blocks(aws_key_value_set_block)
            if aws_selection_element_blocks.get(id)
        ]
        self.associated_key = None

        # build child-parent relationships
        self.child_words = [
            textract_words.get(id)
            for id in get_ids_of_child_blocks(aws_key_value_set_block)
            if textract_words.get(id)
        ]
        for word in self.child_words:
            word.parent_value = self

        self.child_lines = []
        for word in self.child_words:
            if not word.parent_line in self.child_lines:
                self.child_lines.append(word.parent_line)
        for line in self.child_lines:
            line.parent_value = self
        # self.parent_layout = None


class TextractKey(TextractBlock):
    """
    Both Textract Key and Textract Value are modeled as a KEY_VALUE_SET-
    BlockType in the AWS Textract JSON response. The differentiation is
    done via the value in EntityTypes.

    https://docs.aws.amazon.com/textract/latest/dg/how-it-works-kvp.html"""

    def __init__(
        self,
        aws_key_value_set_block: Dict,
        textract_values: Dict,
        textract_words: Dict,
    ) -> None:
        super().__init__(aws_block=aws_key_value_set_block)
        if not "KEY" in aws_key_value_set_block.get("EntityTypes", []):
            raise ValueError("The provided textract block is no KEY block.")
        self.prefix = f"{self.prefix}-key"
        self.child_words = [
            textract_words.get(id)
            for id in get_ids_of_child_blocks(aws_key_value_set_block)
            if textract_words.get(id)
        ]
        associated_value_ids = []
        if any(
            rel.get("Type") == "VALUE"
            for rel in aws_key_value_set_block.get("Relationships", [])
        ):
            associated_value_ids = [
                rel.get("Ids", [])
                for rel in aws_key_value_set_block.get("Relationships", [])
                if rel["Type"] == "VALUE"
            ][0]
        self.associated_values = [
            textract_values.get(id) for id in associated_value_ids
        ]
        for value in self.associated_values:
            value.associated_key = self

        # build child-parent relationships
        self.child_words = [
            textract_words.get(id)
            for id in get_ids_of_child_blocks(aws_key_value_set_block)
            if textract_words.get(id)
        ]
        for word in self.child_words:
            word.parent_key = self

        self.child_lines = []
        for word in self.child_words:
            if not word.parent_line in self.child_lines:
                self.child_lines.append(word.parent_line)
        for line in self.child_lines:
            line.parent_key = self
        # self.parent_layout = None


class TextractSelectionElement(TextractBlock):
    """Models a Textract selection element block

    can be detecten in key-val-sets and tables

    https://docs.aws.amazon.com/textract/latest/dg/how-it-works-selectables.html
    """

    def __init__(
        self,
        aws_selection_element_block: Dict,
        parent_cell: TextractCommonCell = None,
        parent_value: TextractValue = None,
    ) -> None:
        super().__init__(aws_selection_element_block)
        self.prefix = f"{self.prefix}-selection-element"
        self.selected = False
        if aws_selection_element_block.get("SelectionStatus") == "SELECTED":
            self.selected = True
        self.parent_cell = None
        self.parent_value = None

        if parent_cell:
            self.parent_cell = parent_cell
        if parent_value:
            self.parent_value = parent_value


@singledispatch
def points_from_aws_geometry(textract_geom, page_width, page_height):
    """Convert a Textract geometry into a string of points, which are
    scaled to the image width and height."""

    raise NotImplementedError(
        f"Cannot process this type of data ({type(textract_geom)})"
    )


@points_from_aws_geometry.register
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


@points_from_aws_geometry.register
def _(textract_geom: TextractPolygon, page_width: int, page_height: int) -> str:
    """Convert a TextractPolygon into a string of points."""

    points = " ".join(
        f"{math.ceil(point.x * page_width)},{math.ceil(point.y * page_height)}"
        for point in textract_geom.points
    )

    return points


def build_aws_geometry(aws_block_geometry: Dict) -> TextractGeometry:
    """Build polygon geometry if given in the AWS Textract response, or the
    bounding box geometry otherwise."""

    geometry = None
    if "Polygon" in aws_block_geometry:
        geometry = TextractPolygon(aws_block_geometry["Polygon"])
    else:
        geometry = TextractBoundingBox(aws_block_geometry["BoundingBox"])
    return geometry


def get_ids_of_child_blocks(aws_block: Dict) -> List[str]:
    """Searches a AWS-Textract-BLOCK for Relationsships of the type CHILD
    and returns a list of the CHILD-Ids, or an empty list otherwise.

    Arguments:
        aws_block (dict): following AWS-Textract-BLOCK structure
            (https://docs.aws.amazon.com/textract/latest/dg/API_Block.html)
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


def derive_reading_order(word_list: List[TextractWord]):
    """
    The reading order of the objects within an AWS Textract response is
    ultimately given by the order of the word blocks in the response.

    Each word belongs either to a specific line, cell, value, key
    or layout object. Among these, the cases value, key and layout object
    can be considered top level in terms of the reading order. Each cell
    belongs to a table, which then is the top-level reading order object.
    Lines however are a special case: they mostly belong to one of the
    top-level reading order objects, but sometimes can also be a top level
    themselves. This results in two checks for each word:

    - Does the word belong to a line? 
      * And if so: Does the line belong to another top-level object
        (table, key, value, layout)?
      * Otherwise: to which top-level object does it belong?

    With these checks in place, we iterate through all words and collect
    the respective top-level objects in reading order.

    As of my understanding, words can not be top level objects, i.e. always
    stay in a child relation to some other object of the Textract response.
    """

    top_level_objects_in_reading_order = []
    for word in word_list:
        if word.parent_line:
            complex_line_parent = next(
                (
                    parent
                    for parent in [
                        (
                            word.parent_line.parent_cell.parent_table
                            if word.parent_line.parent_cell
                            else None
                        ),
                        word.parent_line.parent_value,
                        word.parent_line.parent_key,
                        word.parent_line.parent_layout,
                    ]
                    if parent
                ),
                False,
            )
            if complex_line_parent:
                if complex_line_parent not in top_level_objects_in_reading_order:
                    top_level_objects_in_reading_order.append(complex_line_parent)

        complex_word_parent = next(
            (
                parent
                for parent in [
                    (word.parent_cell.parent_table if word.parent_cell else None),
                    word.parent_value,
                    word.parent_key,
                    word.parent_layout,
                ]
                if parent
            ),
            False,
        )

        if complex_word_parent:
            if complex_word_parent not in top_level_objects_in_reading_order:
                top_level_objects_in_reading_order.append(complex_word_parent)

    return top_level_objects_in_reading_order


def convert_file(json_path: str, img_path: str, out_path: str) -> None:
    """Convert an AWS-Textract-JSON file to a PAGE-XML file.

    Also requires the original input image of AWS OCR to get absolute image coordinates.
    Output file will reference the image file under `Page/@imageFilename`
    with its full path. (So you may want to use a relative path.)

    Amazon Documentation: https://docs.aws.amazon.com/textract/latest/dg/how-it-works-document-layout.html

    Arguments:
        json_path (str): path to input JSON file
        img_path (str): path to input image file
        out_path (str): path to output XML file
    """
    
    # get absolute image coordinates
    pil_img = Image.open(img_path)
    img_width = pil_img.width
    img_height = pil_img.height
    pil_img.close()
    
    convert_file_without_image(json_path, img_path, img_width, img_height, out_path)


def convert_file_without_image(json_path: str, img_path: str, img_width: int, img_height: int, out_path: str) -> None:
    """Convert an AWS-Textract-JSON file to a PAGE-XML file, without the original input image.

    Also requires the original input image used for AWS OCR, but only to reference it under 
    `Page/@imageFilename` with its full path – does not actually require an existing file under that path.
    Instead, this additionally requires the absolute dimensions (pixel resolution) of the image.

    Amazon Documentation: https://docs.aws.amazon.com/textract/latest/dg/how-it-works-document-layout.html

    Arguments:
        json_path (str): path to input JSON file
        img_path (str): filename of input image file
        img_width (int): width of image in pixels
        img_height (int): height of image in pixels
        out_path (str): path to output XML file
    """

    print(f"beginning converting {json_path}")
    json_file = open(json_path, "r", encoding="utf-8")
    aws_json = json.load(json_file)
    json_file.close()

    # --------------------------------------------------------------------------
    # setup: read textract blocks

    (
        page_block,
        line_blocks,
        word_blocks,
        table_blocks,
        cell_blocks,
        merged_cell_blocks,
        table_title_blocks,
        table_footer_blocks,
        selection_element_blocks,
        key_value_set_blocks,
        layout_blocks,
    ) = ({}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {})
    block_order = {}
    for order, block in enumerate(aws_json["Blocks"]):
        block_order[block["Id"]] = order
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
        if block["BlockType"] == "SELECTION_ELEMENT":
            selection_element_blocks[block["Id"]] = block
        if block["BlockType"] == "KEY_VALUE_SET":
            key_value_set_blocks[block["Id"]] = block
        # we handle layout somewhat different
        if block["BlockType"].startswith("LAYOUT_"):
            layout_blocks[block["Id"]] = block

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
            selection_element_blocks,
            words,
        )

    # build values
    values = {}
    for key_value_set_id, key_value_set in key_value_set_blocks.items():
        if "VALUE" in key_value_set.get("EntityTypes", []):
            values[key_value_set_id] = TextractValue(
                key_value_set, selection_element_blocks, words
            )

    # build keys
    keys = {}
    for key_value_set_id, key_value_set in key_value_set_blocks.items():
        if "KEY" in key_value_set.get("EntityTypes", []):
            keys[key_value_set_id] = TextractKey(key_value_set, values, words)

    # build layouts
    layouts = {}
    for layout_id, layout_block in layout_blocks.items():
        layouts[layout_id] = TextractLayout(
            layout_block,
            # recursive layout_blocks must be replaced after
            # all top-level layout_blocks have been instantiated
            dict(layout_blocks, **tables, **keys, **values),
            words,
            lines,
        )

    # build recursive layouts
    for layout in list(layouts.values()):
        for i, child in enumerate(layout.child_regions):
            if isinstance(child, dict):
                child_id = child["Id"]
                assert child_id in layouts
                layout.child_regions[i] = layouts[child_id]
                layouts[child_id].parent_layout = layout
                # avoid instantiating twice:
                del layouts[child_id]
            elif child.id in tables:
                tables[child.id].parent_layout = layout
                # avoid instantiating twice:
                del tables[child.id]
            # elif child.id in keys:
            #     keys[child.id].parent_layout = layout
            #     # avoid instantiating twice:
            #     del keys[child.id]
            # elif child.id in values:
            #     values[child.id].parent_layout = layout
            #     # avoid instantiating twice:
            #     del values[child.id]

    # build dummy lines for dangling words
    for word in words.values():
        # if word is part of a line do nothing here
        if word.parent_line:
            continue

        # if word is part of a table do nothing here
        if word.parent_cell:
            continue

        # if word is part of a layout do nothing here
        if word.parent_layout:
            continue

        # if word is neither part of a line, table, nor layout,
        # create dummy line around the word
        dummy_block = dict(word_blocks[word.id])
        dummy_block["Id"] = word.id + "_parent"
        dummy = TextractLine(dummy_block, {})
        dummy.child_words = [word]
        word.parent_line = dummy
        block_order[dummy.id] = block_order[word.id]
        lines.append(dummy)

    # build dummy layouts for dangling lines
    for line in lines.values():
        # if line is part of a table do nothing here
        if line.parent_cell:
            continue

        # if line is part of a layout do nothing here
        if line.parent_layout:
            continue

        # if line is neither part of a table, nor of a layout,
        # create dummy region around the line
        dummy_block = dict(line_blocks[line.id])
        dummy_block["Id"] = line.id + "_parent"
        dummy_block["BlockType"] = "LAYOUT_DUMMY"
        dummy = TextractLayout(dummy_block, {}, {}, {})
        dummy.child_lines = [line]
        line.parent_layout = dummy
        block_order[dummy.id] = block_order[line.id]
        layouts[dummy.id] = dummy

    # reading order of top-level objects
    # - derived from linear word-order (as fall-back)
    text_regions = derive_reading_order(words.values())
    # - taken from top level directly (only useful with LAYOUT results)
    if any(layouts):
        def aws_block_order(obj):
            return block_order[obj.id]
        layout_regions = sorted(layouts.values(), key=aws_block_order)
        # tables are special:
        for table in tables.values():
            layout_pos = -1
            # try to find matching LAYOUT_TABLE
            for layout in layout_regions:
                if layout.geometry == table.geometry:
                    layout_pos = layout_regions.index(layout)
                    layout_regions[layout_pos] = table
                    break
            if layout_pos > -1:
                continue
            # or re-use prior/next relations in word-based order
            text_pos = text_regions.index(table)
            if text_pos > 0:
                # insert after predecessor
                layout_pos = layout_regions.index(text_regions[text_pos - 1]) + 1
            else:
                # insert before successor
                layout_pos = layout_regions.index(text_regions[text_pos + 1]) + 1
            layout_regions = layout_regions[:layout_pos] + [table] + layout_regions[layout_pos:]
        textract_objects_in_reading_order = layout_regions
    else:
        textract_objects_in_reading_order = text_regions

    # build PRIMAPageXML
    now = datetime.now()
    page_content_type = PcGtsType(
        Metadata=MetadataType(
            Creator="OCR-D/core %s" % VERSION, Created=now, LastChange=now
        )
    )
    pagexml_page = PageType(
        imageWidth=img_width,
        imageHeight=img_height,
        imageFilename=img_path,
    )
    page_content_type.set_Page(pagexml_page)

    # build global reading order
    local_reading_orders = {}
    global_ordered_group = OrderedGroupType(
        id="global-reading-order",
        comments="Reading order as defined by Textract.",
    )
    for global_reading_order_index, textract_object in enumerate(
        textract_objects_in_reading_order
    ):
        # set up local reading orders for tables
        table = tables.get(textract_object.id, None)
        layout = layouts.get(textract_object.id, None)
        if table:
            local_reading_order = UnorderedGroupIndexedType(
                index=global_reading_order_index,
                id=f"{table.prefix}_{table.id}_reading-order",
                comments="Reading order of this table.",
                regionRef=f"{table.prefix}_{table.id}",
            )
            global_ordered_group.add_UnorderedGroupIndexed(local_reading_order)
            local_reading_orders[f"{table.prefix}_{table.id}_reading-order"] = (
                local_reading_order
            )
        elif layout and (
                layout.textract_layout_type == "LAYOUT_FIGURE" and len(layout.child_lines) or
                len(layout.child_regions)):
            local_reading_order = OrderedGroupIndexedType(
                index=global_reading_order_index,
                id=f"{layout.prefix}_{layout.id}_reading-order",
                comments="Reading order of this region.",
                regionRef=f"{layout.prefix}_{layout.id}",
            )
            global_ordered_group.add_OrderedGroupIndexed(local_reading_order)
            local_reading_orders[f"{layout.prefix}_{layout.id}_reading-order"] = (
                local_reading_order
            )
        else:
            global_ordered_group.add_RegionRefIndexed(
                RegionRefIndexedType(
                    index=global_reading_order_index,
                    regionRef=f"{textract_object.prefix}_{textract_object.id}",
                )
            )

    def instantiate_pagexml(block, parent):
        local_reading_order_index = 0
        local_block_reading_order = local_reading_orders.get(
            f"{block.prefix}_{block.id}_reading-order", None
        )

        # generic arguments
        kwargs = {'Coords':
                  CoordsType(
                      points=points_from_aws_geometry(
                          block.geometry, img_width, img_height
                      )
                  ),
                  'id': f"{block.prefix}_{block.id}",
        }

        # handle figures
        if isinstance(block, TextractLayout) and block.textract_layout_type == "LAYOUT_FIGURE":
            pagexml_img_region = ImageRegionType(
                type_=block.page_layout_type,
                custom="textract-layout-type: figure;",
                **kwargs
            )
            parent.add_ImageRegion(pagexml_img_region)

            for line in block.child_lines:
                # create a dummy text region for each line
                line_region_id = f"{line.prefix}_text-region_{line.id}"
                pagexml_line_region = TextRegionType(
                    Coords=CoordsType(
                        points=points_from_aws_geometry(
                            line.geometry, img_width, img_height
                        )
                    ),
                    id=line_region_id,
                )
                pagexml_img_region.add_TextRegion(pagexml_line_region)

                if local_block_reading_order:
                    local_block_reading_order.add_RegionRefIndexed(
                        RegionRefIndexedType(
                            index=local_reading_order_index,
                            regionRef=line_region_id,
                        )
                    )
                    local_reading_order_index += 1

                instantiate_pagexml(line, pagexml_line_region)

            assert len(block.child_regions) == 0, \
                (f"unexpected AWS layout recursion of {block.child_regions[0].textract_layout_type} "
                 f"in {block.textract_layout_type}")

            return pagexml_img_region

        # handle tables
        if isinstance(block, TextractLayout) and block.textract_layout_type == "LAYOUT_TABLE":
            # we covered tables already
            return None

        if isinstance(block, TextractLine):
            pagexml_text_line = TextLineType(**kwargs)
            if block.text:
                pagexml_text_line.add_TextEquiv(
                    TextEquivType(conf=block.confidence, Unicode=block.text)
                )
            parent.add_TextLine(pagexml_text_line)

            # build pagexml words
            for word in block.child_words:
                instantiate_pagexml(word, pagexml_text_line)
            return pagexml_text_line

        if isinstance(block, TextractWord):
            pagexml_word = WordType(production=block.text_type, **kwargs)
            if block.text:
                pagexml_word.add_TextEquiv(
                    TextEquivType(conf=block.confidence, Unicode=block.text)
                )
            parent.add_Word(pagexml_word)
            return pagexml_word

        if isinstance(block, TextractLayout) and block.textract_layout_type.startswith('LAYOUT_'):
            pagexml_text_region = TextRegionType(type_=block.page_layout_type, **kwargs)
            if block.textract_layout_type != "LAYOUT_DUMMY":
                pagexml_text_region.set_custom(
                    f"textract-layout-type: {block.textract_layout_type.split('LAYOUT_')[1].lower()};"
                )
            parent.add_TextRegion(pagexml_text_region)

            for line in block.child_lines:
                instantiate_pagexml(line, pagexml_text_region)

            for child_region in block.child_regions:
                # todo: do we need this assertion?
                assert child_region.textract_layout_type.startswith("LAYOUT_") and \
                    child_region.textract_layout_type not in ["LAYOUT_FIGURE", "LAYOUT_TABLE"], \
                    (f"unexpected AWS layout recursion of {child_region.textract_layout_type} "
                     f"in {block.textract_layout_type}")

                pagexml_child_text_region = instantiate_pagexml(child_region, pagexml_text_region)
                if local_block_reading_order:
                    local_block_reading_order.add_RegionRefIndexed(
                        RegionRefIndexedType(
                            index=local_reading_order_index,
                            regionRef=pagexml_child_text_region.id,
                        )
                    )
                    local_reading_order_index += 1
            return pagexml_text_region

        if isinstance(block, TextractTable):
            pagexml_table_region = TableRegionType(rows=block.rows, columns=block.columns, **kwargs)
            parent.add_TableRegion(pagexml_table_region)

            visited_merged_cells = []

            for cell in block.common_cells:
                merged_cell = cell.parent_merged_cell
                if merged_cell:
                    if merged_cell in visited_merged_cells:
                        continue
                    visited_merged_cells.append(merged_cell)
                    cell = merged_cell

                # create a text region for each cell
                cell_region_id = f"{cell.prefix}_text-region_{cell.id}"
                pagexml_cell_region = TextRegionType(
                    Coords=CoordsType(
                        points=points_from_aws_geometry(
                            cell.geometry, img_width, img_height
                        )
                    ),
                    id=cell_region_id,
                )
                pagexml_table_region.add_TextRegion(pagexml_cell_region)

                pagexml_table_cell_role = TableCellRoleType(
                    rowIndex=cell.row_index,
                    columnIndex=cell.column_index,
                    rowSpan=cell.row_span,
                    colSpan=cell.column_span,
                    header=cell.column_header,
                )
                pagexml_roles_type = RolesType(TableCellRole=pagexml_table_cell_role)
                pagexml_cell_region.set_Roles(pagexml_roles_type)

                local_block_reading_order.add_RegionRef(
                    RegionRefType(
                        index=local_reading_order_index,
                        regionRef=cell_region_id,
                    )
                )
                local_reading_order_index += 1

                # lines and words might span multiples cells, if this is the case
                # all cell are assigned the same line/word-text. To prevent the
                # according TextLineTypes/WordTypes to have the same IDs, each
                # id is append with the cells row and col index.
                for line in cell.child_lines:
                    # append lines to text regions

                    pagexml_text_line = TextLineType(
                        Coords=CoordsType(
                            points=points_from_aws_geometry(
                                line.geometry, img_width, img_height
                            )
                        ),
                        id=f"{line.prefix}_{line.id}-{cell.row_index}-{cell.column_index}",
                    )
                    if line.text:
                        pagexml_text_line.add_TextEquiv(
                            TextEquivType(conf=line.confidence, Unicode=line.text)
                        )
                    pagexml_cell_region.add_TextLine(pagexml_text_line)

                    # build pagexml words
                    for word in line.child_words:
                        pagexml_word = WordType(
                            Coords=CoordsType(
                                points=points_from_aws_geometry(
                                    word.geometry, img_width, img_height
                                )
                            ),
                            id=f"{word.prefix}_{word.id}-{cell.row_index}-{cell.column_index}",
                            production=word.text_type,
                        )
                        if word.text:
                            pagexml_word.add_TextEquiv(
                                TextEquivType(conf=word.confidence, Unicode=word.text)
                            )
                        pagexml_text_line.add_Word(pagexml_word)
            return pagexml_table_region

    for layout in layouts.values():
        instantiate_pagexml(layout, pagexml_page)

    for table in tables.values():
        instantiate_pagexml(table, pagexml_page)

    reading_order = ReadingOrderType(OrderedGroup=global_ordered_group)
    pagexml_page.set_ReadingOrder(reading_order)
    result = to_xml(page_content_type)

    if not out_path:
        sys.stdout.write(result)
        return

    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write(result)
        print(f"  finished writing {out_path}\n")
