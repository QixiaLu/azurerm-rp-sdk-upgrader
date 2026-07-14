

# RP Upgrade Orchestration — Design

End-to-end flow: one dispatch `(rp_name, target_api_version)` chains the workers,

## High-level flow

```text
Manual trigger (custom prompt) -> api upgrade agent -> acceptance tester -> breaking change detecter -> report
```

## Notes
- Once api upgrade agent finishes, if everything is okay, command invoke acceptance tester
- acceptance tester decides what to test, trigger running command, finish
- a watch script run in the background, polling the status, once finished, invoke tester to look into results
