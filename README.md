# textract2page

> Convert AWS Textract JSON to PRImA PAGE XML


## Introduction

This software converts [Amazon AWS Textract Response](https://docs.aws.amazon.com/textract/latest/dg/how-it-works-document-layout.html)
files to [PAGE XML](https://github.com/PRImA-Research-Lab/PAGE-XML) files.

## Installation

In a Python [virtualenv](https://packaging.python.org/tutorials/installing-packages/#creating-virtual-environments):

    pip install textract2page

## Usage

To convert a Textract file `example.json` for an image file `example.jpg` to a PAGE `example.xml`:

    textract2page example.json example.jpg > example.xml

You can get a list of options with `--help` or `-h`

## Testing

(not yet)
