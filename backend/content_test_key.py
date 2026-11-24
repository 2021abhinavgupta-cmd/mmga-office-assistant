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


def typing_speed(text, seconds_used):
    """(words per minute, accuracy %) for the typing question, or (None, None) when there is nothing to measure."""
    text = (text or "").strip()
    if not text or not seconds_used:
        return None, None
    ratio = difflib.SequenceMatcher(None, TARGET.lower(), text.lower()[: len(TARGET) + 20]).ratio()
    correct = ratio * min(len(TARGET), len(text))
    wpm = (correct / 5.0) / (max(seconds_used, 1) / 60.0)
    return round(wpm), round(ratio * 100)
