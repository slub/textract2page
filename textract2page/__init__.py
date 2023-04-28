"""convert OCR results from Amazon AWS Textract (JSON) to PRImA PAGE (XML)"""

from .convert_aws import convert_file

__all__ = ['convert_file']
