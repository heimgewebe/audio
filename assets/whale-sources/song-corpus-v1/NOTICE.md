# Humpback whale song corpus v1 — source and licence

This directory contains a repository-local copy of the **Raven Pro selection tables only** from:

- Elena Schall, Javier Oña, Judith Denkinger (2024), *Humpback Whale Song Recordings Ecuador 2012-2019*, Figshare, DOI `10.6084/m9.figshare.25259947.v1`.
- Associated study: Javier Oña, Judith Denkinger, Elena Schall (2025), *Acoustic richness and composition changes of humpback whale (Megaptera novaeangliae) songs on breeding grounds off the coast of Ecuador*, Marine Mammal Science, DOI `10.1111/mms.13208`.

The Figshare dataset declares **CC BY 4.0**. The copied `.txt` files remain under that licence and attribution. `source-manifest.json` binds each copied selection table to the published Figshare file id, byte size and MD5 plus a repository-computed SHA-256. Paired WAV metadata is recorded, but the approximately 6.13 GB audio collection is intentionally not vendored into Git.

## What the annotations establish

The associated study states that the Raven tables contain manually logged **phrase windows**. The two-letter prefix of a Category identifies the phrase type. A following numeric repetition code is preserved verbatim, but this repository does **not** independently decode that suffix into unit sequences or counts.

Why: the authors' public analysis repository contains a phrase catalogue and special multi-digit decoding rules, but its MATLAB source explicitly marks the phrase-to-unit transcription path as not yet fixed for correct unit sequence reconstruction. No explicit licence was observed for that analysis repository, so neither its source code nor its phrase catalogue is copied here. It is used only as a revisions-bound method cross-check; `source-manifest.json` records the inspected commit.

Normalized records therefore distinguish:

- `observed`: phrase start/end, duration, frequency bounds and source category;
- `parsed_without_unit_decoding`: two-letter phrase identity and the untouched numeric suffix;
- `published_summary`: song count, median theme sequence and mean units per song copied from the peer-reviewed study table;
- `unknown`: individual unit timestamps, per-phrase unit sequence/count and individual song boundaries inside each released Raven table.

## Frozen split

- Development / parameter-estimation years: **2012–2016**.
- Held-out evaluation years: **2017–2019**.

The later years are never used to derive grammar recommendations. This temporal split deliberately tests generalization across documented song evolution/revolution instead of evaluating on the same material used to tune parameters.

## Excluded source

The Allen et al. 2002–2014 transcription workbook (Dryad DOI `10.5061/dryad.69161bg`) was inspected during source discovery but is **not used or committed here**. Although repository landing pages expose open-licence metadata, the workbook itself contains a more restrictive reuse notice for new studies. The stricter embedded notice is respected.
