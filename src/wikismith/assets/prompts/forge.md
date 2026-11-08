Forge the vault's new material into the wiki, using this server's tools. Never edit vault files yourself.

1. Call `list_pending` and show what waits for the owner first:
   - **Open questions**: ask the owner each one. Save the answers with `answer_questions` (question id → the owner's answer, in their words); pass `approve_areas` only with the proposed areas the owner clearly agreed to. That starts a forge with the answers, so go on with step 3.
   - **Waiting branches**: show what couldn't be verified. Merge (`merge_forge`) or discard (`discard_forge`) one only when the owner says so. If the owner wants a branch changed, discard it and save the correction with `add_capture`; the next forge redoes it.
2. Call `run_forge`.
3. Call `job_status` with `wait_seconds: 120`; repeat it while the job is still running.
4. Show the job's summary to the owner as it is. If it lists questions for the owner, ask them now and save the answers with `answer_questions`, as in step 1.
