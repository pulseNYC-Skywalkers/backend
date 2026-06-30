# Demo Recording Script — Wound-Care Billing Pipeline

**Length:** ~6–8 minutes  
**Audience:** Hackathon judges / clinical billing stakeholders  
**Tone:** Plain English, real-world, no jargon unless you explain it immediately.

---

## Before you hit Record

**Terminal setup** (3 tabs):

| Tab | Command |
|---|---|
| A | *(optional)* `.venv/bin/python -m mock_api.server` — only if demoing offline |
| B | Pipeline commands (below) |
| C | `.venv/bin/python -m dashboard.app` → open **http://127.0.0.1:8050** |

**One-liner to run the full pipeline** (Tab B):

```bash
cd /Users/riddhixraina/pulseNYC-Skywalkers/backend
.venv/bin/python -m orchestration.runner
.venv/bin/python -m pipeline.extract.extractor --force
.venv/bin/python -m pipeline.resolve.resolver
.venv/bin/python -m pipeline.decide.rules
```

---

## OPENING (30 sec)

> "Imagine you're a wound-care biller. Every morning you get clinical notes from the EHR — free-text progress notes, structured assessments, insurance records — and you have to decide: **bill this visit, send it for review, or don't bill at all.**
>
> The problem? The EHR API is down or throttling **70% of the time**. And if you use AI to read the notes, the model might **invent a wound measurement** that isn't in the chart.
>
> We built a pipeline where **neither of those can corrupt a billing decision.** Let me walk you through it."

---

## STAGE 1 — FETCH: "Get the data once, safely" (1 min)

**Show:** Terminal running `orchestration.runner`, or the stats output.

**Say:**

> "Stage one is **Fetch**. We pull patients, diagnoses, coverage, notes, and assessments from the EHR — in our case, the real hackathon API at Pulse Foundry, which throttles about **30% of requests** with a 429.
>
> A normal script would crash or miss patients. Ours **retries with backoff**, honors Retry-After, and never treats throttling as a real outage. Every resource lands in DuckDB as **raw JSON** — a local cache.
>
> Why does that matter? If the API goes down mid-run, we **resume where we left off**. Downtime costs **time**, never **correctness**.
>
> We ran this against the live API: **300 patients**, three facilities, **100% coverage** — even though roughly 500 requests got throttled."

**Optional on screen:**

```bash
.venv/bin/python -m orchestration.runner --stats
```

> "See — 100% complete. Re-run it and it does **zero work**, because everything is already cached."

**Real-world analogy:**

> "It's like a biller photocopying every chart into a folder before the EHR goes offline for maintenance. The billing work happens from the folder, not from a live connection."

---

## STAGE 2 — EXTRACT: "Read the chart, don't invent facts" (1.5 min)

**Show:** Dashboard → pick a patient → expand **Original chart records** with colored highlights.

**Say:**

> "Stage two is **Extract**. Notes come in four different formats — shorthand progress notes, HP prose, Envive narratives, and structured assessment surveys. Some fields are in JSON, some buried in free text.
>
> We use a **tiered extractor**:
> - **Structured first** — parse the assessment survey for free, high confidence.
> - **Regex second** — pull measurements like `3.2 x 2.0 cm` from the note text.
> - **LLM last** — Gemini only when cheaper tiers miss something — and even then, **grounded or null**.
>
> Grounded-or-null means: every fact must point to an **exact character span** in the source. If we can't find it in the chart, we **drop it** — we never bill on something the model made up."

**Click a highlighted measurement in the source panel.**

> "Hover this highlight — length 3.2 centimeters. That number came from **this exact spot** in the note. A biller can verify it in one click."

**Real-world analogy:**

> "It's the difference between a biller highlighting a line in the chart with a yellow marker versus trusting someone's memory of what the note said."

---

## STAGE 3 — RESOLVE: "One patient, one picture" (45 sec)

**Show:** Dashboard Q&A section for one patient (e.g. FA-007).

**Say:**

> "Stage three is **Resolve**. One patient might have an assessment **and** a progress note — sometimes with slightly different numbers. Resolve merges them into **one canonical record**: demographics, coverage, diagnoses, and a **primary wound** — the highest-severity one.
>
> If two sources disagree on depth by more than a threshold, we **flag it** instead of silently picking one. The biller sees the conflict, not a hidden guess."

