PYTHON ?= python
# A PublicDataToolkit checkout. Or `pip install -e $(PDT)` and run `pdt check`.
PDT ?= ../PublicDataToolkit

.PHONY: build views check

## Build metadata records and links from data/raw (build/build_slice.py, build/build_links.py)
build:
	$(PYTHON) build/build_slice.py
	$(PYTHON) build/build_links.py

## Generate agent/ and docs/ from the records (pdt views)
views:
	PYTHONPATH=$(PDT)/lib $(PYTHON) -m pdt views .

## Check the repo against the PublicDataToolkit standard (pdt check)
check:
	PYTHONPATH=$(PDT)/lib $(PYTHON) -m pdt check .
