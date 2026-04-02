"""
Server-side transcript storage and auto-analysis.

Transcripts are saved as JSON after each session ends.
Every 10 sessions, an analysis runs across recent transcripts
and writes tuning notes that the coaching and roleplay systems read.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

_BACKEND_DIR = Path(__file__).parent
TRANSCRIPTS_DIR = _BACKEND_DIR / "transcripts"
TRANSCRIPTS_DIR.mkdir(exist_ok=True)

TUNING_FILE = _BACKEND_DIR / "tuning_notes.json"
ANALYSIS_INTERVAL = 10  # run analysis every N sessions

# ── Counter ──────────────────────────────────────────────────────────────

_counter_file = TRANSCRIPTS_DIR / "_counter.json"


def _read_counter() -> int:
    try:
        return json.loads(_counter_file.read_text()).get("count", 0)
    except Exception:
        return 0


def _write_counter(n: int):
    _counter_file.write_text(json.dumps({"count": n}))


# ── Save ─────────────────────────────────────────────────────────────────

def save_transcript(
    *,
    mode: str,              # "live" or "roleplay"
    history: list[dict],    # [{"speaker": "rep"|"customer", "text": str}, ...]
    stage_reached: str,
    topics_done: list[str],
    equipment_mentioned: list[str],
    customer_name: str,
    scores: list[int],
    profile: dict,
    scenario: str = "",     # roleplay scenario text
) -> str | None:
    """Save a transcript JSON file. Returns the file path, or None on error."""
    if not history:
        return None

    ts = datetime.now(timezone.utc)
    filename = f"transcript_{ts:%Y%m%d_%H%M%S}_{mode}.json"
    filepath = TRANSCRIPTS_DIR / filename

    data = {
        "timestamp": ts.isoformat(),
        "mode": mode,
        "stage_reached": stage_reached,
        "topics_done": sorted(topics_done),
        "equipment_mentioned": equipment_mentioned,
        "customer_name": customer_name,
        "scores": scores,
        "profile": profile,
        "scenario": scenario,
        "turns": history,
        "turn_count": len(history),
    }

    try:
        filepath.write_text(json.dumps(data, indent=2))
        print(f"[transcript] saved {filename} ({len(history)} turns)")
    except Exception as e:
        print(f"[transcript] save error: {e}")
        return None

    # Bump counter and check if analysis is due
    count = _read_counter() + 1
    _write_counter(count)
    if count % ANALYSIS_INTERVAL == 0:
        print(f"[transcript] {count} sessions — triggering auto-analysis")
        # Fire-and-forget — don't block the session teardown
        asyncio.create_task(_run_analysis_safe())

    return str(filepath)


# ── Load recent transcripts ─────────────────────────────────────────────

def _load_recent(n: int = 10) -> list[dict]:
    """Load the N most recent transcript files."""
    files = sorted(TRANSCRIPTS_DIR.glob("transcript_*.json"), reverse=True)
    results = []
    for f in files[:n]:
        try:
            results.append(json.loads(f.read_text()))
        except Exception:
            continue
    return results


# ── Analysis ─────────────────────────────────────────────────────────────

_ANALYSIS_PROMPT = """You are analyzing {count} recent sales coaching practice sessions for Cove Smart home security.

Your job: identify PATTERNS — things that keep going wrong (or right) across multiple calls.
Focus on actionable coaching and roleplay improvements.

Here are the transcripts:

{transcripts}

═══════════════════════════════════════════
ANALYZE AND RETURN JSON with these fields:
═══════════════════════════════════════════

{{
  "summary": "2-3 sentence overview of patterns across these calls",

  "coaching_issues": [
    {{
      "issue": "short description of what's going wrong",
      "frequency": "how many of the {count} calls had this problem",
      "example": "brief quote or paraphrase from a transcript",
      "fix": "specific coaching adjustment to make"
    }}
  ],

  "roleplay_issues": [
    {{
      "issue": "what the AI customer is doing wrong or could do better",
      "fix": "specific roleplay persona adjustment"
    }}
  ],

  "coaching_additions": [
    "New coaching rule or emphasis to add, stated as a direct instruction"
  ],

  "roleplay_additions": [
    "New roleplay behavior rule, stated as a direct instruction"
  ],

  "strengths": [
    "Things that are working well — keep doing these"
  ]
}}

