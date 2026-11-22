# Model performance testing

The model performance test compares models using the task-agent loop, its
configured model API, instructions, routing, tool catalog and limits. It uses
synthetic scenarios, an in-memory job and simulated tools: it never reads or
writes real integration data, sends messages, or creates real jobs. User-created
skills, memory and other live job context are not loaded. Compaction uses the
rule-based summarizer rather than making an unmeasured model call. Results are
therefore a simulation, not a guarantee of production performance.

Run inside the existing task-agent container after rebuilding it with this
version. For example, from the installation root:

```sh
docker compose --env-file data/.env exec -T task-agent \
  python -m assistant_agent.model_benchmark \
  --model example/model-a --model example/model-b \
  --judge-model example/judge-model --runs 2 \
  --output /data/share/model-performance-test.json
```

Replace the example IDs with models accessible via the configured API. The
judge uses that same API and its independently supplied model ID. Task and
judge model fallback is disabled; configured provider and ZDR restrictions
remain in force. Runs incur real model charges, including separate judge calls.
The test never changes the configured task model. To compare again, choose a
new output name; the command refuses to overwrite an existing file.

The output contains the complete test requests, responses, tool events and
judge rationale, plus per-model and per-run measurements. Prompt and completion
tokens and provider-reported cost are `null` if any contributing call omits
them; no prices are estimated. Judge usage and cost are separate. Error rate
is the fraction of runs that did not complete; a judge failure is reported as
unscored and makes the command exit nonzero. Tool calls include discovery and
terminal calls. Timing is wall-clock time for the simulated task, not the judge.
Scores are subjective and the judge may be biased; the included rubric checks
correctness, completeness, tool use and honesty.

**Privacy:** the full test thread, including your configured assistant
instructions and runtime context, is sent to the judge model. The JSON file is
created with mode `0600`, but `/data/share` is the assistant's shared workspace,
not an encrypted private store, and other processes with the same account may
read it. Choose a protected mounted directory instead if available and handle
the results as sensitive. The file is updated after each run; interrupted or
failed calls can have unknown charges. Apply provider spending limits before
testing.
