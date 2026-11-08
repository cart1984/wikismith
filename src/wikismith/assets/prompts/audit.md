Audit the vault, using this server's tools. Never edit vault files yourself.

1. Call `run_audit`, then `job_status` with `wait_seconds: 120`; repeat `job_status` while the job is still running.
2. Show the report's summary to the owner, and the report's path.
3. Fixes go through a forge: if the owner wants some of the suggested fixes, save what they chose with `add_capture` (one capture, naming the pages and fixes), with `forge_now: true`.