Be specific. Reference actual transcript content. Don't be generic.
Only flag issues that appear in 2+ calls. Single-occurrence oddities are noise.
Return ONLY valid JSON, no markdown fencing."""


def _format_transcript(t: dict, idx: int) -> str:
    """Format one transcript for the analysis prompt."""
    lines = [f"── Call {idx+1} ({t.get('mode', '?')}, stage reached: {t.get('stage_reached', '?')}) ──"]
    if t.get("scenario"):
        lines.append(f"Scenario: {t['scenario'][:200]}")
    if t.get("customer_name"):
        lines.append(f"Customer name: {t['customer_name']}")
    topics = t.get("topics_done", [])
    if topics:
        lines.append(f"Topics covered: {', '.join(topics)}")
    equip = t.get("equipment_mentioned", [])
    if equip:
        lines.append(f"Equipment mentioned: {', '.join(equip)}")
    scores = t.get("scores", [])
    if scores:
        avg = round(sum(scores) / len(scores))
        lines.append(f"Scores: {scores} (avg {avg})")
    lines.append("")
    for turn in t.get("turns", []):
        speaker = turn.get("speaker", "?").upper()
        text = turn.get("text", "")
        lines.append(f"  {speaker}: {text}")
    return "\n".join(lines)


async def _run_analysis(api_key: str | None = None):
    """Read recent transcripts, call Claude, save tuning notes."""
    transcripts = _load_recent(ANALYSIS_INTERVAL)
    if len(transcripts) < 3:
        print(f"[analysis] only {len(transcripts)} transcripts — skipping (need at least 3)")
        return

    # Get API key from environment if not passed
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[analysis] no API key available — skipping")
        return

    formatted = "\n\n".join(
        _format_transcript(t, i) for i, t in enumerate(transcripts)
    )
    prompt = _ANALYSIS_PROMPT.format(count=len(transcripts), transcripts=formatted)

    print(f"[analysis] analyzing {len(transcripts)} transcripts...")
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    "x-api-key": api_key,
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()

        # Parse the JSON response
        # Strip markdown fencing if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            if raw.endswith("```"):
                raw = raw[:-3]
        notes = json.loads(raw)

        # Save with metadata
        notes["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        notes["transcript_count"] = len(transcripts)

        # Load existing tuning notes and append (keep history)
        existing = []
        if TUNING_FILE.exists():
            try:
                existing = json.loads(TUNING_FILE.read_text())
                if isinstance(existing, dict):
                    existing = [existing]
            except Exception:
                existing = []

        existing.append(notes)
        # Keep last 10 analyses
        existing = existing[-10:]
        TUNING_FILE.write_text(json.dumps(existing, indent=2))

        print(f"[analysis] saved tuning notes: {len(notes.get('coaching_issues', []))} issues, "
              f"{len(notes.get('coaching_additions', []))} additions, "
              f"{len(notes.get('strengths', []))} strengths")

    except Exception as e:
        import traceback
        print(f"[analysis] error: {e}\n{traceback.format_exc()}")


async def _run_analysis_safe():
    """Wrapper that catches all errors so fire-and-forget is safe."""
    try:
        await _run_analysis()
    except Exception as e:
        print(f"[analysis] uncaught error: {e}")


# ── Read tuning notes (for coaching/roleplay to consume) ────────────────

def get_latest_tuning() -> dict | None:
    """Return the most recent analysis notes, or None."""
    if not TUNING_FILE.exists():
        return None
    try:
        data = json.loads(TUNING_FILE.read_text())
        if isinstance(data, list) and data:
            return data[-1]
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None
