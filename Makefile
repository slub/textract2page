SHELL := bash
PYTHON := python3

help:
	@echo
	@echo " Targets"
	@echo
	@echo "   install    Install in the current Python environment"
	@echo "   deps-test  Install extra dependency for testing"
	@echo "   test-api   Run the API tests (using pytest)"
	@echo "   test-cli   Run the CLI tests (optionally using xmlstarlet)"
	@echo "   test       Run both kinds of tests"
	@echo "   build      Create source and binary pkgs under dist/"
	@echo "   publish    Upload pkgs from dist/ to PyPI"
	@echo
	@echo " Variables"
	@echo
	@echo "   PYTEST_ARGS Additional arguments for pytest [$(PYTEST_ARGS)]"
	@echo "   PYTHON      Name of the Python binary [$(PYTHON)]"

install:
	$(PYTHON) -m pip install .

deps-test:
	$(PYTHON) -m pip install pytest lxml

test: test-api test-cli

test-api:
	$(PYTHON) -m pytest $(PYTEST_ARGS) tests

test-cli: OUT != mktemp -u
test-cli: UNDATED := xmlstarlet ed -N pc=http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15 -d /pc:PcGts/pc:Metadata/* -d "/pc:PcGts/pc:Page/@imageFilename"
test-cli: test-cli-with-image test-cli-without-image
	if command -v xmlstarlet &> /dev/null; then diff -u <($(UNDATED) tests/workspace/reference_page_xml/18xx-Missio-EMU-0042.xml) <($(UNDATED) $(OUT)); fi
	@-$(RM) $(OUT)

test-cli-with-image:
	cd tests/workspace; textract2page -O $(OUT) textract_responses/18xx-Missio-EMU-0042.json images/18xx-Missio-EMU-0042.jpg

test-cli-without-image: OPTS != identify -format "--image-width %w --image-height %h" tests/workspace/images/18xx-Missio-EMU-0042.jpg
test-cli-without-image:
	cd tests/workspace; textract2page -O $(OUT) $(OPTS) textract_responses/18xx-Missio-EMU-0042.json images/18xx-Missio-EMU-0042.jpg

build:
	$(PYTHON) -m pip build

publish:
	twine check dist/*
	twine upload dist/*


.PHONY: help install deps-test test test-api test-cli build publish
