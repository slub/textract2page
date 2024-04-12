"""Convert an AWS Textract response to PRIMA Page XML."""

import json
import math
import sys
from typing import List, Dict, Final
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
    UnorderedGroupIndexedType,
    RegionRefIndexedType,
    RegionRefType,
    RolesType,
    TableRegionType,
    TableCellRoleType,
    ImageRegionType,
)
from ocrd_models.ocrd_page import to_xml


text_type_map: Final = {"PRINTED": "printed", "HANDWRITING": "handwritten-cursive"}


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
    """Generic Textract BLOCK"""

    @abstractmethod
    def __init__(self, aws_block: Dict) -> None:
        self.id = aws_block.get("Id")
        self.geometry = build_aws_geometry(aws_block.get("Geometry"))
        self.confidence = float(aws_block.get("Confidence"))


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
        self, aws_layout_block: Dict, textract_words: Dict, textract_lines: Dict
    ) -> None:
        super().__init__(aws_block=aws_layout_block)
        # Textract layout types -> Page layout types
        layout_type_map: Final = {
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
        self.page_layout_type = layout_type_map.get(aws_layout_block["BlockType"])
        self.textract_layout_type = aws_layout_block["BlockType"]

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

        self.structured = "STRUCTURED_TABLE" in aws_table_block.get("EntityTypes", [])
        self.common_cells = []
        self.merged_cells = []

        # for block_id in get_ids_of_child_blocks(aws_table_block):
        for block in aws_cell_blocks.values():
            self.common_cells.append(
                TextractCommonCell(
                    block,
                    self,
                    aws_selection_element_blocks,
                    textract_words,
                )
            )

        for block in aws_merged_cell_blocks.values():
            self.merged_cells.append(TextractMergedCell(block, self))

        # (apparently, the cells are already ordered correctly as
        # given by textract, so we skip next lines.)
        # order cells in reading order (top-left to bottom-right)
        # ordered_cells = sorted(
        #     self.common_cells,
        #     key=lambda cell: (cell.row_index, cell.column_index),
        # )

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


class TextractLine(TextractBlock):
    """Line class to handle lines detected by AWS Textract."""

    def __init__(
        self,
        aws_line_block: Dict,
        textract_words: Dict,
    ) -> None:
        super().__init__(aws_block=aws_line_block)
        self.text = aws_line_block.get("Text")
        self.child_words = [
            textract_words.get(id) for id in get_ids_of_child_blocks(aws_line_block)
        ]
        for word in self.child_words:
            word.parent_line = self
        self.parent_cell = None
        self.parent_layout = None


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
        self.parent_merged_cell = None
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
        self.text = aws_word_block.get("Text")
        self.text_type = text_type_map.get(aws_word_block.get("TextType"))
        self.parent_line = None
        self.parent_cell = None
        self.parent_layout = None


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
        self.child_words = [
            textract_words.get(id)
            for id in get_ids_of_child_blocks(aws_key_value_set_block)
            if textract_words.get(id)
        ]
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


class TextractSelectionElement(TextractBlock):
    """Models a Textract selection element block
    https://docs.aws.amazon.com/textract/latest/dg/how-it-works-selectables.html
    """

    def __init__(
        self,
        aws_selection_element_block: Dict,
        parent_cell: TextractCommonCell = None,
        parent_value: TextractValue = None,
    ) -> None:
        super().__init__(aws_selection_element_block)
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

    # build layouts
    layouts = [
        TextractLayout(layout_block, words, lines)
        for layout_block in layout_blocks.values()
    ]

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

    # --------------------------------------------------------------------------
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

    global_ordered_group = None
    if preserve_reading_order:
        # set up ReadingOrder
        global_ordered_group = OrderedGroupType(
            id="line_reading_order",
            comments="Reading order of lines as defined by Textract.",
        )

    # build pageXML lines
    # line reading order is given by order of line keys in dict
    global_reading_order_index = 0
    # preserve table positions in reading order
    visited_tables = {}

    for line_id, line in lines.items():
        # if line is part of a table
        if line.parent_cell:
            parent_table = line.parent_cell.parent_table
            if (
                not (parent_table.id in visited_tables.keys())
                and preserve_reading_order
            ):

                local_reading_order = UnorderedGroupIndexedType(
                    index=global_reading_order_index,
                    id=f"table_{parent_table.id}_reading_order",
                    comments="Reading order of this table.",
                )
                global_ordered_group.add_UnorderedGroupIndexed(local_reading_order)
                visited_tables[parent_table.id] = local_reading_order
                global_reading_order_index += 1

        # if line is part of a layout do nothing here
        elif line.parent_layout:
            continue

        # if line is neither part of a table, nor of a layout, create dummy
        # region around the line
        else:
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
            pagexml_page.add_TextRegion(pagexml_text_region_line)

            # store reading order
            if preserve_reading_order:
                global_ordered_group.add_RegionRefIndexed(
                    RegionRefIndexedType(
                        index=global_reading_order_index,
                        regionRef=line_region_id,
                    )
                )
                global_reading_order_index += 1

            # append lines to text regions
            pagexml_text_line = TextLineType(
                Coords=CoordsType(
                    points=points_from_aws_geometry(
                        line.geometry, pil_img.width, pil_img.height
                    )
                ),
                id=f"line-{line_id}",
            )
            if line.text:
                pagexml_text_line.add_TextEquiv(
                    TextEquivType(conf=line.confidence, Unicode=line.text)
                )
            pagexml_text_region_line.add_TextLine(pagexml_text_line)

            # build pagexml words
            for word in line.child_words:
                pagexml_word = WordType(
                    Coords=CoordsType(
                        points=points_from_aws_geometry(
                            word.geometry, pil_img.width, pil_img.height
                        )
                    ),
                    id=f"word-{word.id}",
                    production=word.text_type,
                )
                if word.text:
                    pagexml_word.add_TextEquiv(
                        TextEquivType(conf=word.confidence, Unicode=word.text)
                    )
                pagexml_text_line.add_Word(pagexml_word)

    for layout in layouts:
        # ignore layout_type: other
        if layout.textract_layout_type == "LAYOUT_FIGURE":
            pagexml_text_region = ImageRegionType(
                Coords=CoordsType(
                    points=points_from_aws_geometry(
                        layout.geometry, pil_img.width, pil_img.height
                    )
                ),
                id=f"layout-image-region-{layout.id}",
                type_=layout.page_layout_type,
                custom=f"textract-layout-type: {layout.textract_layout_type.split('LAYOUT_')[1].lower()};",
            )
            pagexml_page.add_TextRegion(pagexml_text_region)

            if preserve_reading_order:
                global_ordered_group.add_RegionRefIndexed(
                    RegionRefIndexedType(
                        index=global_reading_order_index,
                        regionRef=f"layout-text-region-{layout.id}",
                    )
                )
                global_reading_order_index += 1
            continue
        if layout.textract_layout_type == "LAYOUT_TABLE":
            # we cover tables separatly
            continue

        pagexml_text_region = TextRegionType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    layout.geometry, pil_img.width, pil_img.height
                )
            ),
            id=f"layout-text-region-{layout.id}",
            type_=layout.page_layout_type,
            custom=f"textract-layout-type: {layout.textract_layout_type.split('LAYOUT_')[1].lower()};",
        )
        pagexml_page.add_TextRegion(pagexml_text_region)

        if preserve_reading_order:
            global_ordered_group.add_RegionRefIndexed(
                RegionRefIndexedType(
                    index=global_reading_order_index,
                    regionRef=f"layout-text-region-{layout.id}",
                )
            )
            global_reading_order_index += 1

        for line in layout.child_lines:

            pagexml_text_line = TextLineType(
                Coords=CoordsType(
                    points=points_from_aws_geometry(
                        line.geometry, pil_img.width, pil_img.height
                    )
                ),
                id=f"line-{line.id}",
            )
            if line.text:
                pagexml_text_region.add_TextEquiv(
                    TextEquivType(conf=line.confidence, Unicode=line.text)
                )
            pagexml_text_region.add_TextLine(pagexml_text_line)

            # build pagexml words
            for word in line.child_words:
                pagexml_word = WordType(
                    Coords=CoordsType(
                        points=points_from_aws_geometry(
                            word.geometry, pil_img.width, pil_img.height
                        )
                    ),
                    id=f"word-{word.id}",
                    production=word.text_type,
                )
                if word.text:
                    pagexml_word.add_TextEquiv(
                        TextEquivType(conf=word.confidence, Unicode=word.text)
                    )
                pagexml_text_line.add_Word(pagexml_word)

    for table_id, table in tables.items():
        local_reading_order_index = 0
        local_reading_order = visited_tables[table_id]

        pagexml_table_region = TableRegionType(
            Coords=CoordsType(
                points=points_from_aws_geometry(
                    table.geometry, pil_img.width, pil_img.height
                )
            ),
            id=f"table-region-{table_id}",
            rows=table.rows,
            columns=table.columns,
        )
        pagexml_page.add_TableRegion(pagexml_table_region)

        visited_merged_cells = []

        for cell in table.common_cells:
            merged_cell = cell.parent_merged_cell
            if merged_cell:
                if merged_cell in visited_merged_cells:
                    continue
                visited_merged_cells.append(merged_cell)
                cell = merged_cell

            # create a text region for each cell
            cell_region_id = f"cell-region-{cell.id}"
            pagexml_cell_region = TextRegionType(
                Coords=CoordsType(
                    points=points_from_aws_geometry(
                        cell.geometry, pil_img.width, pil_img.height
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

            # store reading order
            if preserve_reading_order:
                local_reading_order.add_RegionRef(
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
                            line.geometry, pil_img.width, pil_img.height
                        )
                    ),
                    id=f"line-{line.id}-{cell.row_index}-{cell.column_index}",
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
                                word.geometry, pil_img.width, pil_img.height
                            )
                        ),
                        id=f"word-{word.id}-{cell.row_index}-{cell.column_index}",
                        production=word.text_type,
                    )
                    if word.text:
                        pagexml_word.add_TextEquiv(
                            TextEquivType(conf=word.confidence, Unicode=word.text)
                        )
                    pagexml_text_line.add_Word(pagexml_word)

    if preserve_reading_order:
        reading_order = ReadingOrderType(OrderedGroup=global_ordered_group)
        pagexml_page.set_ReadingOrder(reading_order)
    result = to_xml(page_content_type)

    if not out_path:
        sys.stdout.write(result)
        return

    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write(result)
        print(f"  finished writing {out_path}\n")
