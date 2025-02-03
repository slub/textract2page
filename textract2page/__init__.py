"""convert OCR results from Amazon AWS Textract (JSON) to PRImA PAGE (XML)"""

from .convert_aws import convert_file, convert_file_without_image
from ._version import version

__all__ = ['convert_file', 'convert_file_without_image']
__version__ = version
