# StackChan Spring Terminal And Follow Tuning Design

## Goal

Keep the visually smoother spring motion already verified on the physical
StackChan, eliminate the observed two-degree pitch undershoot at return-home,
and identify one faster face-follow configuration without restoring visible
jerk or weakening lifecycle safety.

## Observed Failure

After the 15-second face-follow run, the configured home pose was yaw 0 and
pitch 33 degrees, while the physical readback remained yaw 0 and pitch 31
degrees. A second identical home command left the readback unchanged. The
host-side spring state had already treated the target as complete, so the
same-target optimization did not issue a recovery frame.

The spring completion path currently clears the pending final-native-write
flag while it marks the interpolation complete. The following tick therefore
cannot issue the exact target frame and clear the flag only after a successful
servo-bus acknowledgement.

## Design

### Terminal Frame

Keep `snap_on_rest` set when the spring reports completion. The completion tick
may send its last interpolated value, but the following motion tick must send
one exact native target position. Clear the flag only after that exact frame is
fresh and the servo bus acknowledges it. A retarget between the snapshot and
the bus write continues to suppress the obsolete frame through the existing
request-token gate.

### Follow-Speed Candidate

Do not change spring damping, controller gain, step size, and observation
cadence together. First prove the terminal-frame repair locally. Then use the
existing deterministic attention replay/capacity harness to compare one
variable at a time. Prefer a shorter observation interval when the recorded
camera and control capacity can sustain it; keep the current critically damped
spring and four-degree step limit as safety guardrails. Reject a candidate if
it increases failed or stale commands, post-stop dispatches, overlap, servo
limit commands, or simulated tracking error beyond the baseline.

### Device Boundary

Unattended work ends at a validated StackChan firmware artifact and a bounded
physical review procedure. A further OTA, reboot, reset, or power cycle requires
fresh explicit device authorization. Persistent Gateway and power-save
configuration are outside this tuning change.

## Verification

- Host test observes the spring reaching rest while retaining the final-frame
  flag, then observes the flag clear only after a successful exact-target
  write.
- Existing stale-frame, momentum-retention, native-step, firmware host,
  Gateway, and Pico focused tests remain green.
- Deterministic replay/capacity evidence names the single changed follow
  parameter and compares it with the current baseline.
- The next attended physical review repeats the 3-second small-lane test and
  15-second face-follow test, requires home error within one degree, and asks
  the user to judge smoothness and tracking speed directly.

