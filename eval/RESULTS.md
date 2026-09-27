# Eval results

_Generated 2026-09-27 by `python eval/run_eval.py` over the synthetic statements in `samples/`. Deterministic pipeline, no API key required._

| Metric | Result |
|---|---|
| Direction correctness (all files) | PASS |
| Detection recall (real charges) | 100% (11/11) |
| Primary subscriptions list size | 7 |
| Noise kept out of the primary list | PASS |
| Investment safety (no SIP in leaks lens) | 100% |
| Price-creep detected (Netflix) | PASS |
| Cross-account duplicate detected (Spotify) | PASS |
| Aggregator separated (Razorpay to 2) | PASS |

Detected 15 recurring charges across the two-account case (recall-first). After distillation: **7 subscriptions**, 5 investments, 2 set aside with reasons, and 1 ignored as random or one-off spend (kept, not deleted). Injected noise (a self-transfer, a sub-minimum charge, an irregular booking) was correctly demoted.
