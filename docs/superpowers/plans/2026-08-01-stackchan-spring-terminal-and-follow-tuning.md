# StackChan Spring Terminal And Follow Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. Do not
> commit unless the user explicitly requests it.

**Goal:** Preserve the verified smooth spring motion, guarantee one
acknowledged exact-target terminal frame, and select one locally validated
faster face-follow candidate.

**Architecture:** The existing `head_spring_motion.h` helper remains the only
testable spring-transition boundary. `HostInterpolationMotionDriver::Tick()`
delegates completion state and exact terminal-frame selection to that helper;
the existing request-token freshness check and bus-lock ordering remain
unchanged. Follow tuning changes only the current YAML parameter after replay
evidence selects a candidate.

**Tech Stack:** C++17, smooth_ui_toolkit, GoogleTest/CTest, ESP-IDF 5.5.2,
TypeScript/Vitest, Python/pytest.

---

## Task 1: Reproduce The Lost Terminal Frame

**Files:**
- Modify: `firmware/host_test/test_head_spring_motion.cc`
- Modify: `firmware/main/boards/stackchan/head_spring_motion.h`

- [x] Add a GoogleTest case that advances a real `AnimateValue` spring to
      `done()` and expects the final-native-write flag to remain set.
- [x] Add a GoogleTest case that selects the integer target, rather than the
      fractional spring sample, for the pending terminal frame.
- [x] Run
      `cmake --build firmware/host_test/build --target head_spring_motion_test && firmware/host_test/build/head_spring_motion_test`
      and require the new test to fail because the completion helper or exact
      terminal-frame selector is absent.

## Task 2: Keep The Final Frame Until Acknowledgement

**Files:**
- Modify: `firmware/main/boards/stackchan/head_spring_motion.h`
- Modify: `firmware/main/boards/stackchan/stackchan.cc`
- Test: `firmware/host_test/test_head_spring_motion.cc`

- [x] Add the smallest helper that advances a moving spring, marks its integer
      target current at rest, and leaves the final-native-write flag set.
- [x] Add the smallest selector that returns the exact integer target only for
      a pending terminal frame.
- [x] Make `HostInterpolationMotionDriver::Tick()` use those helpers and keep
      `ShouldClearFinalNativeWrite()` as the sole successful-ACK clear point.
- [x] Run the focused host executable and require every spring test to pass.
- [x] Run `just firmware-host-test` and require all firmware host tests to pass.

## Task 3: Select One Faster Follow Candidate

**Files:**
- Inspect: Pico attention replay and capacity reports
- Modify only if evidence passes: `.pico-local/field-stackchan.yaml`

- [x] Record the current 125 ms observation interval baseline using the
      existing deterministic replay/capacity command and current four-degree
      step, critically damped spring, and safety counters.
- [x] Evaluate the 100 ms observation interval as the only changed variable.
- [x] Reject 100 ms after the freshness gate fails, then compare max-step 5,
      yaw-gain 50, and yaw-gain 47 one variable at a time against the retained
      125 ms baseline.
- [x] Retain yaw-gain 47 as the attended comparison candidate, but restore the
      active field configuration to 44 so terminal correctness is verified
      before the gain changes.
- [x] Run the focused Pico tests, typecheck, and lint.

## Task 4: Prepare Attended Physical Review

**Files:**
- Create or update: private report under `~/.pico/field-reports/`

- [x] Build and validate the StackChan artifact without deploying it.
- [x] Record artifact identity, local gate results, the one-variable tuning
      decision, and the exact next attended test commands without secrets or
      device identifiers.
- [x] Ask ChatGPT Pro to review the evidence and physical-test gates.
- [x] Restore the temporary Gateway and ports to their pre-run state.
- [x] Stop before a second OTA or any unattended hardware motion.
