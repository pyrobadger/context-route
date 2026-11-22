# Does the Best Model Depend on the Context? A Preliminary Pilot on Joint Model and Context-Budget Selection for Agentic Issue Resolution

*Pilot data frozen 22 November 2026*

## Abstract

An agentic coding system can vary both the model that resolves an issue and the repository context it receives before its first tool call. We ask whether selecting these two settings jointly reduces total tokens per verified resolution. We evaluated ContextRoute, an OpenCode plugin that builds task-specific context packs and chooses a model and context tier using verified prior outcomes. The pilot covered eight issue-resolution tasks in one synthetic Python repository, two hosted models, and three context tiers. We audited 67 completed executions, including a balanced first round of 48 and 19 partial repeats. In leave-one-task-out replay of the first round, the fixed Big Pickle C2 configuration solved eight of eight tasks at 48,621 tokens per verified resolution. Joint routing solved seven of eight at 87,564 tokens per verified resolution, 80.1% above the fixed baseline. Every adaptive fold made an evidence-driven selection. The lower-token model changed across tiers on six of seven tasks for which both models passed at every tier. Four of the twelve pairwise reversals occurred with identical context text, and repeated runs at identical settings had a median absolute token change of 30.2%. The fixed configuration performed best among the evaluated policies. The pilot leaves open whether model–context interactions can be predicted reliably on new tasks.

**Keywords** Coding agents, model routing, repository context, token efficiency, issue resolution

## 1. Introduction

Coding agents typically take several actions to resolve a repository issue. They may read source files, follow dependencies, edit code, and inspect the results of tools. Supplying relevant source excerpts before the first model request can change that sequence. Useful excerpts can reduce subsequent exploration, while a larger pack increases the first request and can include information the agent never uses. The value of the same pack may also differ across models.

This study asks two questions. **RQ1.** Does the lower-token model change with the initial context tier among configurations that both resolve the task? **RQ2.** Can a policy that jointly selects a model and tier exploit such differences on held-out tasks, while maintaining the verified pass rate and reducing tokens per resolution? The engineering contribution is ContextRoute’s mechanism for constructing, applying, and verifying the initial context and the selected model. The empirical contribution is a balanced offline comparison of four policies, accompanied by checks against repeated runs and identical context packs.

## 2. Related work

