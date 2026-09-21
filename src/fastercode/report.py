import os

from fpdf import FPDF

_NA = "-"

_CONDITION_FILL = {
    "accepted": (198, 239, 206),            # light green
    "constraint_violated": (255, 214, 153), # light orange
    "timeout": (255, 242, 174),             # light yellow
}

_BASELINE_FILL = (225, 225, 225)  # light gray - reference row, not an attempt

_METRIC_UNITS = {
    "speed": "ms",
    "loc": "lines",
    "memory": "KB",
}


def _fmt(value, fmt=None):
    if value is None:
        return _NA
    if fmt is not None:
        return format(value, fmt)
    return str(value)


def _line(pdf, text):
    pdf.cell(0, 7, text=text, new_x="LMARGIN", new_y="NEXT")


def _code_block(pdf, code_lines):
    text = "\n".join(code_lines) if code_lines else "(no code)"
    pdf.set_font("courier", size=8)
    pdf.multi_cell(0, 4.5, text=text, border=1, align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=10)


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


def generate_pdf(result, output_path, transcript_entries=None):
    """
    Renders an optimise() result dict (see core.optimise()) to a PDF summary
    at output_path. Returns output_path.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    transcript_by_try = {}
    for entry in (transcript_entries or []):
        transcript_by_try[entry["try"]] = entry

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("helvetica", "B", 16)
    _line(pdf, "fastercode Optimisation Report")
    pdf.ln(2)

    pdf.set_font("helvetica", size=11)
    _line(pdf, f"Function: {result.get('function') or '?'}")
    _line(pdf, f"Objective: {result['objective']}    Mode: {result['mode']}")
    _line(pdf, f"Tries requested: {result['tries_requested']}")
    _line(pdf, f"Generated: {result.get('generated_at') or ''}")

    provider = result.get("provider") or {}
    provider_line = f"Provider: {provider.get('name', '?')}"
    if provider.get("model"):
        provider_line += f" (model: {provider['model']})"
    _line(pdf, provider_line)

    _line(pdf, f"Constraints: {_format_constraints(result.get('constraints'))}")

    entry_point = result.get("entry_point")
    if result.get("use_captured_inputs"):
        _line(pdf, "Verification: captured-inputs replay (use_captured_inputs=True)")
    else:
        verification_line = "Verification: entry-script replay"
        if entry_point:
            verification_line += f" (entry_point: {entry_point})"
        _line(pdf, verification_line)

    pdf.ln(4)

    original = result["original"]
    pdf.set_font("helvetica", "B", 13)
    _line(pdf, "Baseline")
    pdf.set_font("helvetica", size=10)
    _line(
        pdf,
        f"Execution time: {_fmt(original['execution_time_ms'], '.4f')} ms   "
        f"LOC: {_fmt(original['loc_no_comments'])}   "
        f"Peak memory: {_fmt(original.get('peak_memory_kb'), '.2f')} KB",
    )
    _code_block(pdf, result["original_code"])
    pdf.ln(4)

    pdf.set_font("helvetica", "B", 13)
    _line(pdf, "Attempts")
    col_widths = (14, 20, 32, 26, 18, 24)
    headers = ("Try", "Version", "Condition", "Exec (ms)", "LOC", "Mem (KB)")
    pdf.set_font("helvetica", "B", 9)
    for width, header in zip(col_widths, headers):
        pdf.cell(width, 7, text=header, border=1)
    pdf.ln(7)

    pdf.set_font("helvetica", size=9)
    pdf.set_fill_color(*_BASELINE_FILL)
    baseline_row = (
        "-", "-", "Baseline",
        _fmt(original["execution_time_ms"], ".4f"),
        _fmt(original["loc_no_comments"]),
        _fmt(original.get("peak_memory_kb"), ".2f"),
    )
    for width, value in zip(col_widths, baseline_row):
        pdf.cell(width, 6, text=value, border=1, fill=True)
    pdf.ln(6)

    for attempt in result["attempts"]:
        fill = _CONDITION_FILL.get(attempt["condition"])
        if fill:
            pdf.set_fill_color(*fill)
        row = (
            str(attempt["try"]),
            _fmt(attempt["version"]),
            attempt["condition"],
            _fmt(attempt["execution_time_ms"], ".4f"),
            _fmt(attempt["loc_no_comments"]),
            _fmt(attempt.get("peak_memory_kb"), ".2f"),
        )
        for width, value in zip(col_widths, row):
            pdf.cell(width, 6, text=value, border=1, fill=bool(fill))
        pdf.ln(6)
    pdf.ln(4)

    pdf.set_font("helvetica", "B", 13)
    _line(pdf, "Winner")
    pdf.set_font("helvetica", size=10)
    if result["winner_version"] is None:
        _line(pdf, "No candidate improved on the baseline - original code retained.")
    else:
        _line(pdf, f"Version {result['winner_version']} - metric {result['winner_metric']}")
        baseline_metric = {
            "loc": original["loc_no_comments"],
            "memory": original.get("peak_memory_kb"),
        }.get(result["objective"], original["execution_time_ms"])
        if baseline_metric:
            improvement = 100 * (1 - (result["winner_metric"] / baseline_metric))
            _line(pdf, f"Improvement over baseline: {improvement:.1f}%")
        else:
            _line(pdf, "Improvement over baseline: unavailable")
    pdf.ln(4)

    pdf.set_font("helvetica", "B", 13)
    _line(pdf, "Attempt details")
    for attempt in result["attempts"]:
        fill = _CONDITION_FILL.get(attempt["condition"])
        if fill:
            pdf.set_fill_color(*fill)
        is_winner = (
            result["winner_version"] is not None
            and attempt["version"] == result["winner_version"]
        )
        label = f"Try {attempt['try']} - version {_fmt(attempt['version'])} - {attempt['condition']}"
        if is_winner:
            label += "  * WINNER"
        pdf.set_font("helvetica", "B", 11)
        pdf.cell(0, 7, text=label, new_x="LMARGIN", new_y="NEXT", fill=bool(fill))

        pdf.set_font("helvetica", size=9)
        _line(
            pdf,
            f"Exec: {_fmt(attempt['execution_time_ms'], '.4f')} ms   "
            f"LOC: {_fmt(attempt['loc_no_comments'])}   "
            f"Memory: {_fmt(attempt.get('peak_memory_kb'), '.2f')} KB",
        )

        # Provenance: who wrote this candidate, and a hash of the exact code
        # that was executed to produce the numbers above. Lets a reader check
        # a claimed improvement against the bytes it was actually measured on.
        source = attempt.get("source")
        digest = attempt.get("code_sha256")
        if source or digest:
            origin = {"agent": "agent-supplied", "llm": "LLM-generated"}.get(source, source or "unknown")
            _line(pdf, f"Source: {origin}   Executed code SHA-256: {digest or _NA}")

        transcript_entry = transcript_by_try.get(attempt["try"])

        pdf.set_font("helvetica", "B", 9)
        _line(pdf, "Code sent to LLM")
        if transcript_entry is None:
            pdf.set_font("helvetica", "I", 9)
            _line(pdf, "(transcript not available)")
        elif transcript_entry.get("sent_code"):
            _code_block(pdf, transcript_entry["sent_code"])
        else:
            pdf.set_font("helvetica", "I", 9)
            _line(pdf, "(nothing sent - submit() failed before a candidate was produced)")

        pdf.set_font("helvetica", "B", 9)
        _line(pdf, "Code received from LLM")
        if transcript_entry is None:
            pdf.set_font("helvetica", "I", 9)
            _line(pdf, "(transcript not available)")
        elif transcript_entry.get("received_code"):
            _code_block(pdf, transcript_entry["received_code"])
        else:
            pdf.set_font("helvetica", "I", 9)
            _line(pdf, "(nothing received - submit() failed before a candidate was produced)")

        pdf.set_font("helvetica", "B", 9)
        _line(pdf, "De-anonymized code")
        if attempt.get("code"):
            _code_block(pdf, attempt["code"])
        else:
            pdf.set_font("helvetica", "I", 9)
            _line(pdf, "(no code recorded - failed before a candidate was produced)")

        pdf.set_font("helvetica", "B", 9)
        _line(pdf, "Pseudonym mapping")
        mapping = transcript_entry.get("mapping") if transcript_entry else None
        if mapping:
            pdf.set_font("helvetica", size=9)
            for pseudonym, real in sorted(mapping.items()):
                _line(pdf, f"  {pseudonym} -> {real}")
        else:
            pdf.set_font("helvetica", "I", 9)
            _line(pdf, "(privacy_mode was off - no pseudonyms used)")

        if attempt.get("error"):
            pdf.set_font("helvetica", "I", 9)
            _line(pdf, f"Error: {attempt['error']}")

        pdf.set_font("helvetica", size=10)
        pdf.ln(3)

    pdf.output(output_path)
    return output_path
