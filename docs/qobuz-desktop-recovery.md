# Qobuz ALSA-Direct desktop recovery

`audio-qobuz-desktop-recovery-v1.service` is a narrow repair observer for one
known failure: after Qobuz releases the MOTU M2 ALSA playback PCM, the physical
device remains present but its PipeWire sink does not return.

The observer does not query qbzd. In particular, qbzd `playback_state` and
`device_open` are not recovery evidence. It preserves the Qobuz ALSA-Direct
route, track-native rate behavior, and `reserve_dac_while_running=off`.

Before restarting anything, every observation must pass:

- physical discovery starts from every `/proc/asound/cardN` sysfs device and
  requires exactly one whose resolved USB parent inside `/sys/devices` has
  `idVendor=07fd` and `idProduct=0008`; its `/proc/asound/cardN/usbid` must also
  be `07fd:0008`;
- that USB parent has a required readable serial, and the canonical sysfs path
  has an unambiguous final PCI controller BDF plus the complete USB port chain,
  configuration, and interface needed to derive the exact PipeWire bus path;
  ALSA card IDs are not identity evidence, so duplicate physical M2s remain
  ambiguous and fail closed whether ALSA names them `M2`, `M2_1`, or otherwise;
- every MOTU-looking PipeWire candidate is complete and binds to that physical
  serial and the mandatory full PCI-controller + USB-chain bus identity. Any
  sink on the exact normalized sysfs-derived `device.bus_path` is automatically
  MOTU-looking even when its other identity metadata is missing or malformed.
  Its name, vendor, product, mandatory serial, and full bus path must all match
  exactly; suffix matches, another PCI controller, a moved port, and malformed
  paths are all rejected. A missing, malformed, swapped-serial, or bus-mismatched
  candidate is **ambiguous**, not evidence that the expected sink is absent;
- `pipewire.service`, `pipewire-pulse.service`, and `wireplumber.service` are
  active;
- the readable PipeWire/Pulse sink inventory has no exact physical-MOTU sink.

An armed attempt temporarily stops only the repository-owned
`audio-control-level-observer-v1.service`. That service normally keeps MOTU
capture open for meters, so stopping it is necessary to distinguish the meter
from a real recorder. After the stop is read back as inactive, **every playback
and capture PCM substream** must be present in two identical snapshots with
`hw_params=closed` and `status=closed`. If capture stays open, another capture
client or a real recording may exist: recovery aborts without restarting
WirePlumber. The level observer is started and read back active in the cleanup
path after success, failure, or an exception.
The close gate allows a bounded seven-observation grace period for PipeWire's
normal source-suspend delay; it never stops any other client. A capture stream
that remains open after that grace period is treated as a recording and blocks
the attempt.
A private `0600` quiesce marker is persisted before the stop and removed only
after the observer is read back active. If the recovery process is killed and
cannot run its `finally` block, its next service start repairs the marked
observer before making any recovery observation.

Only after physical identity, exact-sink absence, and the all-PCM-closed
predicate have been observed does the service durably set `handoff_pending`.
After all state writes, service checks, and quiescing are complete, it repeats
the physical/service/sink gates and performs one final stable, double
all-playback-and-capture closed observation. The WirePlumber restart call is
the immediately following effect.

Because that restart is global, every non-MOTU ALSA PCM is also enumerated from
`/proc/asound/card*/pcm*/sub*/status` in two identical snapshots before the
level observer is quiesced and again at the final restart edge. Only the exact
status `CLOSED` permits recovery. Active playback or capture and any missing,
unreadable, changing, or unknown status defer recovery without stopping or
mutating the other stream.

Only `wireplumber.service` is restarted. PipeWire and pipewire-pulse are never
restarted. Bounded readback must then find exactly one serial-bound MOTU sink.
Before that exact sink is set to software unity, unmuted, and made the desktop
default, capture must still be closed. Playback may be closed or have stable
`owner_pid` evidence whose `/proc/PID/exe` is exactly `/usr/bin/pipewire`; this
accepts a waiting desktop stream that legitimately acquired the recreated sink
without accepting a direct Qobuz/ALSA reacquisition. Identity and PCM ownership
are rebound immediately before the default change. A final inventory, volume,
mute, default-sink, and ownership readback must agree.

The versioned state file lives in systemd's private user `StateDirectory`
(`~/.local/state/audio-qobuz-desktop-recovery`, directory mode `0700`, file mode
`0600`). It persists exponential failure backoff (capped at 15 minutes), the
two-minute success cooldown, and the physical-serial-bound `handoff_pending`
intent across service restarts and UI deployments. If an armed exact sink
reappears naturally, the same safe handoff gate restores it and clears pending.
Without pending, a healthy sink remains a no-op, so an intentional alternate
desktop default is not overwritten.

There is no atomic exclusion primitive for arbitrary non-cooperating ALSA
clients. Such a client can still open in the sub-call interval between the last
`/proc/asound` read and `systemctl restart`, or between an ownership read and a
subsequent `pactl` effect. The observer minimizes those windows and fails closed
when the next read sees the race; it does not claim to eliminate them.

The revision-bound audio-control deployer installs the unit and explicitly
reads back that it is loaded and active on every supporting-release convergence,
including unchanged releases. The existing UI unit owns its lifecycle with
`Wants=` and `PartOf=`, so a deployed UI start also starts the recovery observer
and a UI stop stops it.

There is one unavoidable introduction seam: the first timer invocation that
fetches a release containing this unit is still the old in-memory deployer and
cannot know the new runtime-file mapping. That first receipt does **not** prove
recovery installation. The immediately following timer/manual invocation runs
the newly installed deployer, reconciles the missing unit, starts it, and fails
its own deployment rather than writing a successful receipt unless the unit is
loaded and active. Introduction operations therefore require a final readback
from that new-deployer pass.

Before switching `current`, the deployer arms rollback containment for any
candidate that supports recovery. On failed deployment rollback, any recovery
unit exposed to the failed release—including an auto-restart during runtime
installation or an implicit start through the UI's `Wants=`—is raw-read,
explicitly stopped, and read back inactive before runtime files or the release
pointer are restored or removed. The deployer then daemon-reloads and restores
the release/UI lifecycle. If the restored release supports recovery, the
deployer explicitly starts it when needed and reads it back loaded and active;
a legacy release without the recovery contract requires no recovery activation.