SWE-bench evaluates models on issue resolution using real GitHub repositories and executable verification. We adopt the principle of judging repository edits with tests and use a small synthetic fixture so we can run every model–tier combination on the same tasks. LocAgent uses a graph representation to help agents localize relevant code. ContextRoute uses repository relationships to expand task-specific excerpts, and the pilot varies the amount and structure of the initial evidence supplied. [Jimenez et al., SWE-bench](https://arxiv.org/abs/2310.06770) · [Chen et al., LocAgent](https://arxiv.org/abs/2503.09089)

SWE-Router studies model selection for multistep software engineering tasks. Scrouting first explores the repository and uses a structured handoff to inform model selection. LLMRouter provides several routing methods and evaluates their quality and cost against common candidate outcomes. HM-Router jointly chooses a model and an agent harness, and Rivet studies context control for repository-level coding agents. Our pilot holds the harness fixed and uses initial context tier as the second selection variable. [Son et al., SWE-Router](https://arxiv.org/abs/2607.00053) · [Bhola et al., Scrouting](https://arxiv.org/abs/2608.04804) · [Feng et al., LLMRouter](https://arxiv.org/abs/2608.06867) · [Chen et al., HM-Router](https://arxiv.org/abs/2609.32213) · [Pan et al., Rivet](https://arxiv.org/abs/2610.34967)

## 3. System and evaluation method

### 3.1 Initial repository context

ContextRoute is a TypeScript plugin for OpenCode. It indexes eligible repository files locally, ranks excerpts with a BM25-style lexical score, and gives priority to paths mentioned in the task. It constructs three candidate packs. In the dependency-aware tiers, the pack can include excerpts from imports, callers, and nearby tests. A tier’s budget is a ceiling on the estimated size of the rendered pack. The delivered size varies with the available excerpts. [BM25](https://doi.org/10.1561/1500000019) · [Context implementation](src/context/context-builder.ts)

| Tier | Estimated token ceiling | Pack construction |
|---|---:|---|
| C1 | 1,200 | Lexically ranked excerpts |
| C2 | 3,200 | Lexical excerpts and first-hop repository relationships |
| C3 | 6,400 | C2-style retrieval extended through second-hop relationships |

For each experimental execution, the runner forces an exact model identifier and tier. The plugin attaches the pack to the first primary request. The audit reconciles the actual primary model, the hash of the saved pack, and the HTTP request attestation. Both models receive the same pack text within a given task and tier. OpenCode handles subsequent tool use and model calls. [Plugin measurement hooks](src/plugin/hooks.ts) · [Trace auditor](benchmark/evaluator/study.py)

### 3.2 Routing policy

The empirical router considers each compatible model–tier pair and looks up distinct tasks with completed, independently verified outcomes for the same model, tier, and retrieval version. It uses evidence for the inferred task category when that category meets the minimum sample count, and otherwise uses pooled evidence for the pair. A pair qualifies with at least four training tasks, a 95% Wilson lower bound on its success rate of at least 0.50, and a known mean list cost. It then ranks qualifying pairs by mean whole-task list cost, mean total tokens, and mean latency, in that order. With all list costs recorded as zero in this pilot, token count governs ranking after the eligibility checks. When no pair qualifies, the policy retains the starting model and prefers C2 if compatible. [Empirical optimizer](src/router/joint-optimizer.ts) · [Analysis rules](experiments/analysis_rules.md)

| Policy | Candidate models | Candidate tiers |
|---|---|---|
| Fixed | Big Pickle | C2 |
| Model-only | Both models | C2 |
| Context-only | Big Pickle | C1, C2, C3 |
| Joint | Both models | C1, C2, C3 |

### 3.3 Benchmark and outcomes

The fixture is `contextroute-shop`, a synthetic Python repository at commit `6a04966b04f2bee261f11fbb203cd2bef3d9769c`. The eight tasks cover inventory, pricing, refunds, reporting, shipping, SKU lookup, checkout, and quantity validation. The model IDs are `opencode/big-pickle` and `opencode/deepseek-v4.1-flash-free`. Each execution used a clean disposable checkout and an isolated OpenCode 2.2.7 session. Shell, external-directory, and subagent permissions were denied. Verification ran independently against the resulting checkout using the task’s executable acceptance tests. A run that completed and failed those tests counts as a task failure. Rate limits, timeouts, and invalid executions count toward attempt coverage. [Frozen tasks](benchmark/tasks/tasks.json) · [Runner](benchmark/runner/run.py)

The main analysis uses the single first-round outcome for each of the 48 task–model–tier cells. Each of the eight replay folds holds out a whole task, including all six of its outcomes. The production TypeScript optimizer receives outcomes from the other seven tasks, subject to its category and eligibility rules. We join its recommendation to the recorded outcome for the selected held-out configuration. The fixed policy uses the Big Pickle C2 record directly. Each policy is scored on the same eight tasks. This is an offline policy evaluation of actual agent executions collected under forced settings. The 19 second-round observations are used for the repeatability check. [Replay implementation](benchmark/evaluator/evaluate.py) · [Frozen execution export](experiments/results/results.jsonl)

The primary metric is **tokens per verified resolution**. We sum the recorded input and output tokens for every selected attempt, including completed task failures, and divide by the number of passed tasks. We report the pass count alongside that ratio. The source is OpenCode’s cumulative session usage, so the metric covers the recorded session rather than just the initial context. Auxiliary request identities are logged separately. The zero list-cost fields make token use the efficiency measure in this study. [Usage telemetry](src/plugin/hooks.ts) · [Exported observations](experiments/results/results.jsonl)

## 4. Results

### 4.1 Execution coverage

The saved grid contains **178 execution attempts**. Of these, **67** passed the execution and evidence audit, with **64 task passes** and **three test failures**. The remaining 111 attempts comprise **88 HTTP 429 failures, 22 timeouts, and one invalid execution**. The first round covers all 48 cells, with 45 passes. The partial second round covers 19 of those cells, all of which passed. [Frozen execution export](experiments/results/results.jsonl) · [Attempt records](benchmark/results/grid/runs)

### 4.2 Fixed configurations

All rows in Table 1 use eight first-round executions. The two models have identical mean initial pack size at each tier because they received identical packs task by task. Big Pickle passed at every tier. DeepSeek failed Daily Report at each of the three tiers and passed the other seven tasks.

**Table 1.** Recorded performance of the six configurations in the balanced first round.

| Model | Tier | Verified passes | Mean initial pack, estimated tokens | Total session tokens | Tokens per verified resolution | Mean latency |
|---|---|---:|---:|---:|---:|---:|
| Big Pickle | C1 | 8/8 | 739.4 | 432,849 | 54,106 | 33.6 s |
| Big Pickle | C2 | 8/8 | 852.9 | 388,968 | 48,621 | 26.9 s |
| Big Pickle | C3 | 8/8 | 909.9 | 407,994 | 50,999 | 29.9 s |
| DeepSeek V4.1 Flash Free | C1 | 7/8 | 739.4 | 297,964 | 42,566 | 23.3 s |
| DeepSeek V4.1 Flash Free | C2 | 7/8 | 852.9 | 340,074 | 48,582 | 24.0 s |
| DeepSeek V4.1 Flash Free | C3 | 7/8 | 909.9 | 379,248 | 54,178 | 25.4 s |

DeepSeek used 21.4% fewer tokens at C1 than at C3 and resolved the same seven tasks. Big Pickle used 6.1% more tokens at C1 than at C3, with C2 lowest across its three tiers. A smaller initial pack therefore coincided with lower whole-task token use for DeepSeek in this round, while Big Pickle showed a different pattern. These are exploratory contrasts. Tier changes affect retrieval structure and delivered text as well as the nominal budget. [First-round and repeated observations](experiments/results/results.jsonl)

### 4.3 Held-out policy performance

**Table 2.** Leave-one-task-out replay using the shipped optimizer. The percentage change is relative to the fixed policy’s tokens per verified resolution.

| Policy | Verified passes | Total session tokens | Tokens per verified resolution | Change vs fixed | Empirical choices / fallbacks |
|---|---:|---:|---:|---:|---:|
| Fixed Big Pickle C2 | 8/8 | 388,968 | 48,621 | Reference | Fixed by design |
| Model-only | 7/8 | 412,012 | 58,859 | +21.1% | 8 / 0 |
| Context-only | 8/8 | 560,908 | 70,114 | +44.2% | 8 / 0 |
| Joint | 7/8 | 612,948 | 87,564 | +80.1% | 8 / 0 |

The fixed policy matched the best pass count and had the lowest token cost per verified resolution among the evaluated policies. Model-only and joint routing each missed Daily Report. On this fold, all available DeepSeek training outcomes were successful. Model-only selected DeepSeek C2, which failed after 72,511 tokens. Joint routing selected DeepSeek C3, which failed after 129,708 tokens. The fixed Big Pickle C2 run passed in 49,467 tokens. The joint policy and the model-only policy each resolved seven tasks, and joint used 48.8% more tokens per resolution. The remaining fold selections appear in Appendix A.

Context-only routing passed all eight tasks but selected expensive configurations on several held-out folds. For example, its Big Pickle C1 choice for Refund Balance used 115,107 tokens, while Big Pickle C2 used 23,590. The selected total can exceed the totals of every constant tier because different folds contribute different high-token tasks. Each individual selection was checked against its held-out record.

### 4.4 Ranking reversals and repeatability

Seven tasks passed under both models at all three tiers. Six of those seven showed a reversal in which model used fewer tokens across tiers. Across the 21 task-by-tier-pair comparisons with successful runs from both models at both tiers, 12 showed a reversal. Four of the 12 occurred where the two tiers had identical context hashes for the task. For example, on Refund Balance, DeepSeek used 26,114 tokens at C1 versus Big Pickle’s 115,107. At C2, Big Pickle used 23,590 tokens versus DeepSeek’s 47,461, even though the C1 and C2 packs were identical.

Across the 19 repeated cells, the exact model, tier, task, and context hash matched the first-round execution. The median absolute change in total tokens, expressed as a percentage of the first-round count, was 30.2%. Variation at identical settings makes the observed model rankings an uncertain basis for choosing a tier on a new task. [First-round and repeated observations](experiments/results/results.jsonl)

### 4.5 Hindsight headroom

For each task, a hindsight selector chooses the passing configuration with the fewest recorded tokens within the given candidate set. This calculation uses held-out outcomes and estimates an empirical upper bound on improvements available from these particular runs. It also benefits from selecting unusually low token counts among several executions.

**Table 3.** Best passing configurations selected with hindsight on the balanced first round.

| Choices available | Verified passes | Total session tokens | Tokens per verified resolution | Reduction vs fixed |
|---|---:|---:|---:|---:|
| Fixed Big Pickle C2 | 8/8 | 388,968 | 48,621 | Reference |
| Model-only at C2 | 8/8 | 282,133 | 35,267 | 27.5% |
| Context-only with Big Pickle | 8/8 | 259,453 | 32,432 | 33.3% |
| Joint model and tier | 8/8 | 230,455 | 28,807 | 40.8% |

The joint hindsight selector estimates the headroom in the recorded outcomes. The held-out replay shows that the implemented policy was unable to realize that headroom. The earlier exporter includes a random-arm calculation labeled as a permutation null. That procedure holds the observed oracle total fixed and randomizes the comparison baseline. Its tail probability describes where the fixed baseline falls among random assignments. A test of model–context interaction would require an appropriate null for the interaction statistic and a design that accounts for repeated executions. [Earlier analysis implementation](experiments/export_results.py#L321)

## 5. Discussion and limitations

**Model choice and context choice can look coupled in one run.** The observed ranking reversals expose sensitivity to the recorded model and tier combination, while reversals with identical packs expose the role of execution variability. A model-by-context interaction that a router can predict requires consistency across repeated executions. The incomplete repeats leave that question unresolved.

**The existing policy underperformed in this pilot.** The eligibility guard operated as designed, and all adaptive folds made empirical selections. With a small and mostly successful training set, pooling outcomes gives a coarse estimate of which arms will work or use fewer tokens on the next task. A useful comparison for a future study is the best fixed arm selected within the training split, in addition to the fixed deployment default. Both comparisons should be evaluated on independent repositories, with repeated runs and the same correctness criteria.

**The experiment varies the initial pack as a package.** Moving from C1 to C2 or C3 changes both the nominal budget and the retrieval expansion. Actual packs averaged well below their ceilings. A follow-up could separately vary budget while holding retrieval strategy fixed, and compare targeted injection with plain OpenCode using the same model, tasks, and acceptance tests. Measured whole-task usage and verified correctness would establish whether a smaller first request reduces overall usage.

**Scope and measurement.** The tasks are hand-built and share one repository, so the folds share project structure and conventions. The hosted model aliases, provider availability, and token accounting limit external comparability. Most cells have one execution, and the partial repeat round is exposed to model availability and acquisition order. The results are descriptive at the scale of eight tasks. For a confirmatory study, use multiple repositories, harder issues with a wider spread of verified outcomes, balanced repetitions and randomized run order, and a held-out repository split. Lock the policy and token accounting before collecting the confirmatory results.

## 6. Conclusion

The lower-token model varied across context tiers in the recorded pilot, but changes also occurred with identical context text. On the balanced eight-task replay, fixed Big Pickle C2 delivered more verified resolutions at lower token use than the joint router. ContextRoute supplies a working mechanism for controlling and verifying the initial model and context. The empirical results support keeping the fixed baseline for this setting and using future matched, repeated experiments to determine when targeted context and model selection improve end-to-end efficiency.

## References

- Bhola, I., Krishnan, A., Mukunda, N. S., and Jaglan, D. (2026). *Scrouting: Cost-Aware Routing of Coding Agents by Scouting the Repository First*. [arXiv:2608.04804](https://arxiv.org/abs/2608.04804)
- Chen, H. M., Lee, R., Okoshi, Y., Anastasiou, D., Luk, W., and Fan, H. (2026). *HM-ROUTER: Joint Model and Harness Routing for Agentic Systems*. [arXiv:2609.32213](https://arxiv.org/abs/2609.32213)
- Chen, Z., et al. (2025). *LocAgent: Graph-Guided LLM Agents for Code Localization*. [arXiv:2503.09089](https://arxiv.org/abs/2503.09089)
- Feng, T., et al. (2026). *LLMRouter: Unified Infrastructure for Developing, Evaluating, and Deploying LLM Routers*. [arXiv:2608.06867](https://arxiv.org/abs/2608.06867)
- Jimenez, C. E., Yang, J., Wettig, A., Yao, S., Pei, K., Press, O., and Narasimhan, K. (2024). *SWE-bench: Can Language Models Resolve Real-World GitHub Issues?* [ICLR 2024 paper](https://arxiv.org/abs/2310.06770)
- Pan, R., Wu, L., Zhang, Y., Yang, Z., Zhang, L., and Zhang, H. (2026). *Rivet: Utility-Preserving Context Control for Repository-Level Coding Agents*. [arXiv:2610.34967](https://arxiv.org/abs/2610.34967)
- Robertson, S., and Zaragoza, H. (2009). *The Probabilistic Relevance Framework: BM25 and Beyond*. Foundations and Trends in Information Retrieval, 3(4), 333–389. [doi:10.1561/1500000019](https://doi.org/10.1561/1500000019)
- Son, S., Yoon, S., Tang, J., Wang, S., Wolf, L., and Bogunovic, I. (2026). *SWE-Router: Routing in Multi-turn Agentic Software Engineering Tasks*. [arXiv:2607.00053](https://arxiv.org/abs/2607.00053)

## Appendix A. Held-out selections

Each cell gives the selected model, tier, and total session tokens. **BP** is Big Pickle. **DS** is DeepSeek V4.1 Flash Free. An asterisk marks a completed execution that failed the task verifier.

| Held-out task | Fixed | Model-only | Context-only | Joint |
|---|---|---|---|---|
| `inventory-adjustment` | BP C2 · 51,290 | BP C2 · 51,290 | BP C3 · 66,323 | BP C3 · 66,323 |
| `pricing-discount` | BP C2 · 25,788 | BP C2 · 25,788 | BP C1 · 62,977 | BP C1 · 62,977 |
| `refund-balance` | BP C2 · 23,590 | BP C2 · 23,590 | BP C1 · 115,107 | BP C1 · 115,107 |
| `daily-report` | BP C2 · 49,467 | DS C2 · 72,511\* | BP C3 · 77,668 | DS C3 · 129,708\* |
| `shipping-threshold` | BP C2 · 50,500 | BP C2 · 50,500 | BP C2 · 50,500 | BP C2 · 50,500 |
| `sku-lookup` | BP C2 · 43,990 | BP C2 · 43,990 | BP C2 · 43,990 | BP C2 · 43,990 |
| `checkout-shipping` | BP C2 · 39,929 | BP C2 · 39,929 | BP C2 · 39,929 | BP C2 · 39,929 |
| `quantity-validation` | BP C2 · 104,414 | BP C2 · 104,414 | BP C2 · 104,414 | BP C2 · 104,414 |

## Appendix B. Reproducibility and analysis provenance

The normalized [execution export](experiments/results/results.jsonl) includes the repeat index, observed model, tier, context hash, verified outcome, token counts, and latency. Its SHA-256 is `206fc481cfde41629c47ce42730361ab797ee1e3296f60a3761387e84bb17f31`. The per-run [raw records](benchmark/results/grid/runs) and [telemetry sidecars](benchmark/results/grid/telemetry) contain the model catalog data, candidate pack metadata, and request attestations required to audit and replay the exact policy. The [analysis rules](experiments/analysis_rules.md) specify the frozen fixture, arms, and thresholds. The older [exporter analysis](experiments/results/analysis.json) averages whatever repeats are available and implements a separate Python ranking rule. The main results here instead use the balanced first round and call the TypeScript optimizer with its existing thresholds.

Build the TypeScript optimizer with `npm run build`. From the repository root, run the following Python snippet against the saved records to reproduce the policy totals. This step runs offline.

```python
import json
from pathlib import Path

from benchmark.evaluator.evaluate import read_runs, replay
from benchmark.evaluator.study import audit_finished

source = Path("benchmark/results/grid")
frozen = Path("experiments/results/results.jsonl").read_text(encoding="utf-8-sig")
exported = [json.loads(line) for line in frozen.splitlines() if line.strip()]
first_round_ids = {row["execution_id"] for row in exported if row["repeat"] == 0}
rows = [row for row in read_runs(source / "runs") if row["execution_id"] in first_round_ids]
commit = "6a04966b04f2bee261f11fbb203cd2bef3d9769c"
assert len(rows) == len(first_round_ids) == 48
assert all(audit_finished(row, source, commit) is None for row in rows)

models = ["opencode/big-pickle", "opencode/deepseek-v4.1-flash-free"]
evaluation = replay(rows, models[0], models=models, contexts=["C1", "C2", "C3"])
assert evaluation["complete_tasks"] == 8 and evaluation["replay_status"] == "ok"
for name, policy in evaluation["policies"].items():
    passed = policy["successes"]
    total = policy["total_tokens"]
    print(name, passed, total, round(total / passed, 1))
```
