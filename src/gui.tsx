import { Command, Panel, Plugin, Style, type PanelProps } from "@opencode/plugin/gui";
import { createEffect, createSignal, For, onCleanup, Show } from "solid-js";
import { routingRpc } from "./plugin/desktop-rpc.js";
import { formatContextReduction, formatContextTokens, modelDisposition, routingSourceLabels } from "./plugin/desktop-format.js";
import type { RoutingState } from "./plugin/desktop-state.js";
import css from "./gui.css?inline";

const labels: Record<RoutingState["stage"], string> = {
  waiting: "Waiting for task", analyzing: "Analyzing repository", routing: "Routing",
  executing: "Executing", completed: "Completed", interrupted: "Interrupted", error: "Error",
};
const strategies: Record<string, string> = { lexical: "Lexical", dependencies: "Dependency-aware" };
const count = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const cost = (value: number) => new Intl.NumberFormat(undefined, {
  style: "currency", currency: "USD", maximumFractionDigits: 6,
}).format(value);
const percentage = (value: number) => new Intl.NumberFormat(undefined, {
  style: "percent", maximumFractionDigits: 1,
}).format(value);

function ModelValue(props: { value: { name: string | null; id: string } | null }) {
  return <Show when={props.value} fallback="Unavailable">{model =>
    <div class="cr-model">
      <Show when={model().name && model().name !== model().id}><strong>{model().name}</strong></Show>
      <code>{model().id}</code>
    </div>
  }</Show>;
}

function RouteSummary(props: { state: RoutingState }) {
  const route = () => props.state.route!;
  const outcome = () => modelDisposition(props.state);
  const headline = {
    switched: "Model switch verified", retained: "Model retained", confirmation: "Switch needs confirmation",
    unverified: "Model switch awaiting verification", mismatch: "Model mismatch", unknown: "Model decision",
  };
  const comparison = () => route().confirmation ? route().recommended : route().applied;
  const showOriginal = () => Boolean(route().original && comparison() && route().original!.id !== comparison()!.id);
  return <section class="cr-summary" data-outcome={outcome()} data-fallback={route().source === "cold_start_fallback"}>
    <div class="cr-summary-head">
      <span class="cr-source">{routingSourceLabels[route().source] ?? routingSourceLabels.unknown}</span>
      <Show when={route().demo}><span class="cr-demo">Local demo</span></Show>
    </div>
    <h3>{outcome() === "unverified" && props.state.modelCheck === "unavailable" ? "Model switch unverified" : headline[outcome()]}</h3>
    <div class="cr-models" data-comparison={showOriginal()}>
      <Show when={showOriginal()}>
        <div><span class="cr-model-label">Original</span><ModelValue value={route().original} /></div>
        <span class="cr-arrow" aria-hidden="true">→</span>
      </Show>
      <div>
        <Show when={showOriginal()}><span class="cr-model-label">{route().confirmation ? "Recommended" : "Applied"}</span></Show>
        <ModelValue value={comparison()} />
      </div>
    </div>
    <Show when={route().confirmation && route().applied}>
      <p>OpenCode kept <strong>{route().applied!.name ?? route().applied!.id}</strong> for this request.</p>
    </Show>
    <Show when={route().source === "cold_start_fallback"}>
      <p>{route().reasons.find(reason => reason.startsWith("Public benchmark check"))
        ?? "The evidence threshold for switching was unmet."}</p>
    </Show>
    <Show when={route().demo}><p class="cr-demo-note">Simulated responses, token counts, and routing evidence.</p></Show>
  </section>;
}

