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
    rep_overrides: list[str] | None = None,  # items rep manually checked/unchecked
    user_feedback: str = "",  # post-call feedback from rep
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
        "rep_overrides": rep_overrides or [],
        "user_feedback": user_feedback,
        "turns": history,
        "turn_count": len(history),
    }

    try:
        filepath.write_text(json.dumps(data, indent=2))
        print(f"[transcript] saved {filename} ({len(history)} turns)")
    except Exception as e:
        print(f"[transcript] save error: {e}")
        return None

    # Analysis is triggered after feedback is attached (via /api/feedback)

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

_ANALYSIS_PROMPT = """You are analyzing {count} recent sales coaching session(s) for Cove Smart home security.

Your #1 priority: CHECKLIST ACCURACY. When reps have to manually check or uncheck boxes that the system should have handled automatically, that is the most important signal. Each transcript includes "rep_overrides" — items the rep had to manually correct. This is the strongest indicator of system failures.

Your #2 priority: USER FEEDBACK. Reps can submit feedback after each call. Take their feedback seriously — these are direct requests for improvement.

Here are the transcripts:

{transcripts}

═══════════════════════════════════════════
ANALYZE AND RETURN JSON with these fields:
═══════════════════════════════════════════

{{
  "summary": "2-3 sentence overview of what's working and what needs fixing",

  "checklist_issues": [
    {{
      "issue": "which checklist item was wrong and what the system did vs what it should have done",
      "item_key": "the checklist key (e.g. 'had_system_before', 'door_sensors')",
      "direction": "false_positive (AI checked but shouldn't have) or false_negative (AI missed it)",
      "transcript_evidence": "the relevant quote showing why the check/uncheck was wrong",
      "fix": "specific detection rule change — what phrase or pattern should trigger (or not trigger) this item"
    }}
  ],

  "coaching_issues": [
    {{
      "issue": "short description of what's going wrong",
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

  "user_feedback_actions": [
    "Specific action to take based on user feedback, stated as a direct instruction"
  ],

  "strengths": [
    "Things that are working well — keep doing these"
  ]
}}

RULES:
- Be specific. Reference actual transcript content. Don't be generic.
- Checklist issues are HIGHEST priority — every rep override must be analyzed.
- User feedback is SECOND priority — every piece of feedback must be addressed.
- For single-call analysis, flag everything notable. For multi-call, focus on patterns.
- coaching_additions and roleplay_additions are injected directly into prompts — write them as clear instructions.
- user_feedback_actions should translate vague feedback into concrete system changes.
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

    # Highlight rep overrides — these are the #1 signal
    overrides = t.get("rep_overrides", [])
    if overrides:
        lines.append(f"  *** REP MANUAL OVERRIDES (system was wrong): {', '.join(overrides)} ***")

    # Highlight user feedback — #2 signal
    feedback = t.get("user_feedback", "")
    if feedback:
        lines.append(f"  *** USER FEEDBACK: {feedback} ***")

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
    transcripts = _load_recent(10)  # Always look at last 10 for patterns
    if not transcripts:
        print("[analysis] no transcripts — skipping")
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
        async with httpx.AsyncClient(timeout=120) as http:
            resp = await http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    "x-api-key": api_key,
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 3000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                print(f"[analysis] API error {resp.status_code}: {resp.text[:500]}")
                raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")
            raw = resp.json()["content"][0]["text"].strip()
            print(f"[analysis] raw response ({len(raw)} chars): {raw[:200]}")

        # Parse the JSON response — strip markdown fencing if present
        cleaned = raw
        if cleaned.startswith("```"):
            # Remove first line (```json or ```)
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        notes = json.loads(cleaned)

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

    except json.JSONDecodeError as e:
        print(f"[analysis] JSON parse error: {e}\nRaw: {raw[:500] if 'raw' in dir() else 'N/A'}")
        raise
    except Exception as e:
        import traceback
        print(f"[analysis] error: {e}\n{traceback.format_exc()}")
        raise


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
