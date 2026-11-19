#!/usr/bin/env Rscript
# Convert gssrdoc's gss_doc table to JSON for build/build_slice.py.
#
# Usage: Rscript build/convert_gssrdoc.R <gss_doc.rda> <output.json>
#
# Needs only base R and jsonlite (the pdt-xlang conda env has both). The output
# has the shape CultureWar's codebook/convert_gssrdoc_to_json.R wrote for
# gssrdoc_full.json: a JSON array with one single-element array per variable,
# whose nested tables (var_yrtab, yrballot_df, module_df, subject_df) are JSON
# strings in *_json fields. The rda is the data file from gssrdoc's repository
# at the commit pinned in sources.yaml; loading it runs no package code.

library(jsonlite)

args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 2)
env <- new.env()
load(args[1], envir = env)
doc <- as.data.frame(env$gss_doc)

nested_json <- function(col, skip_character = FALSE) {
  vapply(col, function(x) {
    if (is.null(x) || (skip_character && is.character(x))) return(NA_character_)
    as.character(toJSON(as.data.frame(x), auto_unbox = TRUE))
  }, character(1))
}

out <- data.frame(
  variable = doc$variable,
  description = doc$description,
  question = doc$question,
  value_labels = unname(doc$value_labels),
  var_yrtab_json = nested_json(doc$var_yrtab, skip_character = TRUE),
  yrballot_df_json = nested_json(doc$yrballot_df),
  module_df_json = nested_json(doc$module_df),
  subject_df_json = nested_json(doc$subject_df),
  norc_id = doc$norc_id,
  norc_url = doc$norc_url,
  stringsAsFactors = FALSE
)

rows <- split(out, seq_len(nrow(out)))
names(rows) <- NULL
write_json(rows, args[2], auto_unbox = TRUE, na = "null")
cat("wrote", nrow(out), "variables to", args[2], "\n")
