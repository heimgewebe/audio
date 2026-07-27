check:
    bash tests/test-audio-safety.sh
    bash scripts/check-audio-safety .
    python3 -m unittest discover -s tests -v
    python3 -m compileall -q scripts tests

doctor:
    ./scripts/audio-doctor --pretty

reference-tone output="/tmp/audio-reference-1khz-minus20dbfs.wav":
    ./scripts/generate-audio-reference "{{output}}" --kind tone --dbfs -20 --duration 5

physical-status:
    ./scripts/audio-physical status

plan profile="desktop-mixed":
    ./scripts/audio-plan "{{profile}}"

calibration-pack pack output:
    ./scripts/create-calibration-pack "{{pack}}" "{{output}}"

level wav:
    ./scripts/analyze-audio-level "{{wav}}"
