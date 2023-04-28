import argparse

from . import textract2page


def textract2page_cli():
    parser = argparse.ArgumentParser(
        description="Convert Textract output to PAGE-XML format"
    )
    parser.add_argument(
        "--json_path",
        type=str,
        default="textract.json",
        help="Path to Textract JSON input file",
    )
    parser.add_argument(
        "--img_path",
        type=str,
        default="img.jpg",
        help="Path to image input file",
    )
    parser.add_argument(
        "--out_path",
        type=str,
        default="page.xml",
        help="Path to output PAGE-XML file",
    )
    args = parser.parse_args()
    textract2page(args.json_path, args.img_path, args.out_path)
