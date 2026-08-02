check:
    bash tests/test-audio-safety.sh
    bash scripts/check-audio-safety .
    python3 scripts/audio_control.py check
    ./scripts/audio-product-model check
    python3 -m unittest discover -s tests -v
    python3 -m compileall -q scripts tests

product-model-check:
    ./scripts/audio-product-model check

product-workspace-validate workspace="profiles/audiozentrale-workspace.example.v1.json":
    ./scripts/audio-product-model validate "{{workspace}}"

doctor:
    ./scripts/audio-doctor --pretty

truth output="${XDG_STATE_HOME:-$HOME/.local/state}/audio/truth/latest.v1.json":
    ./scripts/audio-truth capture --output "{{output}}"

truth-verify report="${XDG_STATE_HOME:-$HOME/.local/state}/audio/truth/latest.v1.json":
    ./scripts/audio-truth verify "{{report}}"

truth-drift before after output="${XDG_STATE_HOME:-$HOME/.local/state}/audio/truth/drift.v1.json":
    ./scripts/audio-truth compare "{{before}}" "{{after}}" --output "{{output}}"

reference-tone output="/tmp/audio-reference-1khz-minus20dbfs.wav":
    ./scripts/generate-audio-reference "{{output}}" --kind tone --dbfs -20 --duration 5

physical-status:
    ./scripts/audio-physical status

plan profile="desktop-mixed":
    ./scripts/audio-plan "{{profile}}"

transition-diff profile="desktop-mixed":
    ./scripts/audio-transition diff "{{profile}}"

transition-apply plan_sha256 profile="desktop-mixed":
    ./scripts/audio-transition apply "{{profile}}" --plan-sha256 "{{plan_sha256}}"

transition-status:
    ./scripts/audio-transition status

transition-recover:
    ./scripts/audio-transition recover

transition-rollback operation_id:
    ./scripts/audio-transition rollback --operation-id "{{operation_id}}"

transition-rollback-latest:
    ./scripts/audio-transition rollback

calibration-pack pack output:
    ./scripts/create-calibration-pack "{{pack}}" "{{output}}"

level wav:
    ./scripts/analyze-audio-level "{{wav}}"

record-init:
    ./scripts/audio-record init

record-plan name session_type="voice-recording" maximum_seconds="1800":
    ./scripts/audio-record plan "{{name}}" --session-type "{{session_type}}" --maximum-seconds "{{maximum_seconds}}"

record-start name plan_sha256 session_type="voice-recording" maximum_seconds="1800":
    ./scripts/audio-record start "{{name}}" --session-type "{{session_type}}" --maximum-seconds "{{maximum_seconds}}" --expected-plan-sha256 "{{plan_sha256}}"

record-status:
    ./scripts/audio-record status

record-stop:
    ./scripts/audio-record stop

record-recover:
    ./scripts/audio-record recover

production-mix-init:
    ./scripts/audio-production-mix init

production-mix-plan:
    ./scripts/audio-production-mix plan

production-mix-start plan_sha256:
    ./scripts/audio-production-mix start --expected-plan-sha256 "{{plan_sha256}}"

production-mix-status:
    ./scripts/audio-production-mix status

production-mix-stop:
    ./scripts/audio-production-mix stop

production-mix-recover:
    ./scripts/audio-production-mix recover

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

whale-morph:
    python3 scripts/whale_live.py mode morph

whale-organic:
    python3 scripts/whale_live.py mode organic

whale-realistic:
    python3 scripts/whale_live.py mode realistic

whale-ufo:
    python3 scripts/whale_live.py mode ufo

whale-bank-build:
    python3 scripts/build_whale_sample_bank.py

whale-morph-bank-build:
    python3 scripts/build_whale_morph_bank.py

whale-voice-model-build:
    python3 scripts/build_whale_voice_model.py

whale-voice-model-check:
    python3 scripts/build_whale_voice_model.py --check

whale-voice-model-evaluate engine="organic" output="/tmp/buckelwal-voice-model-cross-validation.json":
    python3 scripts/evaluate_whale_voice_model.py --engine "{{engine}}" --output "{{output}}"

whale-voice-model-evaluate-external engine="organic" output="/tmp/buckelwal-voice-model-external-evaluation.json":
    python3 scripts/evaluate_whale_voice_model.py --engine "{{engine}}" --external --output "{{output}}"

whale-install-controls:
    python3 scripts/install_whale_desktop_controls.py

control-check:
    python3 scripts/audio_control.py check

control-serve port="8765":
    ./scripts/audio-control serve --port "{{port}}"

control-start port="8765":
    ./scripts/audio-control start --port "{{port}}"

control-status:
    ./scripts/audio-control status

control-stop:
    ./scripts/audio-control stop


control-deploy-install expected_commit="":
    python3 scripts/install_audio_control_autodeploy.py {{ if expected_commit != "" { "--expected-commit " + expected_commit } else { "" } }}

control-deploy-sync:
    python3 "${HOME}/.local/libexec/audio-control-deploy.py" sync

control-deploy-status:
    python3 "${HOME}/.local/libexec/audio-control-deploy.py" status
