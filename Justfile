check:
    bash tests/test-audio-safety.sh
    bash scripts/check-audio-safety .
    python3 -m unittest discover -s tests -v
    python3 -m compileall -q scripts tests

doctor:
    ./scripts/audio-doctor --pretty

reference-tone output="/tmp/audio-reference-1khz-minus20dbfs.wav":
    ./scripts/generate-audio-reference "{{output}}" --kind tone --dbfs -20 --duration 5
