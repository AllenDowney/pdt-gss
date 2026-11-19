PYTHON ?= python

.PHONY: build check

## Build metadata records from data/raw (see build/build_slice.py)
build:
	$(PYTHON) build/build_slice.py

## Check the records against the PublicDataToolkit standard
check:
	$(PYTHON) tests/check_metadata.py
