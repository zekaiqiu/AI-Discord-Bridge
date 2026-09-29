"""Shared prompt instruction blocks injected into model turns.

Extracted from claude_runner so the haihub agent path can inject the
IDENTICAL text — keeping the inline-artifacts / pre-installed-libs /
quant-CLI / image-gen-marker contract in one place instead of letting two
provider runners drift. Both the claude CLI path and the haihub tool path
write artifacts to the same per-session dir, which app.py collects after
the turn (``_collect_user_container_artifacts`` / ``_scan_new_artifacts`` /
``_process_image_requests``).
"""
from __future__ import annotations


def artifacts_instructions(artifacts_path: str) -> str:
    """Return the full inline-artifacts + libs + quant-CLI + image-gen
    instruction block, with ``artifacts_path`` interpolated into the save
    path and the image-marker path. Caller prepends it to the prompt; the
    returned text ends with a blank line so it concatenates cleanly.
    """
    return (
        "[Inline artifacts — when you generate plots, charts, tables, "
        "diagrams, spreadsheets, documents, or any sample file the "
        "user asked for, save them as files to:\n"
        f"  {artifacts_path}/<name>.<ext>\n"
        "Supported extensions (each renders with a typed inline "
        "preview in the user's view):\n"
        "  - images: .png .jpg .jpeg .webp .gif .svg\n"
        "  - video: .mp4 .mov .webm .mkv .avi (native <video> player)\n"
        "  - audio: .mp3 .wav .flac .ogg .m4a (native <audio> player)\n"
        "  - tables: .csv .tsv\n"
        "  - columnar data: .parquet .feather .arrow (decoded in "
        "browser to a tabular preview; preferred over CSV for any "
        "wide / OHLCV / quant data — write with "
        "`df.to_parquet('out.parquet')`)\n"
        "  - spreadsheets: .xlsx .xls .ods (multi-sheet preview)\n"
        "  - docs: .pdf .html .htm\n"
        "  - office (download-only inline; PREFER exporting a .pdf "
        "alongside for inline preview): .docx .doc .pptx .ppt\n"
        "  - structured: .json .xml .xsl .xslt\n"
        "  - geo: .geojson (rendered as JSON) .kml (rendered as XML); "
        "no map preview yet\n"
        "  - notebooks: .ipynb (parsed cell-by-cell; code + markdown + "
        "image / html / text outputs render inline)\n"
        "  - diagrams: .mmd (mermaid; rendered as SVG); .dot .puml "
        "(source-only — no graph rendering yet); .drawio (XML); "
        ".excalidraw (JSON)\n"
        "  - 3D / CAD: .obj .stl .gltf (text/JSON formats — source "
        "preview); .glb (binary — download-only)\n"
        "  - archives (download-only): .zip .tar .gz\n"
        "  - fonts (sample-text preview via @font-face): .ttf .otf "
        ".woff .woff2\n"
        "  - code/text (rendered with syntax highlighting): .py .js "
        ".jsx .ts .tsx .go .rs .java .kt .swift .c .h .cpp .hpp .cs "
        ".rb .php .pl .lua .sh .bash .sql .r .yaml .yml .toml .ini "
        ".md .txt .log .scala .dart .ex .exs .clj .cljs .hs .zig "
        ".nim .jl\n"
        "Rules:\n"
        "  - Name files with short, lowercase, descriptive basenames "
        "(e.g. revenue_q3.png, schema.svg, sample.xsl). Avoid spaces "
        "and hex hashes.\n"
        "  - Use Bash with `mkdir -p` first if the dir doesn't exist.\n"
        "  - For matplotlib: `plt.savefig(<path>)`. For plotly HTML: "
        "`fig.write_html(<path>)`. For tables: write CSV/JSON directly. "
        "For PDFs: reportlab / fpdf2 / weasyprint. For spreadsheets: "
        "openpyxl / pandas.to_excel. For .docx: python-docx. For "
        ".pptx: python-pptx. For notebooks: nbformat. For audio: "
        "numpy + scipy.io.wavfile or pydub. For zips: zipfile.\n"
        "  - When the user asks for a 'sample <type> file' or 'an "
        "example of X', SAVE the file as an artifact rather than only "
        "pasting code in the response — they want a downloadable "
        "result they can preview. The code can still appear in the "
        "response as explanation.\n"
        "  - Skip artifacts entirely for plain conversational answers "
        "where no file is being requested or generated.]\n\n"
        "[Pre-installed Python libraries — `python3` (linuxbrew "
        "3.14) already has the following baked in; `import` them "
        "directly without `pip install`:\n"
        "  - data / numerics: pandas, numpy, scipy, statsmodels, "
        "scikit-learn\n"
        "  - viz: matplotlib, seaborn, plotly\n"
        "  - data formats: pyarrow (parquet/feather/arrow), duckdb "
        "(in-process SQL on parquet/CSV/df), openpyxl, xlsxwriter, "
        "fastparquet\n"
        "  - market data: yfinance (Yahoo Finance OHLCV / fundamentals)\n"
        "  - backtesting: backtesting (single-asset event-driven; "
        "`from backtesting import Backtest, Strategy`)\n"
        "  - broker / exchange clients: ccxt (crypto exchanges), "
        "alpaca-py, polygon-api-client\n"
        "  - reporting: weasyprint, reportlab\n"
        "Use these by default for quant / data work — don't waste a "
        "turn pip-installing something that's already there. For "
        "OHLCV data default to `yfinance.download(...)`. For tabular "
        "research output prefer `df.to_parquet('out.parquet')` over "
        "CSV — the chat UI renders parquet inline. duckdb is great "
        "for ad-hoc SQL over a folder of parquet files: "
        "`duckdb.sql(\"select * from 'data/*.parquet'\")`.\n"
        "Heavier libs that need numba (vectorbt, quantstats, "
        "pandas-ta, empyrical-reloaded) are NOT in `python3` because "
        "numba doesn't yet support Python 3.14 — they ARE installed "
        "for the system python at `/usr/local/bin/python3` (3.12). "
        "Use `/usr/local/bin/python3 -c '...'` if you specifically "
        "need vectorbt or quantstats; otherwise stick to `python3`.\n"
        "FRED macro data: `from fredapi import Fred; "
        "f = Fred(api_key=os.environ['FRED_API_KEY']); "
        "f.get_series('UNRATE')` returns a pandas Series indexed by "
        "release date. ~800k US/global macro series — rates ('DGS10', "
        "'FEDFUNDS'), inflation ('CPIAUCSL', 'T5YIE'), employment "
        "('UNRATE', 'PAYEMS'), GDP ('GDPC1'), spreads ('BAMLH0A0HYM2'). "
        "Cache to `/workspace/data/fred/<series>.parquet` so future "
        "turns don't re-pull. If `FRED_API_KEY` isn't set in the env, "
        "tell the user to add it to their per-user container env "
        "(free key from fred.stlouisfed.org/docs/api/api_key.html).]\n\n"
        "[Quant-research CLI tools — pre-installed binaries on PATH; "
        "use these instead of rolling per-session reporting code:\n"
        "  - `tearsheet <returns.parquet> [--benchmark SPY] [--out tearsheet.pdf] [--title \"…\"]`\n"
        "    Reads a returns column from CSV/parquet/feather/xlsx, "
        "fetches benchmark via yfinance, renders a quantstats PDF "
        "(Sharpe / Sortino / drawdown / monthly heatmap / rolling "
        "vol / etc). Saves alongside the input by default. Pass "
        "`--benchmark none` to skip benchmark download. Use this "
        "for ANY end-of-research strategy summary — don't re-implement "
        "the chart code. The output is a normal .pdf artifact and "
        "renders inline.\n"
        "  - `robustness <returns.parquet> [--window 252] [--paths 1000] [--block 10] [--out rob.pdf]`\n"
        "    Runs (a) walk-forward rolling Sharpe so the user can "
        "see whether edge is regime-concentrated, and (b) stationary-"
        "bootstrap MC on the returns to produce 95% CIs for "
        "annualised return / vol / Sharpe / max DD / Calmar plus "
        "the share of bootstrap paths with negative cumulative "
        "return (the most direct overfit indicator). Output is a "
        "multi-page PDF + a sidecar .robustness.json with raw "
        "stats. Run this on EVERY backtest before declaring victory "
        "— a Sharpe 3 in-sample with a 95% CI of [0.2, 4.5] is "
        "telling you something different than one with [2.4, 3.6].\n"
        "Both CLIs accept the same input shapes (column named "
        "returns/ret/r/pnl_pct, or first numeric column; first column "
        "auto-promoted to DatetimeIndex if it parses as dates; "
        "percent-quoted inputs auto-detected and decimalized).]\n\n"
        "[AI image generation — you have an autonomous image-gen "
        "tool. To produce a photoreal photo, illustration, digital "
        "painting, or any AI-generated image, write a JSON marker "
        "file to the artifacts dir and the chat backend will call "
        "Gemini 2.5-flash-image and drop the result alongside it as "
        "an artifact in this same turn — no user toggle, no separate "
        "round-trip. Protocol:\n"
        f"  echo '{{\"prompt\":\"<vivid description>\",\"filename\":"
        "\"<name>.png\"}}' > "
        f"{artifacts_path}/_image_request_<unique>.json\n"
        "where <unique> is a short token like `1`, `cat`, `hk_skyline`. "
        "Use one marker file per image request; multiple markers per "
        "turn are fine. Rules:\n"
        "  - The marker name MUST start with `_image_request_` and "
        "end in `.json`. The backend deletes it after processing.\n"
        "  - Make the prompt vivid and specific (subject, style, "
        "lighting, composition) — Gemini takes a single prompt with "
        "no follow-up.\n"
        "  - The output filename should be a short, lowercase "
        "descriptive name ending in `.png` or `.jpg` "
        "(e.g. cat_flying_hk.png).\n"
        "  - Do NOT also paste a base64 image, ASCII art, or SVG "
        "fallback — the actual generated image will appear as an "
        "artifact under your response automatically.\n"
        "  - Briefly describe what you're generating in your reply "
        "(e.g. 'Generating a photoreal cat flying over Hong Kong "
        "harbour at dusk…') so the user sees intent before the "
        "image renders. Don't apologise for not having image-gen — "
        "you do, via this tool.\n"
        "Plotting (matplotlib, plotly, mermaid diagrams, hand-coded "
        "SVG) remains your job and uses the regular artifact path "
        "above — only photoreal / illustrative AI imagery uses the "
        "image-gen marker protocol.]\n\n"
        "[Scheduled wake — you CAN schedule yourself to act later, and "
        "it really fires (this is the ONLY mechanism that does; do not "
        "claim you'll 'check back' or 'ping' without using it). Write a "
        "JSON marker to the artifacts dir; the chat backend persists a "
        "durable timer and, when it fires, re-opens THIS thread, runs "
        "your stored instruction as a fresh turn, and your reply lands "
        "in the conversation. The user sees a clock indicator on the "
        "composer for any pending wakes. Protocol:\n"
        f"  echo '{{\"in\":\"30m\",\"prompt\":\"<what to do when this "
        "fires>\",\"note\":\"<short label>\"}}' > "
        f"{artifacts_path}/_schedule_request_<unique>.json\n"
        "Timing — provide exactly one of:\n"
        "  - \"in\": a relative delay like \"45s\", \"30m\", \"2h\", "
        "\"1d\" (bare number = seconds).\n"
        "  - \"at\": an absolute ISO-8601 time, e.g. "
        "\"2026-06-18T17:00:00Z\" (assume UTC if no offset given).\n"
        "  - \"every\": a recurring interval like \"1h\" or \"1d\" "
        "(fires repeatedly until the user cancels it).\n"
        "Rules:\n"
        "  - The marker name MUST start with `_schedule_request_` and "
        "end in `.json`. The backend deletes it after registering.\n"
        "  - \"prompt\" is an instruction to your FUTURE self — write it "
        "self-contained (the wake turn re-resumes this session so you "
        "keep context, but be explicit about what to produce).\n"
        "  - On wake you address the user directly; if they're away the "
        "message still posts to the thread and they see it on return.\n"
        "  - Use this when the user asks to be reminded, to follow up "
        "later, to check something after a delay, or to run a recurring "
        "report. Confirm in your reply what you scheduled and when.]\n\n"
    )