function Verification(props: { state: RoutingState }) {
  const context = () => props.state.contextCheck;
  const model = () => props.state.modelCheck;
  const contextLabels = {
    pending: "Awaiting verification", verified: "Context delivered — verified",
    mismatch: "Context mismatch", unavailable: "Context verification unavailable", not_applied: "No context pack applied",
  };
  const modelLabels = {
    pending: "Model verification pending", verified: "Model verified", mismatch: "Model mismatch",
    unavailable: "Model verification unavailable", not_applied: "No model verification required",
  };
  return <section class="cr-card">
    <h3>Execution verification</h3>
    <div class="cr-verification">
      <span class="cr-mark" data-status={context()} aria-hidden="true" />
      <div><strong>{contextLabels[context()]}</strong>
        <Show when={context() === "verified"}>
          <small>{props.state.contextScope === "http" ? "Verified in HTTP request" : "Matched at the first primary request hook"}</small>
        </Show>
      </div>
    </div>
    <div class="cr-verification">
      <span class="cr-mark" data-status={model()} aria-hidden="true" />
      <div><strong>{modelLabels[model()]}</strong>
        <Show when={model() === "mismatch" && props.state.actual}><code>{props.state.actual}</code></Show>
      </div>
    </div>
  </section>;
}

function Details(props: { state: RoutingState; openSource: (url: string) => void }) {
  const route = () => props.state.route!;
  const evidence = () => route().evidence;
  return <div class="cr-grid">
    <RouteSummary state={props.state} />
    <section class="cr-card">
      <h3>Initial repository context</h3>
      <Show when={route().tier && route().tier !== "default"} fallback={<p>No context pack applied.</p>}>
        <div class="cr-context"><strong>{route().tier}</strong>
          <Show when={route().strategy}>{strategy => <span>{strategies[strategy()]}</span>}</Show>
        </div>
        <Show when={route().initialTokens != null && (route().budget ?? 0) > 0}>
          <meter class="cr-budget" aria-label="Estimated repository context tokens used" min={0}
            max={route().budget!} value={route().initialTokens!} />
        </Show>
        <dl class="cr-values">
          <Show when={route().initialTokens !== null || route().budget !== null}>
            <dt>Estimated tokens</dt><dd>{formatContextTokens(route().initialTokens, route().budget)}</dd>
          </Show>
          <Show when={route().files !== null}>
            <dt>Selected files</dt><dd>{count(route().files)}</dd>
          </Show>
        </dl>
        <Show when={route().contextEstimate}>{estimate =>
          <div class="cr-estimates">
            <h4>Context estimates</h4>
            <dl class="cr-values">
              <Show when={estimate().c2Tokens !== null}>
                <dt>Context tokens vs C2</dt>
                <dd classList={{ "cr-token-reduction": estimate().compactTokens < estimate().c2Tokens! }}>
                  {formatContextReduction(estimate().compactTokens, estimate().c2Tokens!)}
                </dd>
              </Show>
              <dt>Break-even vs plain OpenCode</dt>
              <dd>{count(estimate().compactTokens)} tokens</dd>
            </dl>
            <p class="cr-policy">Later calls need to save more than the break-even amount for total usage to fall.</p>
          </div>
        }</Show>
        <Show when={route().excerpts.length > 0}>
          <details>
            <summary>{props.state.contextCheck === "verified" ? "Files and lines sent" : "Files and lines selected"}</summary>
            <ul class="cr-excerpts"><For each={route().excerpts}>{excerpt =>
              <li><code>{excerpt.path}</code><span>{excerpt.start === excerpt.end
                ? `Line ${excerpt.start}` : `Lines ${excerpt.start}–${excerpt.end}`}</span></li>
            }</For></ul>
          </details>
        </Show>
      </Show>
    </section>
    <Verification state={props.state} />
    <section class="cr-card cr-evidence">
      <h3>Routing evidence</h3>
      <Show when={route().source === "empirical" && evidence()?.evidence != null}>
        <p>{count(evidence()?.evidence)} {route().demo ? "sample observations" : "supporting observations"}</p>
      </Show>
      <Show when={route().publicBenchmark}>{item =>
        <dl class="cr-values">
          <dt>Benchmark</dt><dd>{item().benchmark}</dd>
          <dt>Selected score</dt><dd>{percentage(item().selected.resolved / item().selected.tasks)}
            {" (" + count(item().selected.resolved) + "/" + count(item().selected.tasks) + ")"}</dd>
          <dt>Evaluation</dt><dd>{item().evaluationDate}</dd>
          <dt>Source</dt><dd><a href={item().selected.sourceUrl} onClick={event => {
            event.preventDefault();
            props.openSource(item().selected.sourceUrl);
          }}>SWE-bench Experiments</a></dd>
        </dl>
      }</Show>
      <Show when={!route().demo && route().publicCoverage}>{coverage =>
        <p class="cr-policy">Public cache · {count(coverage().matched)} of {count(coverage().available)} model IDs matched
          <Show when={coverage().lastUpdate}> · {new Date(coverage().lastUpdate!).toLocaleDateString()}</Show>
        </p>
      }</Show>
      <details>
        <summary>Why this configuration?</summary>
        <Show when={route().reasons.length > 0}><ul><For each={route().reasons}>{reason => <li>{reason}</li>}</For></ul></Show>
        <Show when={route().policy}><p class="cr-policy">Policy <code>{route().policy}</code></p></Show>
        <Show when={route().publicBenchmark}>{item =>
          <>
            <dl class="cr-values">
              <Show when={item().original} fallback={<><dt>Current model</dt><dd>No matching score</dd></>}>
                {original => <><dt>Original score</dt><dd>{percentage(original().resolved / original().tasks)}</dd></>}
              </Show>
              <Show when={item().reference}>{reference =>
                <><dt>Compared with</dt><dd><code>{reference().key}</code> · {percentage(reference().resolved / reference().tasks)}</dd></>
              }</Show>
              <dt>Selected version</dt><dd><code>{item().selected.modelVersion}</code></dd>
              <dt>Harness</dt><dd>{item().harness}</dd>
              <dt>New passes / new failures</dt><dd>{count(item().improvements)} / {count(item().regressions)}</dd>
            </dl>
            <p class="cr-policy">{item().limitations}</p>
          </>
        }</Show>
        <Show when={route().source === "empirical" ? evidence() : undefined}>{item =>
          <dl class="cr-values">
            <Show when={item().success != null}><dt>Historical success</dt><dd>{percentage(item().success!)}</dd></Show>
            <Show when={item().wilson != null}><dt>Wilson lower bound</dt><dd>{percentage(item().wilson!)}</dd></Show>
            <Show when={item().tokens != null}><dt>Mean historical tokens</dt><dd>{count(item().tokens)}</dd></Show>
            <Show when={item().seconds != null}><dt>Mean historical latency</dt><dd>{item().seconds!.toFixed(2)} s</dd></Show>
            <Show when={item().cost != null}><dt>Mean estimated list price</dt><dd>{cost(item().cost!)}</dd></Show>
          </dl>
        }</Show>
        <Show when={route().source === "empirical" && route().alternatives.length > 0}>
          <h4>{route().evidenceQualifiedAlternatives ? "Evidence-qualified alternatives" : "Alternatives within constraints"}</h4>
          <ul><For each={route().alternatives}>{item =>
            <li><code>{item.model}</code> · {item.context}
              <Show when={item.evidence !== null}> · {count(item.evidence)} observations</Show>
            </li>
          }</For></ul>
        </Show>
      </details>
    </section>
    <Show when={props.state.metrics}>{metrics =>
      <section class="cr-card">
        <h3>Execution measurements</h3>
        <dl class="cr-values">
          <Show when={metrics().input !== null}><dt>Input tokens</dt><dd>{count(metrics().input)}</dd></Show>
          <Show when={metrics().output !== null}><dt>Output tokens</dt><dd>{count(metrics().output)}</dd></Show>
          <Show when={metrics().total !== null}><dt>Total tokens</dt><dd>{count(metrics().total)}</dd></Show>
          <Show when={metrics().seconds !== null}><dt>Duration</dt><dd>{metrics().seconds!.toFixed(2)} s</dd></Show>
          <Show when={metrics().cost !== null}><dt>Estimated list price</dt><dd>{cost(metrics().cost!)}</dd></Show>
        </dl>
      </section>
    }</Show>
  </div>;
}

