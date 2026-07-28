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

whale-doctor:
    python3 scripts/whale_live.py doctor

whale-demo output="/tmp/buckelwal-live-voice-v1-demo.wav":
    python3 scripts/whale_live.py demo "{{output}}"

whale-start:
    python3 scripts/whale_live.py start

whale-status:
    python3 scripts/whale_live.py status

whale-stop:
    python3 scripts/whale_live.py stop

whale-toggle:
    python3 scripts/whale_live.py toggle

whale-realistic:
    python3 scripts/whale_live.py mode realistic

whale-ufo:
    python3 scripts/whale_live.py mode ufo

whale-bank-build:
    python3 scripts/build_whale_sample_bank.py

whale-install-controls:
    python3 scripts/install_whale_desktop_controls.py
