# pdt-gss

Metadata for the [General Social Survey](https://gss.norc.org/) (GSS), built to the [PublicDataToolkit](https://github.com/AllenDowney/PublicDataToolkit) standard.

**Status:** a 45-variable slice of the 1972-2024 cumulative file, Release 3a, built for the PublicDataToolkit spike. It is not the full GSS yet, and the generated site, Python API and LLM files don't exist yet.

## What the dataset is

The GSS is a repeated cross-sectional survey of US adults, run by NORC at the University of Chicago since 1972. It asks about attitudes, behaviors and attributes. From 1988 on, many questions are asked of a random part of the sample only (ballots A, B, C and D).

## Terms of use

The GSS codebook may be used, copied, modified and distributed free of charge, provided NORC's notice is included; it is in [`NOTICE.md`](NOTICE.md). The metadata here is derived from it and from [gssrdoc](https://github.com/kjhealy/gssrdoc). The terms, as reviewed, are in [`dataset.yaml`](dataset.yaml).

## How to get the data

The data isn't in this repository. Download the cumulative Stata file from NORC; [`sources.yaml`](sources.yaml) gives the steps and checksums. A GitHub release of the data is planned.

The data release covers the current release, 7224r3a, only. Both releases keep their records, links and views. `make data` writes `data/processed/gss-7224r3a.parquet` in the common format; `make data RELEASES=7224r3` writes the earlier release, from its own Stata file. Run either under a memory cap: for the full release, `make data` takes about 50 minutes and up to about 5 GB.

## The metadata

- [`metadata/releases/7224r3a/variables.jsonl`](metadata/releases/7224r3a/variables.jsonl): one record per variable: label, question text, codes with typed missing values, years and ballots asked, and where each fact came from.
- [`metadata/groups.jsonl`](metadata/groups.jsonl): batteries.
- [`metadata/corrections.jsonl`](metadata/corrections.jsonl): known errors in the documentation, with evidence.
- [`metadata/annotations/`](metadata/annotations/), [`metadata/derived/`](metadata/derived/): subject tags and scale signatures.

The record format is defined in the [PublicDataToolkit standard](https://github.com/AllenDowney/PublicDataToolkit/blob/main/STANDARD.md).

## How to cite

Cite the GSS as NORC asks (the `citation` in [`dataset.yaml`](dataset.yaml)), and gssrdoc if you use its text.

## How to rebuild

With the sources in `data/raw/` and a PublicDataToolkit checkout next to this repo:

```bash
make build
make check
```

`build/report.md` lists what the build checked against the data.
