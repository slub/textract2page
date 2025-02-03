# textract2page

> Convert AWS Textract JSON to PRImA PAGE XML

[![PyPI Release](https://img.shields.io/pypi/v/textract2page.svg)](https://pypi.org/project/textract2page/)
[![CI Tests](https://github.com/rue-a/textract2page/actions/workflows/test.yml/badge.svg)](https://github.com/rue-a/textract2page/actions/workflows/test.yml)

## Introduction

This software converts OCR results from
[Amazon AWS Textract Response](https://docs.aws.amazon.com/textract/latest/dg/how-it-works-document-layout.html)
files to [PRImA PAGE XML](https://github.com/PRImA-Research-Lab/PAGE-XML) files.

## Installation

In a Python [virtualenv](https://packaging.python.org/tutorials/installing-packages/#creating-virtual-environments):

    pip install textract2page

## Usage

The package contains a file-based conversion function provided as CLI and Python API.
The function takes the Textract JSON file and the original image file which was used
as input for the OCR. (That is necessary because Textract stores coordinates in
`float` ratios, whereas PAGE uses `int` in pixel indices.)

### Python API

To convert a Textract file `example.json` for an image file `example.jpg` to a PAGE `example.xml`:

```python
from textract2page import convert_file

convert_file("example.json", "example.jpg", "example.xml")
```

Alternatively, if you do not have access to the image file, 
but do know its pixel resolution, use:

```python
from textract2page import convert_file_without_image

convert_file_without_image("example.json",
    # just give it a name (will not be read):
    "example.jpg",
    # set image width so PAGE coordinates will be correct:
    2135,
    # set image width so PAGE coordinates will be correct:
    3240,
    "example.xml")
```


### CLI

Analogously, on the command line interface:

    # with image file
    textract2page example.json example.jpg > example.xml
    textract2page -O example.xml example.json example.jpg
    # without image file (just its path name)
    textract2page --image-width 2135 --image-height 3240 example.json example.jpg > example.xml
    textract2page --image-width 2135 --image-height 3240 -O example.xml example.json example.jpg

You can get a list of options with `--help` or `-h`

## Testing

Requires installation and a local copy of the repository.

To run regression tests with `pytest`, do

    make deps-test
    make test-api

To run regression test via command line, do

    # optionally:
    sudo apt-get install xmlstarlet
    make test-cli

(If `xmlstarlet` is available, then the CLI test will
also validate the result tree. Otherwise, this just
checks the command completes without error.)
