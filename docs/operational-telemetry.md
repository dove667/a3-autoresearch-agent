# Operational telemetry

The Host exposes bounded counters through `GET /telemetry`. An authenticated Agent receives its own counters by default. `?scope=descendants` includes authorized descendants, and `?scope=run` includes the registered research identities visible in the current run. Other runs, external users and Host credentials remain outside that scope. The response contains no owner IDs, paths, route strings, request IDs, query strings, request/response bodies, messages, error text/types, credentials, model names, or input source filenames. It is constructed from an explicit whitelist, never by returning or parsing raw diagnostic or usage logs.

The rolling window holds at most 256 authenticated completed requests across the Host, grouped into fixed request categories. `recent_events` contains the latest 32 authorized events in chronological order, each limited to kind, failure flag, actual observed duration and optional model role/numeric usage. Events contain no identifiers or timestamps. Snapshots deep-copy retained event data so callers cannot alter future observations. Unknown and control routes (`/telemetry`, operator feedback/input controls, scheduler identity polls to `/sessions/self` and `/evolution`, health) do not contribute. Observed model durations use a monotonic clock around the provider call, including failures; request durations are measured separately. Only numeric bounded token usage fields are accepted. `usage_observed_calls` distinguishes missing usage from observed usage. Cost remains `null`: this interface does not infer prices or charges.

`POST /telemetry/observations` optionally replaces the caller's latest local counters. Supply exactly these seven integer fields, each between 0 and 1,000,000,000:

```json
{"read_calls":10,"repeated_reads":7,"retrieved_characters":1000,"delivered_reads":3,"open_items":2,"waiting_items":1,"closed_items":0}
```

There are no identity, path, free text or custom fields. Latest counters for at most 256 owners are retained and summed within the authorized scope. Host checkpoints persist them under `gateway.telemetry_observations`. Local observations are Agent-reported operational evidence, not independently verified task success. Gateway rolling request counters restart empty; the window is not a lifetime total. Consumers can post counters best effort once per decision; posting does not call a model.
