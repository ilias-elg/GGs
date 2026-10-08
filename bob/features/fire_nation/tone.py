"""
Picks how a line should be delivered aloud from how it is written: capitals
and "!!" are shouted, "..." trails off, a question sounds like one. Rules
only — no AI request, so choosing a tone costs nothing and takes no time.

The tone becomes the plain-language direction Gemini TTS is given in front
of the line. A line with no pattern in it gets Bob's normal delivery.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Tone:
    name: str
    label: str
    direction: str


TONES: dict[str, Tone] = {t.name: t for t in [
    Tone("urgent", "Urgent / commanding",
         "loud, urgent and commanding, like an officer barking an order across a battlefield"),
    Tone("angry", "Angry", "angry and sharp, clipped, with barely contained fury"),
    Tone("excited", "Excited / hyped", "energetic, triumphant and fired up, grinning as he says it"),
    Tone("menacing", "Menacing", "slow, cold and menacing, every word a quiet threat"),
    Tone("sad", "Sad / sombre", "quiet, slow and heavy, genuinely sorrowful"),
    Tone("whisper", "Whisper", "a hushed, conspiratorial near-whisper, as if sharing a secret"),
    Tone("sarcastic", "Sarcastic", "dripping with dry, drawn-out sarcasm, clearly not meaning a word of it"),
    Tone("amused", "Amused", "amused, with a laugh in his voice, as if barely keeping it together"),
    Tone("disbelief", "Disbelief", "incredulous and baffled, as if he cannot believe what he is hearing"),
    Tone("question", "Questioning", "curious and questioning, with a clear rising intonation at the end"),
    Tone("hesitant", "Hesitant", "hesitant and unsure, with small pauses, trailing off"),
    Tone("formal", "Formal announcement", "formal and ceremonial, like a royal herald making a proclamation"),
    Tone("warm", "Warm", "warm, friendly and sincere"),
]}

MIN_SCORE = 3

# (tone, points, pattern). Every match adds its points; the highest total wins.
_WORD_RULES: list[tuple[str, int, re.Pattern]] = [
    (tone, points, re.compile(pattern, re.IGNORECASE))
    for tone, points, pattern in [
        ("urgent", 2, r"\b(now|hurry|quick(ly)?|move( it| out)?|go go|asap|immediately|emergency|alert|incoming"
                      r"|under attack|all (units|hands)|get (in|on|here)|fall back|retreat|charge|attack)\b"),
        ("angry", 3, r"\b(wtf|what the (hell|fuck|heck)|shut up|idiots?|stupid|morons?|furious|pissed|angry|i hate"
                     r"|how dare|sick of|fed up|useless|pathetic)\b"),
        ("excited", 2, r"\b(let'?s go+|lets go+|gg|ggs|we won|victory|congrat\w*|hype\w*|amazing|awesome|insane"
                       r"|woo+|huge|finally|w raid|big w)\b"),
        ("menacing", 3, r"\b(you will (regret|pay|burn|fall)|no mercy|burn (them|it|you)|crush (them|you)"
                        r"|destroy (them|you)|fear (me|us)|your time (is up|has come)|kneel|mark my words"
                        r"|i'?m (coming|watching)|we'?re coming for)\b"),
        ("sad", 3, r"\b(rip|rest in peace|sadly|unfortunately|i'?m sorry|so sorry|we lost|miss (you|him|her|them)"
                   r"|heartbroken|goodbye|farewell|passed away|sad|depress\w*|it'?s over)\b"),
        ("whisper", 3, r"\b(psst+|shh+|secret(ly)?|between (us|you and me)|don'?t tell|keep (it|this) quiet"
                       r"|whisper\w*|quietly|nobody (can|must) know)\b"),
        ("sarcastic", 3, r"(/s\b|\b(yeah,? right|sure,? buddy|oh,? (great|wow|sure|really)|totally|wow,? so"
                         r"|how original|what a surprise|good (job|one),? genius|nice one|as if)\b)"),
        ("amused", 3, r"\b(lol+|lmao+|lmfao+|rofl|haha+|hehe+|xd+|bruh|that'?s hilarious|i'?m dead)\b"),
        ("hesitant", 2, r"\b(u+h+|u+m+|erm+|hmm+|i guess|maybe|not sure|i think so|kinda|sort of|well\W)"),
        ("formal", 3, r"\b(attention|announcement|hear ye|by order of|hereby|it is (my|our) (honou?r|duty)"
                      r"|ladies and gentlemen|citizens of|all rise|let it be known|proclaim\w*)\b"),
        ("warm", 3, r"\b(thank you|thanks (everyone|all|guys)|well done|proud of|good (work|job)|appreciate"
                    r"|welcome (back|home|aboard)|happy birthday|love (you|yall|y'all))\b"),
    ]
]

_EMOJI_RULES: list[tuple[str, int, str]] = [
    ("excited", 3, "🔥🎉🥳🚀💪🏆🙌"),
    ("amused", 3, "😂🤣💀😆😹"),
    ("sad", 3, "😢😭💔😔🥀😞"),
    ("angry", 3, "😡🤬😠💢"),
    ("menacing", 3, "😈👿☠"),
    ("sarcastic", 3, "🙄😒"),
    ("whisper", 3, "🤫"),
    ("warm", 3, "❤🫡🤝😊"),
    ("disbelief", 3, "😳🤨😱"),
]

# "[whisper] meet me at the gate" / "(angry) who did this" — said in that tone,
# with the tag left out of what is spoken.
_TAG = re.compile(r"^\s*[\[(]\s*([a-z /]+?)\s*[\])]\s*[:\-]?\s*", re.IGNORECASE)
_TAG_ALIASES = {
    "shout": "urgent", "shouting": "urgent", "yell": "urgent", "yelling": "urgent", "loud": "urgent",
    "command": "urgent", "order": "urgent", "mad": "angry", "rage": "angry", "hype": "excited",
    "hyped": "excited", "happy": "excited", "threat": "menacing", "evil": "menacing", "dark": "menacing",
    "sombre": "sad", "somber": "sad", "quiet": "whisper", "whispering": "whisper", "secret": "whisper",
    "sarcasm": "sarcastic", "laugh": "amused", "laughing": "amused", "funny": "amused",
    "confused": "disbelief", "shocked": "disbelief", "curious": "question", "nervous": "hesitant",
    "unsure": "hesitant", "announcement": "formal", "herald": "formal", "friendly": "warm", "kind": "warm",
}


def _caps_ratio(text: str) -> float:
    """Share of letters that are capitals, counting only real words (not "OK", "HR", "VC")."""
    words = [w for w in re.findall(r"[A-Za-z]{4,}", text)]
    letters = "".join(words)
    return sum(c.isupper() for c in letters) / len(letters) if len(words) >= 2 else 0.0


def split_tag(text: str) -> tuple[Tone | None, str]:
    """A leading "[tone]" tag, and the text without it."""
    tag = _TAG.match(text)
    if not tag:
        return None, text
    name = tag.group(1).lower()
    tone = TONES.get(_TAG_ALIASES.get(name, name))
    return (tone, text[tag.end():]) if tone else (None, text)


def detect_tone(text: str) -> Tone | None:
    """The tone the writing itself calls for, or None for Bob's normal delivery."""
    scores: dict[str, int] = {}

    def add(tone: str, points: int) -> None:
        scores[tone] = scores.get(tone, 0) + points

    for tone, points, pattern in _WORD_RULES:
        if pattern.search(text):
            add(tone, points)
    for tone, points, emoji in _EMOJI_RULES:
        if any(e in text for e in emoji):
            add(tone, points)

    stripped = text.rstrip(" \t\n*_~)\"'")
    exclamations = text.count("!")
    shouting = _caps_ratio(text) >= 0.7

    if shouting:
        # Capitals are volume: they make an angry line angrier and anything else an order.
        add("angry" if scores.get("angry") else "excited" if scores.get("excited") else "urgent", 4)
    if exclamations >= 2:
        add("excited" if scores.get("excited", 0) > scores.get("urgent", 0) else "urgent", 2)
    elif exclamations == 1:
        for tone in ("urgent", "excited", "angry"):
            if scores.get(tone):
                add(tone, 1)
    if re.search(r"\?!|!\?|\?{2,}", text):
        add("disbelief", 4)
    elif stripped.endswith("?"):
        add("question", 3)
    if re.search(r"(\.{3,}|…)", text):
        # Trailing off reads as whatever mood is already there, or plain hesitation.
        add("sad" if scores.get("sad") else "menacing" if scores.get("menacing") else "hesitant", 2)
    if re.fullmatch(r"\s*\(.+\)\s*", text, re.DOTALL):
        add("whisper", 3)  # a whole line in brackets is an aside
    if re.search(r"~\s*$|\b\w*([aeiou])\1{3,}\w*\b", text) and not shouting:
        add("amused", 1)  # "sureee~", "nooooo"

    best = max(scores.values(), default=0)
    # One mild hint ("now", "maybe") in an otherwise plain sentence is not a mood.
    if best < MIN_SCORE:
        return None
    # Ties go to the first in TONES, which lists the stronger moods first.
    return next(t for t in TONES.values() if scores.get(t.name) == best)


def tone_for(text: str, chosen: str | None = None) -> tuple[Tone | None, str]:
    """
    The tone to use and the text to speak. An explicit choice wins, then a
    "[tone]" tag at the start of the text, then whatever the writing suggests.
    """
    tagged, spoken = split_tag(text)
    if chosen == "neutral":
        return None, spoken
    if chosen in TONES:
        return TONES[chosen], spoken
    if tagged:
        return tagged, spoken
    # "/s" marks sarcasm for the reader; it isn't something to say.
    return detect_tone(text), re.sub(r"\s*/s\b\s*$", "", text, flags=re.IGNORECASE)


def delivery_for(tone: Tone | None) -> str | None:
    """The direction given to the TTS model, or None to use the default delivery."""
    if tone is None:
        return None
    return f"Speak as a man with a low, confident voice. Deliver this line {tone.direction}. Read this line:"