# ---------------------------------------------------------------------------
# Per-model self-identity.
#
# Each model option in the chat picker should KNOW which model it is — left to
# itself a model self-identifies from its training prior, which is often wrong
# (an Anthropic model may not know its exact version; a third-party model may
# even claim to be GPT/Claude). We inject an authoritative identity directive:
#   - Claude path  -> appended to --append-system-prompt (claude_runner)
#   - haihub/local -> prepended to the system message (haihub_runner)
#
# alias -> (display_name, model_id, vendor). Keep in sync with storage._VALID_MODELS
# and the frontend picker labels. The Claude entries track the CLI's current alias
# resolution (opus->Opus 4.8, sonnet->Sonnet 4.6, haiku->Haiku 4.5 as of 2026-06);
# update the version here when the `claude` CLI repoints an alias to a new release.
# "default" is intentionally omitted — it resolves to the account default at runtime,
# so we can't assert a specific identity for it.
MODEL_IDENTITY: dict[str, tuple[str, str, str]] = {
    # glm/kimi lead the lineup since the 2026-09-28 change (TokenHub-hosted);
    # the Anthropic aliases below remain mapped for sessions/paths that still
    # reference them but are no longer offered on the wizerith picker.
    "glm": ("GLM-5.3", "glm-5.3", "Z.ai"),
    "kimi": ("Kimi K3", "kimi-k3", "Moonshot AI"),
    "mimo": ("MiMo V2.6 Pro", "mimo-v2.6-pro", "Xiaomi"),
    "mimo-flash": ("MiMo V2.6 Flash", "mimo-v2.6-flash", "Xiaomi"),
    "opus": ("Claude Opus 4.8", "claude-opus-4-8", "Anthropic"),
    "sonnet": ("Claude Sonnet 4.6", "claude-sonnet-4-6", "Anthropic"),
    "haiku": ("Claude Haiku 4.5", "claude-haiku-4-5", "Anthropic"),
    "fable": ("Claude Fable 5.1", "claude-fable-5-1", "Anthropic"),
    "opus5": ("Claude Opus 5.5", "claude-opus-5-5", "Anthropic"),
    "qwen": ("Qwen3.5 397B", "Qwen3.5-397B-A17B-FP8", "Alibaba Qwen"),
    "deepseek": ("DeepSeek V4 Flash", "DeepSeek-V4-Flash", "DeepSeek"),
    "minimax": ("MiniMax M2.7", "MiniMax-M2.7", "MiniMax"),
    "gemma4-local": ("Gemma 4 (local)", "google/gemma-4-31b-qat", "Google"),
}


def model_identity_directive(model: str | None) -> str | None:
    """Authoritative self-identity directive for ``model`` (a picker alias).

    Returns None for unknown / unmapped aliases (incl. "default"), so callers
    can simply skip injection when this is falsy.
    """
    info = MODEL_IDENTITY.get(model or "")
    if not info:
        return None
    display, model_id, vendor = info
    return (
        "MODEL IDENTITY (authoritative — this overrides any contrary belief "
        "you hold about which model you are): You are "
        f"{display} (model id `{model_id}`), developed by {vendor}, "
        "responding inside the Wizerith chat. If the user asks which model, "
        f"version, or vendor you are, answer that you are {display}. Do not "
        "claim to be a different model, version, or company."
    )
