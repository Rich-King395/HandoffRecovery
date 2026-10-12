# GPT Prior-Progress Smoke Task Set

This task set is selected from the formal GPT recovery run:

`results/formal-gpt-qwenmax/qwen3.8-max_gpt-5.6-luna_177_30_20261008T124934Z_d097eb34`

Selection criteria:

- `task_success=false`;
- `takeover_status=VALID`;
- `flow_passed=true`;
- no recorded `error_class` or cloud-agent error;
- exclude failures whose primary termination signal was `budget_exhausted`;
- cover different apps and takeover types.

The public `instance_id` values are the values to pass to the batch runner. The
`task_id` values identify the frozen HandoffBench task behind each instance.

| Instance | Task | App | Takeover type(s) | Planner branch |
|---:|---:|---|---|---|
| 15 | 2014 | `org.fossify.filemanager` | `3_partial_completion`, `6_progress_rollback` | EXECUTE |
| 28 | 2027 | `org.wikipedia` | `3_partial_completion` | EXECUTE |
| 179 | 2178 | `com.android.contacts` | `3_partial_completion` | EXECUTE |
| 23 | 2022 | `org.fossify.gallery` | `2_context_switch`, `6_progress_rollback` | EXECUTE |
| 34 | 2033 | `org.wikipedia` | `6_progress_rollback` | EXECUTE |
| 38 | 2057 | `com.google.android.youtube` | `6_progress_rollback` | EXECUTE |
| 144 | 2089 | `de.danoeh.antennapod` | `6_progress_rollback` | EXECUTE |
| 20 | 2019 | `org.fossify.gallery` | `2_context_switch`, `4_goal_completed` | EXECUTE |
| 22 | 2021 | `org.tasks` | `2_context_switch`, `4_goal_completed` | EXECUTE |
| 29 | 2028 | `org.videolan.vlc` | `2_context_switch`, `4_goal_completed` | VERIFY |
| 39 | 2060 | `com.google.android.youtube` | `2_context_switch` | EXECUTE |
| 43 | 2082 | `com.android.chrome` | `2_context_switch` | EXECUTE |
| 60 | 2107 | `org.readera` | `2_context_switch` | EXECUTE |
| 61 | 2108 | `com.google.android.apps.translate` | `2_context_switch` | EXECUTE |
| 62 | 2109 | `com.google.android.apps.translate` | `4_goal_completed` | VERIFY |
| 69 | 2144 | `org.fossify.filemanager` | `4_goal_completed` | VERIFY |
| 71 | 2134 | `org.isoron.uhabits` | `4_goal_completed` | EXECUTE |
| 87 | 2155 | `com.google.android.calendar` | `5_additional_user_edit` | EXECUTE |
| 172 | 2171 | `org.isoron.uhabits` | `5_additional_user_edit` | EXECUTE |
| 186 | 2185 | `org.wikipedia` | `5_additional_user_edit` | EXECUTE |

The set contains 20 unique instances. By declared labels it includes 3
partial-completion cases, 5 rollback cases, 8 context-switch cases, 6
goal-completed cases, and 3 additional-user-edit cases. Some instances carry
more than one label. Three prior `VERIFY` branches are retained for branch
coverage.
The experiment should replay this exact instance list for both the optimized
configuration and its control configuration.
