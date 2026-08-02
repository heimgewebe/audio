# NOAA-PMEL external evaluation v2

Retrieved on 2 August 2026 from the NOAA Pacific Marine Environmental
Laboratory Acoustics Program multimedia page.

NOAA PMEL states that, unless copyrighted or otherwise noted, information on
its Acoustics Program pages is public information and may be distributed freely
with NOAA PMEL attribution. These files are used only for external evaluation;
they do not enter model building, runtime selection, or live synthesis.

## Bound raw recordings

| File | Description | Source | SHA-256 |
|---|---|---|---|
| `raw/HB-ship-SBNMS.wav` | Humpback vocalizations, Stellwagen Bank National Marine Sanctuary, with ship noise | `https://www.pmel.noaa.gov/acoustics/multimedia/HB-ship-SBNMS.wav` | `24bf234e0d302ca91fd3e31f3b964185244403f74666569b753ca12080b59750` |
| `raw/HB-ship-AMSNP.wav` | Humpback vocalizations, National Marine Sanctuary of American Samoa, with snapping shrimp | `https://www.pmel.noaa.gov/acoustics/multimedia/HB-ship-AMSNP.wav` | `2a6c7035808ae31576146d561e4ca08aea77f0851e4212530974cd6abd0bd0a1` |

Source and rights page:
`https://www.pmel.noaa.gov/acoustics/multimedia.html`

## Derivatives

`scripts/build_whale_external_evaluation_v2.py` deterministically selects four
fixed, non-overlapping two-second intervals from each recording, resamples them
to mono PCM16 at 48 kHz with a 32-tap Lanczos-windowed sinc interpolator and
applies 20 ms boundary fades that reach exact zero. It performs no
normalization, denoising, content filtering, listening-based selection, or
engine-result-based selection.

The pre-merge linear-interpolation derivatives and their scores were invalidated
after external review identified spectral imaging, especially for the 5 kHz
American-Samoa source. Raw bytes, segment boundaries, the frozen candidate,
engine parameters, and the distance model were not changed.

All interval definitions and derivative hashes are recorded in `manifest.json`.