**Real-world analogy:**

> "A senior biller reading both the nurse's assessment and the physician's note and saying: 'Here's the wound I'm billing today — and heads-up, these two documents don't quite match.'"

---

## STAGE 4 — DECIDE: "Rules decide, not the AI" (1.5 min)

**Show:** `config/rules_config.yaml` briefly, then dashboard filters: **Ready to bill / Needs review / Don't bill**.

**Say:**

> "Stage four is **Decide** — and this is the most important design choice: **the LLM never decides a claim.** A deterministic rule engine does, driven by a YAML file a biller can edit without touching code.
>
> Three outcomes only:
> - **Ready to bill** — Medicare Part B, active coverage, active diagnosis, required wound fields present.
> - **Needs review** — conditional payer like Medicare Part A, missing documentation, or extraction flags.
> - **Don't bill** — bundled insurance like HMO, or no active coverage.
>
> Every decision comes with **plain-English reasons** — not a black-box score."

**Show patient FA-001 (HMO → Don't bill):**

> "FA-001 has HMO insurance. Managed care is bundled — wound care isn't separately billable. The system says **don't bill**, with a clear reason. No biller time wasted."

**Show patient FA-003 (MCA → Needs review):**

> "FA-003 has Medicare Part A — conditionally billable. Documentation looks good: surgical wound, sacrum, 8.9 by 3.9 centimeters. But the **payer needs human confirmation**. That's **needs review**, not auto-bill."

**Real-world analogy:**

> "The AI is the intern who highlights facts in the chart. The rule book — which the biller owns — is what actually says yes or no."

---

## STAGE 5 — DASHBOARD: "The biller's daily worklist" (1.5 min)

**Show:** Full dashboard — white worklist table, Q&A summary, approve button on FA-003.

**Say:**

> "Stage five is the **biller dashboard**. No JSON, no terminal — a worklist a non-technical biller can actually use.
>
> Left side: every patient, filterable by **Ready to bill**, **Needs review**, or **Don't bill**.
>
> Right side: a plain **question-and-answer summary** — who is this patient, what's the wound, is insurance active, why did the system flag this?
>
> For review cases, we show a **documentation checklist** — which required fields are present, which are missing, and checkboxes to mark a field as optional if your policy allows it."

**Click "Approve for billing" on FA-003:**

> "The biller confirms: 'Yes, Medicare Part A is billable for this visit.' One click — patient moves to **Ready to bill**. We didn't re-fetch from the EHR. We didn't re-run the LLM. The override is **auditable**."

**Real-world analogy:**

> "This is the biller's desk: inbox on the left, chart summary on the right, approve or send back — except every number links back to the source."

---

## CLOSING — The three promises (30 sec)

> "Three things we guarantee:
>
> **One** — API downtime only costs time. Raw data is cached; runs are resumable.
>
> **Two** — No hallucinated facts. Grounded or null. Every value ties to a span in the chart.
>
> **Three** — The model advises; the biller-configurable rule engine decides. Same patient, same rules → same outcome. Every time.
>
> That's the pipeline: **Fetch, Extract, Resolve, Decide, Present.** Thanks."

---

## Quick reference — numbers to mention (from real runs)

| Metric | Value |
|---|---|
| Patients | 300 (facilities 101/102/103: 120/90/90) |
| Fetch coverage | 100% (1,203 tasks) |
| Throttle rate | ~30% (429s — handled, not failures) |
| Ready to bill | ~67 |
| Needs review | ~203 |
| Don't bill | ~30 |
| Edge-case tests | 20/20 ingestion |

---

## Demo patient cheat sheet

| Patient | Payer | System decision | Story to tell |
|---|---|---|---|
| **FA-001** | HMO | Don't bill | Bundled insurance — auto-reject |
| **FA-003** | MCA | Needs review | Good documentation, conditional payer → biller approves |
| **FA-007** | MCB | Ready to bill | Clean Medicare Part B case — bill outright |

---

## If something breaks during recording

**Dashboard 500 error?**

```bash
pkill -f "dashboard.app"
pkill -f "from store.dao import Store"
.venv/bin/python -m dashboard.app
```

**DuckDB locked?** Another Python process is holding the file — kill it, restart dashboard.

**LLM summary slow?** Template fallback kicks in after 12 seconds — dashboard still works.
