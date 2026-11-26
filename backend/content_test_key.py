"""Answer keys and reviewer rubrics for the content writing test.

Kept on the server on purpose: the questions live in frontend/content-test-config.js, which anyone can
read in the browser, so the keys and rubrics must not be there. Used by routes/content_test.py to put
points and "what a 3 looks like" next to each answer in the emailed Excel sheet.
Taken from the MMGA Proof screening test.
"""
import difflib

TARGET = "Vada pav at Ghatkopar station tastes better after a bad day."

# multiple-choice points, by option id (the options are shuffled on screen, the ids are not)
A_KEY = {
    "A1": {"a": 0, "b": 2, "c": 3, "d": 0},
    "A2": {"a": 0, "b": 1, "c": 3, "d": 0},
    "A3": {"a": 1, "b": 3, "c": 0, "d": 0},
    "A4": {"a": 1, "b": 3, "c": 0, "d": 0},
    "A5": {"a": 3, "b": 0, "c": 3, "d": 1},
}
C4_PICK = {"A": 0, "B": 2}      # the reason is scored by a person, 0 or 1

SCALE = "0 missing, generic or blames others. 1 fine, anyone could have written it. 2 specific and theirs. 3 specific and surprising."

RUBRIC = {
    "Q0": "Not scored. Used for typing speed.",
    "P1": "Not scored. Money expectations, for HR.",
    "P2": "Not scored.",
    "A6": "Not scored. Look at what they put first (e.g. 'Being right' first: ask about coachability).",
    "B1": '3: specific, recent, explains why it is funny. 0: "memes", generic.',
    "B2": '3: a real, named ad plus a sharp, opinionated reason. 0: vague, or "all ads are annoying".',
    "B3": "3: funny and self aware, not fishing for compliments. 0: humblebrag.",
    "B4": '3: owns part of the decision, specific, calm about the employer. 0: blame language ("toxic", "they didn\'t value me") with no specifics.',
    "B5": '3: a real flaw plus what they do about it. 0: "I\'m a perfectionist" or "I work too hard".',
    "B6": '3: first person ("I"), concrete actions, told someone early. 0: someone else\'s mistake, or no action.',
    "B7": '3: specific, honest, shows a direction and self awareness. 0: generic ("grow in a reputed organisation").',
    "B8": "3: names something specific MMGA made or stands for and ties it to who they are. 0: could be pasted into any agency application.",
    "B9": "3: sharp or funny, sounds like a person, readable in 3 seconds at 60 km/h. 0: bland, empty or a list of hashtags.",
    "C1": "3: three genuinely different angles, at least one you would stop scrolling for, star on the strongest. 1: rewordings of one idea. 0: corporate lines.",
    "C2": '3: a real tension or insight (e.g. "the fan you never notice until it squeaks at 2 AM") plus a line built on it. 0: feature list.',
    "C4": "Pick B = 2 points (automatic). Score the REASON 0 or 1: 1 if it names specificity, tension or truth.",
    "C5": "3: respectful, confident, offers value beyond money (product, repost, long term deal, story add on), names a clear number. 0: begging or bullying.",
    "C6": '3: sensible split with a reason, a specific offline idea that also creates content (not "put up flyers"), an online idea that fits the audience. 0: generic.',
    "C7": "3: lock scripts, then shot list, then location/talent/props confirmed, then call sheet with timings, then backup plan (order and completeness). 0: vague.",
    "C8": "3: the change shows an eye (light, texture, colour, props) and fits the brief. 0: vague or no change.",
    "C9": '3: ideas fit an agency voice (culture, process, work), references show taste, twist not copy. 0: "5 tips for marketing".',
    "C10": '3: ambitious but defensible number, moves are specific, repeatable and fit an agency voice. 0: "post consistently, use hashtags, collaborate".',
}


def auto_points(qid, fields):
    """Points a question earns without a human, or None when a person has to score it."""
    f = fields or {}
    if qid in A_KEY:
        c = f.get("choice")
        return A_KEY[qid].get(str(c), 0) if c is not None else 0
    if qid == "C4":
        c = f.get("choice")
        return C4_PICK.get(str(c), 0) if c is not None else 0
    return None


B_IDS = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"]
REV = B_IDS + ["C1", "C2", "C4", "C5", "C6", "C7", "C8", "C9", "C10"]     # scored 0-3 by a person or the AI (C4 reason is 0-1)
PASS_MARK = 60


def _words(s):
    return len((s or "").split())


def estimate_scores(answers):
    """Last-resort scores from answer length only (the live site does the same when its AI is down)."""
    out = {}
    for i in REV:
        w = _words((answers or {}).get(i))
        out[i] = {"s": (1 if w > 3 else 0) if i == "C4" else (0 if w < 3 else 1 if w < 12 else 2),
                  "why": "Estimated from length. Reviewer to confirm."}
    return out


