import html
import json
import os
from difflib import SequenceMatcher

from .core import _outputs_match

_NA = "-"

_METRIC_UNITS = {"speed": "ms", "loc": "lines", "memory": "KB"}
_METRIC_LABELS = {"speed": "execution time", "loc": "lines of code", "memory": "peak memory"}
_METRIC_KEYS = {"loc": "loc_no_comments", "memory": "peak_memory_kb"}
_METRIC_DIGITS = {"speed": 4, "memory": 2}

# condition -> (color token used in CSS, human label)
_CONDITION_STYLE = {
    "accepted": ("green", "Accepted"),
    "constraint_violated": ("purple", "Constraint violated"),
    "timeout": ("amber", "Timeout"),
}


def _esc(text):
    return html.escape(str(text)) if text is not None else ""


def _fmt(value, digits=None):
    if value is None:
        return _NA
    if digits is not None:
        return f"{value:.{digits}f}"
    return str(value)


def _pretty(value):
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return repr(value)


def _metric_value(container, objective):
    return container.get(_METRIC_KEYS.get(objective, "execution_time_ms"))


def _format_constraints(constraints):
    if not constraints:
        return "none"
    parts = []
    for metric, spec in constraints.items():
        unit = _METRIC_UNITS.get(metric, "")
        if "max" in spec:
            parts.append(f"{metric} <= {spec['max']} {unit}".strip())
        else:
            parts.append(f"{metric} <= {spec['max_relative'] * 100:.0f}% of baseline")
    return ", ".join(parts)


def _improvement_pct(result):
    if result["winner_version"] is None:
        return None
    baseline_metric = _metric_value(result["original"], result["objective"])
    if not baseline_metric:
        return None
    return 100 * (1 - (result["winner_metric"] / baseline_metric))


def _render_pane(rows, empty_message):
    if not rows:
        return f'<div class="diff-empty">{_esc(empty_message)}</div>'
    parts = []
    for number, kind, line in rows:
        css_class = f" line-{kind}" if kind else ""
        parts.append(
            f'<div class="diff-line{css_class}"><span class="line-num">{number}</span>'
            f'<span class="line-code">{_esc(line) or "&nbsp;"}</span></div>'
        )
    return "".join(parts)


