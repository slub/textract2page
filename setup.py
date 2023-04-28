# -*- coding: utf-8 -*-
"""
textract2page
-----

Convert Amazon Textract JSON files into 
Page XML files.
"""

from pathlib import Path

from setuptools import find_packages, setup

this_directory = Path(__file__).parent
long_description = (this_directory / "README.rst").read_text()

setup(
    name="textract2page",
    author="Arne Rümmler",
    author_email="arne.ruemmler@gmail.com",
    version="0.0.1",
    description="Convert Amazon Textract JSON files into Page XML files.",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    url="https://github.com/rue-a/textract2page",
    packages=find_packages(".", exclude=["tests", "tests.*"]),
    classifiers=[
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.10.6",
        "Operating System :: OS Independent",
        "License :: OSI Approved :: MIT License",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires=">=3.10.6",
)
