# Benchmark results (2026-10-21 15:13)

LLM: `us.amazon.nova-pro-v1:0` on Amazon Bedrock. Decider: Strands Decider 2B on laptop RTX 4050 (WSL2). Cost uses an assumed $0.8/$3.2 per 1M input/output tokens; token counts are measured. Decider cost is $0 per call on owned hardware (electricity not counted).

| metric | Decider-gated agent | LLM-only agent |
|---|---|---|
| alarms | 49 | 49 |
| category_accuracy | 0.816 | 0.816 |
| accuracy_by_profile | {'bottling_line': 0.8, 'discrete_assembly': 0.786, 'process_plant': 0.85} | {'bottling_line': 0.8, 'discrete_assembly': 0.786, 'process_plant': 0.85} |
| safety_recall_category | 4/5 | 5/5 |
| safety_surfaced | 5/5 | 5/5 |
| real_alarms_hidden | 0 | 0 |
| operator_load_cut | 0.49 | 0.653 |
| decision_ms_p50 | 955 | 1695 |
| decision_ms_p95 | 1146 | 2638 |
| llm_calls | 11 | 49 |
| tokens_in | 3326 | 42828 |
| tokens_out | 783 | 7255 |
| llm_cost_usd | 0.0052 | 0.0575 |
| llm_cost_per_1k_alarms_usd | 0.11 | 1.17 |