def _diff_panes(before_lines, after_lines):
    # Line-level diff via SequenceMatcher, rendered as two aligned panes
    # (each with its own line numbering) rather than a unified patch - matches
    # how the reference UI shows a before/after pair, just with real
    # add/remove highlighting instead of two plain code dumps.
    before_lines = before_lines or []
    after_lines = after_lines or []
    before_rows, after_rows = [], []
    before_no = after_no = 0
    matcher = SequenceMatcher(a=before_lines, b=after_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in before_lines[i1:i2]:
                before_no += 1
                after_no += 1
                before_rows.append((before_no, "", line))
                after_rows.append((after_no, "", line))
        else:
            for line in before_lines[i1:i2]:
                before_no += 1
                before_rows.append((before_no, "removed", line))
            for line in after_lines[j1:j2]:
                after_no += 1
                after_rows.append((after_no, "added", line))
    return before_rows, after_rows


def _code_block(lines, empty_message="(no code)"):
    if not lines:
        return f'<div class="code-empty">{_esc(empty_message)}</div>'
    rows = "".join(
        f'<div class="diff-line"><span class="line-num">{i}</span>'
        f'<span class="line-code">{_esc(line) or "&nbsp;"}</span></div>'
        for i, line in enumerate(lines, start=1)
    )
    return f'<div class="code-pane">{rows}</div>'


def _mapping_block(mapping):
    if not mapping:
        return '<p class="muted">(privacy_mode was off - no pseudonyms used)</p>'
    rows = "".join(
        f'<div class="mapping-row"><code>{_esc(pseudonym)}</code> &rarr; <code>{_esc(real)}</code></div>'
        for pseudonym, real in sorted(mapping.items())
    )
    return f'<div class="mapping-block">{rows}</div>'


def _recorded_calls_table(original_inputs, original_outputs, winner_outputs):
    original_inputs = original_inputs or []
    original_outputs = original_outputs or []
    rows = []
    for i, inputs in enumerate(original_inputs):
        original_output = original_outputs[i] if i < len(original_outputs) else None
        winner_output = winner_outputs[i] if winner_outputs is not None and i < len(winner_outputs) else None
        if winner_outputs is None:
            status_class, status_label = "muted", "no winner"
        elif _outputs_match(original_output, winner_output):
            status_class, status_label = "io-match", "match"
        else:
            status_class, status_label = "io-mismatch", "mismatch"
        rows.append(
            "<tr>"
            f"<td>{i + 1}</td>"
            f"<td>{_esc(_pretty(inputs))}</td>"
            f"<td>{_esc(_pretty(original_output))}</td>"
            f"<td>{_esc(_pretty(winner_output)) if winner_outputs is not None else _NA}</td>"
            f'<td class="{status_class}">{status_label}</td>'
            "</tr>"
        )
    return "".join(rows)


def _attempt_section(attempt, transcript_entry, is_winner):
    color, label = _CONDITION_STYLE.get(attempt["condition"], ("red", "Rejected"))
    winner_tag = ' <span class="badge badge-winner">WINNER</span>' if is_winner else ""

    source = attempt.get("source")
    origin_label = {"agent": "agent-supplied", "llm": "LLM-generated"}.get(source, source or "unknown")
    correct = attempt.get("correct")
    correct_label = "yes" if correct is True else "no" if correct is False else _NA

    if source == "agent":
        transcript_html = (
            '<p class="muted">Provided directly by agent - no LLM was called for this candidate.</p>'
        )
    elif transcript_entry is None:
        transcript_html = '<p class="muted">(transcript not available)</p>'
    else:
        sent_empty = "(nothing sent - submit() failed before a candidate was produced)"
        received_empty = "(nothing received - submit() failed before a candidate was produced)"
        transcript_html = (
            f'<div class="subsection"><h4>Code sent to LLM</h4>'
            f'{_code_block(transcript_entry.get("sent_code"), sent_empty)}</div>'
            f'<div class="subsection"><h4>Code received from LLM</h4>'
            f'{_code_block(transcript_entry.get("received_code"), received_empty)}</div>'
            f'<div class="subsection"><h4>Pseudonym mapping</h4>'
            f'{_mapping_block(transcript_entry.get("mapping"))}</div>'
        )

    error_html = (
        f'<p class="error-text">Error: {_esc(attempt["error"])}</p>' if attempt.get("error") else ""
    )
    code_empty = "(no code recorded - failed before a candidate was produced)"

    return f'''
    <details class="attempt-row cond-{color}">
      <summary>
        <span class="chevron"></span>
        <span class="attempt-title">Try {attempt["try"]} &middot; version {_fmt(attempt["version"])}</span>
        <span class="badge badge-{color}">{label}</span>{winner_tag}
        <span class="attempt-metrics">{_fmt(attempt["execution_time_ms"], 4)} ms &middot; {_fmt(attempt["loc_no_comments"])} LOC &middot; {_fmt(attempt.get("peak_memory_kb"), 2)} KB</span>
      </summary>
      <div class="attempt-body">
        <p class="muted">Source: {_esc(origin_label)} &middot; Correct: {correct_label} &middot; SHA-256: <code>{_esc(attempt.get("code_sha256") or _NA)}</code></p>
        {error_html}
        <div class="subsection"><h4>Executed code</h4>{_code_block(attempt.get("code"), code_empty)}</div>
        {transcript_html}
      </div>
    </details>
    '''


def _css():
    return """
:root {
  --bg: #111111;
  --bg-white: #262626;
  --bg-subtle: #333333;
  --bg-code: #0d0d0d;
  --border: #8a8a8a;
  --text-primary: #f0f0f0;
  --text-secondary: #b8b8b8;
  --green: #16a34a; --green-dim: rgba(22,163,74,0.22); --green-text: #4ade80;
  --amber: #d97706; --amber-dim: rgba(217,119,6,0.22); --amber-text: #fbbf24;
  --red: #dc2626; --red-dim: rgba(220,38,38,0.22); --red-text: #f87171;
  --purple: #7c3aed; --purple-dim: rgba(124,58,237,0.22); --purple-text: #a78bfa;
  --primary: #39FF14;
  --primary-text: #39FF14;
  --primary-soft: rgba(57,255,20,0.22);
  --radius: 8px;
  --radius-lg: 12px;
  --shadow-sm: 0 3px 8px rgba(0,0,0,0.55);
  --font-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: var(--font-ui); background: var(--bg); color: var(--text-primary); font-size: 14px; line-height: 1.5; }
.page-header { padding: 20px 32px; border-bottom: 1px solid var(--border); background: var(--bg-white); }
.brand-row { display: flex; align-items: center; gap: 8px; }
.brand-link { display: inline-flex; align-items: center; gap: 8px; text-decoration: none; color: inherit; }
.brand-link:hover .brand-name { text-decoration: underline; }
.brand-mark { display: inline-flex; }
.brand-mark svg { display: block; }
.brand-name { font-size: 18px; font-weight: 700; color: var(--text-primary); }
.title-sep { font-size: 18px; font-weight: 700; color: var(--text-secondary); }
.page-header .subtitle { font-family: var(--font-mono); color: var(--text-secondary); margin-top: 4px; }
.page { display: grid; grid-template-columns: 1fr 300px; gap: 24px; padding: 24px 32px; align-items: start; }
.content { display: flex; flex-direction: column; gap: 24px; min-width: 0; }
.section, .stat-card, .attempt-row { background: var(--bg-white); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-sm); }
.section { padding: 20px; }
.section-head h3 { margin: 0 0 4px; font-size: 15px; }
.muted { color: var(--text-secondary); font-size: 12.5px; margin: 0; }
.stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat-card { padding: 16px; }
.stat-card.highlight { background: var(--primary-soft); border-color: var(--primary); }
.stat-val { font-size: 22px; font-weight: 700; font-family: var(--font-mono); }
.stat-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-secondary); margin-top: 4px; }
.diff-wrap { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
.diff-pane { background: var(--bg-code); border: 1px solid var(--border); border-radius: var(--radius); overflow: auto; max-height: 480px; }
.diff-pane-head { position: sticky; top: 0; padding: 8px 12px; background: var(--bg-subtle); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-secondary); border-bottom: 1px solid var(--border); }
.diff-line { display: flex; font-family: var(--font-mono); font-size: 12.5px; }
.line-num { flex: 0 0 40px; text-align: right; padding: 1px 10px; color: #666; user-select: none; }
.line-code { padding: 1px 10px 1px 0; white-space: pre-wrap; word-break: break-word; }
.line-added { background: var(--green-dim); }
.line-added .line-code { color: var(--green-text); }
.line-removed { background: var(--red-dim); }
.line-removed .line-code { color: var(--red-text); }
.code-pane { background: var(--bg-code); border: 1px solid var(--border); border-radius: var(--radius); overflow: auto; max-height: 400px; margin-top: 6px; }
.code-empty, .diff-empty { padding: 12px; color: var(--text-secondary); font-style: italic; font-size: 12.5px; }
table.io-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12.5px; }
table.io-table th { text-align: left; text-transform: uppercase; letter-spacing: 0.04em; font-size: 10.5px; color: var(--text-secondary); border-bottom: 1px solid var(--border); padding: 6px 8px; }
table.io-table td { border-bottom: 1px solid var(--border); padding: 6px 8px; font-family: var(--font-mono); vertical-align: top; }
.io-match { color: var(--green-text); }
.io-mismatch { color: var(--red-text); }
.badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.badge-green { background: var(--green-dim); color: var(--green-text); border: 1px solid var(--green); }
.badge-amber { background: var(--amber-dim); color: var(--amber-text); border: 1px solid var(--amber); }
.badge-red { background: var(--red-dim); color: var(--red-text); border: 1px solid var(--red); }
.badge-purple { background: var(--purple-dim); color: var(--purple-text); border: 1px solid var(--purple); }
.badge-winner { background: var(--primary-soft); color: var(--primary-text); border: 1px solid var(--primary); margin-left: 6px; }
.attempts-list { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
.attempt-row { padding: 0; overflow: hidden; }
.attempt-row.cond-green { border-left: 6px solid var(--green); }
.attempt-row.cond-amber { border-left: 6px solid var(--amber); }
.attempt-row.cond-red { border-left: 6px solid var(--red); }
.attempt-row.cond-purple { border-left: 6px solid var(--purple); }
.attempt-row > summary { list-style: none; cursor: pointer; padding: 12px 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.attempt-row > summary::-webkit-details-marker { display: none; }
.chevron { width: 8px; height: 8px; border-right: 2px solid var(--text-secondary); border-bottom: 2px solid var(--text-secondary); transform: rotate(-45deg); transition: transform 0.15s ease; flex: none; }
.attempt-title { font-family: var(--font-mono); font-weight: 600; }
.attempt-metrics { margin-left: auto; font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }
.attempt-body { padding: 0 16px 16px; border-top: 1px solid var(--border); }
.subsection { margin-top: 14px; }
.subsection h4 { margin: 0 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-secondary); }
.mapping-block { font-family: var(--font-mono); font-size: 12.5px; background: var(--bg-code); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px 12px; margin-top: 6px; }
.mapping-row { padding: 2px 0; }
.error-text { color: var(--red-text); font-size: 12.5px; }
.sidebar { display: flex; flex-direction: column; gap: 16px; }
.sidebar-section { background: var(--bg-white); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px; }
.sidebar-section h4 { margin: 0 0 10px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-secondary); }
.info-row { display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; font-size: 12.5px; border-bottom: 1px solid var(--border); }
.info-row:last-child { border-bottom: none; }
.info-row span:first-child { color: var(--text-secondary); }
.info-row span:last-child { text-align: right; word-break: break-word; }
.page-footer { padding: 16px 32px 32px; color: var(--text-secondary); font-size: 11.5px; }
@media (max-width: 900px) {
  .page { grid-template-columns: 1fr; }
  .stats-row { grid-template-columns: repeat(2, 1fr); }
  .diff-wrap { grid-template-columns: 1fr; }
}
"""


def generate_html(result, output_path, transcript_entries=None):
    """
    Renders an optimise() result dict (see core.optimise()) to a self-contained,
    static HTML report at output_path. Returns output_path.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    transcript_by_try = {entry["try"]: entry for entry in (transcript_entries or [])}
    original = result["original"]
    objective = result["objective"]

    winner_attempt = None
    if result["winner_version"] is not None:
        for attempt in result["attempts"]:
            if attempt["version"] == result["winner_version"]:
                winner_attempt = attempt
                break

    improvement = _improvement_pct(result)
    digits = _METRIC_DIGITS.get(objective)
    baseline_metric_html = _fmt(_metric_value(original, objective), digits)
    winner_metric_html = _fmt(result.get("winner_metric"), digits) if winner_attempt is not None else _NA
    improvement_html = f"{improvement:.1f}%" if improvement is not None else _NA

    calls_total = len(result.get("original_inputs") or [])
    winner_outputs = winner_attempt.get("outputs") if winner_attempt else None
    calls_verified_html = f"{calls_total}"
    if winner_outputs is not None:
        original_outputs = original.get("outputs") or []
        verified = sum(
            1 for o, w in zip(original_outputs, winner_outputs) if _outputs_match(o, w)
        )
        calls_verified_html = f"{verified}/{calls_total}"

    provider = result.get("provider") or {}
    provider_label = provider.get("name", "?")
    if provider.get("model"):
        provider_label += f" ({provider['model']})"

    if result.get("use_captured_inputs"):
        verification_label = "Captured-inputs replay"
    else:
        verification_label = "Entry-script replay"
        if result.get("entry_point"):
            verification_label += f" ({result['entry_point']})"

    if winner_attempt is not None:
        before_rows, after_rows = _diff_panes(result["original_code"], winner_attempt.get("code"))
        winner_html = f'''
        <div class="section">
          <div class="section-head">
            <h3>Winner &mdash; version {_fmt(winner_attempt["version"])}</h3>
            <p class="muted">{improvement_html} improvement over baseline</p>
          </div>
          <div class="diff-wrap">
            <div class="diff-pane"><div class="diff-pane-head">Before</div>{_render_pane(before_rows, "(no code)")}</div>
            <div class="diff-pane"><div class="diff-pane-head">After</div>{_render_pane(after_rows, "(no code)")}</div>
          </div>
        </div>
        '''
    else:
        winner_html = '''
        <div class="section">
          <div class="section-head"><h3>Winner</h3></div>
          <p class="muted">No candidate improved on the baseline - original code retained.</p>
        </div>
        '''

    calls_section_html = ""
    if calls_total:
        rows_html = _recorded_calls_table(result.get("original_inputs"), original.get("outputs"), winner_outputs)
        calls_section_html = f'''
        <div class="section">
          <div class="section-head">
            <h3>Recorded calls</h3>
            <p class="muted">{calls_total} call(s) recorded from the original function</p>
          </div>
          <table class="io-table">
            <thead><tr><th>#</th><th>Inputs</th><th>Original output</th><th>Winner output</th><th>Status</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
        '''

    attempts_html = "".join(
        _attempt_section(
            attempt, transcript_by_try.get(attempt["try"]),
            winner_attempt is not None and attempt["version"] == winner_attempt["version"],
        )
        for attempt in result["attempts"]
    )

    stats_html = f'''
    <div class="stats-row">
      <div class="stat-card highlight"><div class="stat-val">{improvement_html}</div><div class="stat-label">Improvement</div></div>
      <div class="stat-card"><div class="stat-val">{baseline_metric_html}</div><div class="stat-label">Baseline {_METRIC_LABELS[objective]}</div></div>
      <div class="stat-card"><div class="stat-val">{winner_metric_html}</div><div class="stat-label">Winner {_METRIC_LABELS[objective]}</div></div>
      <div class="stat-card"><div class="stat-val">{calls_verified_html}</div><div class="stat-label">Recorded calls verified</div></div>
    </div>
    '''

    sidebar_html = f'''
    <div class="sidebar">
      <div class="sidebar-section">
        <h4>Run info</h4>
        <div class="info-row"><span>Function</span><span>{_esc(result.get("function") or "?")}</span></div>
        <div class="info-row"><span>Objective</span><span>{_esc(objective)}</span></div>
        <div class="info-row"><span>Mode</span><span>{_esc(result["mode"])}</span></div>
        <div class="info-row"><span>Verification</span><span>{_esc(verification_label)}</span></div>
        <div class="info-row"><span>Provider</span><span>{_esc(provider_label)}</span></div>
        <div class="info-row"><span>Constraints</span><span>{_esc(_format_constraints(result.get("constraints")))}</span></div>
        <div class="info-row"><span>Tries requested</span><span>{_esc(result["tries_requested"])}</span></div>
      </div>
    </div>
    '''

    html_doc = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>fastercode optimisation report - {_esc(result.get("function") or "")}</title>
<style>{_css()}</style>
</head>
<body>
  <header class="page-header">
    <div class="brand-row">
      <a href="https://fastercode.ai" class="brand-link" target="_blank" rel="noopener">
        <span class="brand-mark" aria-hidden="true"><svg width="18" height="18" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"><rect width="64" height="64" rx="16" fill="var(--primary)"></rect></svg></span>
        <span class="brand-name">fastercode.ai</span>
      </a>
      <span class="title-sep">optimisation report</span>
    </div>
    <div class="subtitle">{_esc(result.get("function") or "?")}</div>
  </header>
  <div class="page">
    <div class="content">
      {stats_html}
      {winner_html}
      {calls_section_html}
      <div class="section">
        <div class="section-head">
          <h3>All attempts</h3>
          <p class="muted">{len(result["attempts"])} attempt(s), including any rejected candidates</p>
        </div>
        <div class="attempts-list">{attempts_html}</div>
      </div>
    </div>
    {sidebar_html}
  </div>
  <footer class="page-footer">Generated {_esc(result.get("generated_at") or "")}</footer>
  <script>
    document.querySelectorAll('details.attempt-row').forEach(function (d) {{
      d.addEventListener('toggle', function () {{
        var chevron = d.querySelector(':scope > summary .chevron');
        if (chevron) {{ chevron.style.transform = d.open ? 'rotate(45deg)' : 'rotate(-45deg)'; }}
      }});
    }});
  </script>
</body>
</html>'''

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    return output_path