function RoutingPanel(props: PanelProps & { openSource: (url: string) => void }) {
  const [state, setState] = createSignal<RoutingState>();
  const [availability, setAvailability] = createSignal<"connecting" | "ready" | "unavailable">("connecting");
  const [retry, setRetry] = createSignal(0);

  createEffect(() => {
    retry();
    const session = props.session;
    const location = session.location;
    const client = session.server.client;
    setState(undefined);
    if (!location || !session.server.connected) {
      setAvailability("connecting");
      return;
    }

    setAvailability("connecting");
    const controller = new AbortController();
    const rpc = client.rpc(routingRpc);
    let refreshing = false;
    let queued = false;
    const refresh = async () => {
      if (refreshing) {
        queued = true;
        return;
      }
      refreshing = true;
      do {
        queued = false;
        try {
          const result = await rpc.snapshot({ sessionID: session.id }, {
            location: { directory: location.directory }, signal: controller.signal,
          }) as { state: RoutingState | null };
          if (!controller.signal.aborted) {
            setState(result.state ?? undefined);
            setAvailability(result.state ? "ready" : "unavailable");
          }
        } catch {
          if (!controller.signal.aborted) setAvailability("unavailable");
        }
      } while (queued && !controller.signal.aborted);
      refreshing = false;
    };

    const unsubscribe = rpc.events.on("updated", event => {
      if ((event.data as { sessionID: string }).sessionID === session.id
        && event.location.directory === location.directory) void refresh();
    }, { signal: controller.signal });
    onCleanup(() => {
      controller.abort();
      unsubscribe();
    });
    void refresh();
  });

  const workspace = () => props.session.directory.split(/[\\/]/).filter(Boolean).at(-1);
  return <div class="cr-panel">
    <header class="cr-header">
      <div><h2>ContextRoute</h2><p>{workspace()}</p></div>
      <span class="cr-status" data-stage={state()?.stage} aria-live="polite">
        {state() ? labels[state()!.stage] : availability() === "connecting" ? "Connecting" : "Unavailable"}
      </span>
    </header>
    <Show when={availability() === "unavailable"}>
      <section class="cr-card cr-empty">
        <p>Routing status is unavailable. Check that ContextRoute is configured in this project and the session server is connected.</p>
        <button type="button" onClick={() => setRetry(n => n + 1)}>Retry</button>
      </section>
    </Show>
    <Show when={availability() === "ready" && state()}>{current =>
      <>
        <Show when={current().error}>
          <p class="cr-error" role="status">ContextRoute reported an integration error <code>{current().error}</code>.</p>
        </Show>
        <Show when={current().route} fallback={<p class="cr-empty" role="status">{
          current().stage === "waiting" ? "Submit a prompt in this session to see its routing decision." :
          current().stage === "error" ? "This prompt has no completed routing decision." : "Preparing a routing decision for this prompt."
        }</p>}>
          <Show when={current().time}><p class="cr-time">Latest prompt · {
            new Date(current().time!).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
          }</p></Show>
          <Details state={current()} openSource={props.openSource} />
        </Show>
      </>
    }</Show>
  </div>;
}

const tab = { id: "routing", title: "ContextRoute", pinned: true } as const;
const tabs = [tab];

export default Plugin.define({
  setup(ctx) {
    ctx.add(Style, css);
    ctx.add(Panel, {
      id: tab.id, region: "side", width: 370,
      list: () => tabs,
      render: props => <RoutingPanel {...props} openSource={url => ctx.system.openExternal(url)} />,
    });
    ctx.add(Command, () => ({
      id: "show-routing",
      title: "ContextRoute: Show Routing Decision",
      description: "Inspect the current session's model and repository context",
      section: "session" as const,
      enabled: Boolean(ctx.sessions.current()),
      run: () => {
        const session = ctx.sessions.current();
        if (session) ctx.layout.open(ctx.id + ":" + tab.id, session);
      },
    }));
  },
});

