# README

The package converts OCR results from AWS Textract JSONs into PAGE XMLs. 


## Installation

You can install the package from the Python Package Index (PyPI):

`pip install textract2page`

Or by downloading this repository:

1. Download and unzip the repository
2. Open Shell and _cd_ to unzipped repository
3. Run `pip install -e .` (in the folder that contains ```setup.py```)

## Contents

The package contains a conversion script that is provided via CLI and Python API. The script requires the Textract JSON and the original image that was used for the OCR as input (Textract JSON stores coordinates in ratios and PAGE XML in absolutes).

### Python API

```python
from textract2page import textract2page

textract2page(
    json_path="textract.json",
    img_path="img.jpg",    
    out_path="page.xml",
)

```