def ai_scores(questions, answers):
    """Ask the cheap model to score each reviewer question 0-3 with its rubric. Returns {id:{s,why}} or raises."""
    import json
    import re
    import llm_openrouter as lo
    if not lo.enabled():
        raise RuntimeError("no AI model configured")
    items = [{"id": i, "question": (questions.get(i) or {}).get("label", i), "rubric": RUBRIC.get(i, ""),
              "answer": (answers or {}).get(i) or "(no answer)"} for i in REV]
    prompt = ("You are scoring a hiring screening test for MMGA, a creative and marketing agency in Mumbai, for a "
              "Creative Producer / Campaign Lead role. Be strict and fair. Score each answer 0 to 3 using its rubric and "
              "these anchors: 0 = missing, generic, corporate, or blames others. 1 = fine, but anyone could have written it. "
              "2 = specific, sound, clearly theirs. 3 = specific and surprising, you would remember it tomorrow. "
              "For C4 score only 0 or 1. An empty answer is 0. Typos do not matter.\n\n"
              'Return ONLY a JSON array, no prose: [{"id":"B1","score":2,"why":"max 14 words, plain English"}]\n\n'
              + json.dumps(items, ensure_ascii=False))
    resp = lo.create(system="You score hiring tests. Reply with JSON only.", tools=[],
                     messages=[{"role": "user", "content": prompt}], max_tokens=3000)
    txt = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    arr = json.loads(re.search(r"\[[\s\S]*\]", txt).group(0))
    out = {}
    for r in arr:
        if isinstance(r, dict) and r.get("id") in REV:
            top = 1 if r["id"] == "C4" else 3
            try:
                out[r["id"]] = {"s": max(0, min(top, int(round(float(r.get("score") or 0))))), "why": str(r.get("why") or "")[:140]}
            except (TypeError, ValueError):
                pass
    if not out:
        raise ValueError("no scores in the AI reply")
    return out


def compute(scores, by_id):
    """The screening result, same formulas as the MMGA Proof test (attitude 35%, personality 25%, skill 40%)."""
    S = lambda i: (scores.get(i) or {}).get("s", 0)
    auto = {i: (auto_points(i, (by_id.get(i) or {}).get("fields")) or 0) for i in A_KEY}
    c4auto = auto_points("C4", (by_id.get("C4") or {}).get("fields")) or 0
    att = sum(auto.values()) / 15
    pers = sum(S(i) for i in B_IDS) / (len(B_IDS) * 3)
    c4 = c4auto + S("C4")
    script = (S("C1") + c4) / 6
    ideas = (S("C2") + S("C6") + S("C9") + S("C10")) / 12
    execu = (S("C5") + S("C7") + S("C8")) / 9
    skill = 0.45 * script + 0.35 * ideas + 0.20 * execu
    score = round(100 * (0.35 * att + 0.25 * pers + 0.40 * skill))
    gate = (S("C1") + c4) / 2 < 2
    if att < 0.6:
        verdict, reason, ok = "Do not advance", "Attitude is below 60%. Hard stop, whatever the total.", False
    elif gate:
        verdict, reason, ok = "Do not advance", "Script gate: C1 and C4 average below 2.", False
    elif score >= PASS_MARK:
        verdict, reason, ok = "Advance to Stage 2", "Above the pass mark. Book the live sprint.", True
    elif score >= PASS_MARK - 10 and skill >= 0.75:
        verdict, reason, ok = "Advance to Stage 2", "Borderline total, but skill is strong enough.", True
    else:
        verdict, reason, ok = "Do not advance", f"Below the pass mark of {PASS_MARK}.", False
    return {"score": score, "att": round(att, 3), "pers": round(pers, 3), "script": round(script, 3), "ideas": round(ideas, 3),
            "exec": round(execu, 3), "skill": round(skill, 3), "gate": gate, "verdict": verdict, "reason": reason, "advance": ok,
            "scores": scores}


def score_submission(answers, meta):
    """Full result for one stored test: AI scores when a model is configured, else length estimates. Never raises."""
    qs = (meta or {}).get("questions") or []
    by_id = {q.get("id"): q for q in qs}
    source = "ai"
    try:
        scores = ai_scores(by_id, answers)
    except Exception:
        scores, source = {}, "estimate"
    missing = [i for i in REV if i not in scores]
    if missing:
        est = estimate_scores(answers)
        for i in missing:
            scores[i] = est[i]
        source = "estimate" if source == "estimate" else "ai+estimate"
    res = compute(scores, by_id)
    res["source"] = source
    return res


def fit(result):
    """A short, candidate-facing message about what kind of fit they are, from the pillar scores."""
    r = result or {}
    att, pers = r.get("att", 0), r.get("pers", 0)
    area = {"script": r.get("script", 0), "ideas": r.get("ideas", 0), "exec": r.get("exec", 0)}
    top = max(area, key=area.get)
    if att < 0.6:
        return {"label": "A different working style",
                "text": "Your gut-check answers point to a working style that is not quite what this role needs right now. That is a fit question, not a verdict on your talent."}
    if r.get("advance") and min(area.values()) >= 0.6 and pers >= 0.5:
        return {"label": "The all-rounder",
                "text": "You are strong across script, ideas and execution, and your answers sound like a person we would enjoy working with. A rare mix."}
    base = {
        "script": ("The storyteller", "Your strength is the line and the script: sharp angles, a good ear, and a feel for what makes people stop scrolling."),
        "ideas": ("The ideas engine", "Your strength is thinking: you find the tension in a brief and turn it into campaigns and series that go beyond the obvious."),
        "exec": ("The operator", "Your strength is making it happen: budgets, shoots, negotiations and the order things must be done in. Producers like you keep a campaign alive."),
    }[top]
    extra = " We also liked how you showed up as a person." if pers >= 0.6 else ""
    if not r.get("advance"):
        return {"label": base[0], "text": base[1] + " For this particular role the overall score fell short of what we need right now." + extra}
    return {"label": base[0], "text": base[1] + extra}


def typing_speed(text, seconds_used):
    """(words per minute, accuracy %) for the typing question, or (None, None) when there is nothing to measure."""
    text = (text or "").strip()
    if not text or not seconds_used:
        return None, None
    ratio = difflib.SequenceMatcher(None, TARGET.lower(), text.lower()[: len(TARGET) + 20]).ratio()
    correct = ratio * min(len(TARGET), len(text))
    wpm = (correct / 5.0) / (max(seconds_used, 1) / 60.0)
    return round(wpm), round(ratio * 100)
