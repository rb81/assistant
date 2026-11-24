# Model performance testing

Compare candidate models and reasoning policies using the real task-agent loop,
skills, tool schemas, cache reduction and execution controls. Jobs, memory and
integrations are simulated; no live task/reminder is read, executed or modified.
The task/reminder review scenario checks complete listing of 23 tasks and
24 reminders, distinguishing pending, waiting/review and failed work. Larger
requested limits can retrieve this fixture in one page; unit tests separately
exercise pagination. Detail reads and bounded retrieval of large cached reminder
instructions support both advertised absolute and shared-root-relative cache
paths, with traversal outside the owned cache rejected. Smaller
arithmetic, file-summary and missing-data scenarios remain available.

## Upload one candidate script without deploying

From a checkout containing these changes, package public source and defaults
outside the repository:

```sh
python3 src/scripts/package_model_benchmark.py --output ../candidate-model-benchmark.sh
scp ../candidate-model-benchmark.sh user@dev-server:~/candidate-model-benchmark.sh
```

On the development server, change to the existing Assistant installation root:

```sh
ASSISTANT_BENCHMARK_OUTPUT="$HOME/benchmark-review.json" \
  sh "$HOME/candidate-model-benchmark.sh" \
  "$(docker compose --env-file data/.env ps -q task-agent)" \
  --model example/baseline --model example/comparison \
  --judge-model example/judge \
  --scenario task-reminder-review \
  --reasoning auto --reasoning low --reasoning provider_default \
  --runs 1 --iterations 16 --task-token-limit 200000 --output-tokens 8192
```

Replace IDs with accessible **OpenRouter** models. The script uses the running
container's saved credentials, provider restrictions and ZDR settings. It refuses
disabled OpenRouter, disables task/judge fallback and imports the packaged
candidate code/defaults in an owned temporary directory. No rebuild, worker
restart, secret export or database connection is needed. Existing environment
overrides are retained; benchmark limits/policies affect only the simulation.
The script never overwrites its protected host results file.

**These are paid requests.** Start with one model/policy, then broaden. A run has
16 iterations and a 200,000 reported-token ceiling by default, a 180-second
between-call wall-time allowance and at most 45 seconds per inference request.
Provider capability discovery may add catalog latency. A call already sent can
consume the remaining allowance; uncertain/failed calls can have unknown charges.
Use provider account spending limits too. Unsupported explicit reasoning is
reported as failure, not silently substituted. `--reasoning budget` additionally
uses `--thinking-tokens 2048`, which must fit inside total output tokens and be
supported by the selected model.

## Run an already deployed version

```sh
docker compose --env-file data/.env exec -T task-agent \
  python -m assistant_agent.model_benchmark \
  --model example/model-a --model example/model-b \
  --judge-model example/judge --scenario task-reminder-review \
  --reasoning auto --reasoning provider_default \
  --output /data/share/model-performance-test.json
```

This version uses the configured task provider. Choose a new output name each
time. Add other scenarios with repeated `--scenario`, or omit it for all.

## Results and privacy

JSON preserves per-call requests/responses, finish reasons, actual model/provider,
reasoning decisions, elapsed time, token usage, tool errors and guidance
deferrals. Summaries separate each model/policy pair. `fixture_errors` is the
benchmark-only subset of `tool_errors`, not evidence of production tool failure. Plain or
single-fenced JSON judge responses are validated identically. Internal assistant
drafts are not treated as delivered answers. Judge tokens/cost are separate;
its reasoning uses provider defaults regardless of the tested policy.
Failed/incomplete runs skip the judge rather than paying for a redundant call.
Missing token/cost/reasoning usage is `null`, not an estimate. Non-completion or
an unscored judge makes the command exit nonzero; inspect the saved result.

By default the benchmark uses synthetic standing instructions/identity. Only
`--use-saved-instructions` opts into your operator instructions and identity.
The judge receives synthetic visible model turns and tool events, not opaque
native reasoning/signature blocks. The local results contain full provider
responses and should be treated as sensitive even with mode `0600`; shared
storage is not encrypted. Scores are subjective, and simulated completion does
not prove real delivery, approvals, native-provider compatibility or production
reliability. Repeat successful comparisons and examine transcripts before
changing production defaults.
