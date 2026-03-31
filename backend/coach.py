import asyncio
import json
import httpx

# Exact script lines per stage — these override Claude's next_step so the rep
# always sees verbatim script language, not a paraphrase.
STAGE_SCRIPT: dict[str, list[str]] = {
    "intro": [
        "Are you already a Cove customer, or are you looking to get a security system?",
        "I'll be the one to help you with that. Are you currently on the Cove website?",
        "[If on website] Awesome! Where are you in the process right now?",
        "[If not on website] No problem. I'll walk you through the process and help you get set up.",
    ],
    "discovery": [
        "What has you looking into security? Did something happen, did you just move, what's going on?",
        "Have you ever had a security system before?",
        "[If yes] Who did you have it with? Anything you liked about that system you'd like me to try to include in your new one?",
        "Who are we looking to protect — is it just you or is there anyone else living there with you?",
        "[If kids] Are we talking about little kids or teenagers?",
        "[If pets] Are you referring to a cat or a dog? I can relate because...",
        "I understand how important it is to make sure they're safe. I'll make sure we take great care of you.",
    ],
    "collect_info": [
        "I'm just going to get some information from you before we start building out the system. Could you please spell your first and last name?",
        "Thank you. And what's your best phone number?",
        "And an email so I can send all this information over to you at the end of the call?",
        "And before we get ahead of ourselves, I just want to verify that we have coverage. What's the address you're looking to get the security set up at?",
        "Perfect, we actually have great coverage there so we can definitely help you out. Let's dive right in.",
    ],
    "build_system": [
        "How many doors go in and out of your home?",
        "[After customer answers doors] [EXACT NUMBER] doors, we'll get you [EXACT NUMBER] door sensors to get your entry points covered. How many ground-floor windows do you have?",
        "How many ground-floor windows do you have?",
        "[After customer answers windows] [EXACT NUMBER] windows, we'll get you [EXACT NUMBER] window sensors as well, that way all your entry points are covered and monitored. With all those door and window sensors, obviously when the system is armed they'll trigger the alarm, but when the system is unarmed they'll activate the chime feature — that's the feature that says 'front door open' or 'bedroom window open'. Does that make sense?",
        "On top of that, I'm also gonna throw in a free indoor camera for you. It's live, it's HD, it's recording, night vision, two-way audio with a motion sensor — so wherever you are, you have eyes and ears at all times. Does that make sense, [NAME]?",
        "I'm also gonna get you a free hub for the system — it serves as the brain of the system that connects everything. It's cellular connected, so if your power or Wi-Fi goes down, your home is still protected and secure. It comes with 24/7 live monitoring for police, medical, and fire — so no matter what happens, we've got you covered. I'm also gonna get you a free color touchscreen panel — this serves as the user interface for navigation. Does that all make sense?",
        "I'm also gonna get you a free yard sign and window stickers, that way everyone knows you're protected and have security. Everything also comes with smartphone access — you can arm and disarm the system through your phone, pull up the cameras, speak through them, and access the system no matter where you are.",
        "Would you like to add a smoke detector or any additional cameras to the system?",
    ],
    "recap": [
        "So currently what I have here is [EXACT NUMBER] door sensors to get your entry points covered, [EXACT NUMBER] window sensors to get your windows covered, a free indoor camera to monitor the inside of your home, the hub to connect everything and run the system, color touchscreen panel for navigation, a yard sign and window stickers so everyone knows you have security, and smartphone access so you'll have full control from your phone. Personally I think we have your home fully protected, but just to be sure — is there anything else you might want to add?",
        "Is there anything else you were hoping I could add to your system?",
    ],
    "closing": [
        "Let me go ahead and build the system real quick, just give me a minute or two... Alright, it looks like I'll be able to get you a lot of extra discounts.",
        "First, all of our systems come with no contract and some of the best customer service in the industry.",
        "Here's how it works: we don't charge you anything for the installation. Everything is wireless, so we'll send all the equipment out to you and you'll set it up yourself. Because it's all wireless, the entire setup should take you maybe 20 minutes — super easy.",
        "Then I'm gonna get you a bunch of extra discounts here. On the monthly monitoring cost, that'll just be ___ per month. The equipment we have for you would usually cost around $___,  but with all the discounts and promotions we have available today, I'm gonna get your total equipment cost all the way down to just ___. So to get you set up today, all you have to pay is ___ for the equipment, and from then on it'll just be ___ per month.",
        "Does that sound like it will work for you, [NAME]?",
        "Congratulations and welcome to the Cove family! You'll get tracking information as soon as your package ships — that's usually 3 to 7 business days. Once your equipment arrives, you'll find detailed instructions on how to set it up. If you need any help, our customer service team is top notch and we'd love to walk you through it over the phone. Is there anything else I can help you with before we end the call?",
    ],
}

SYSTEM_PROMPT = """You are a real-time sales coach for Cove Smart home security. You have five jobs:

1. LISTEN carefully to EVERYTHING the customer says. Every detail matters.
2. TRACK where the rep is in the sales script and tell them what to do next.
3. DETECT customer objections and give the rep the right rebuttal.
4. NEVER REPEAT A QUESTION. If the rep already asked something — or the customer already answered it — that topic is DONE. Suggesting it again, even reworded, makes the rep sound like they weren't listening.
5. ACCEPT VAGUE ANSWERS AND MOVE ON. If the customer gives a short or vague answer to a discovery question (like "I just decided it was time" or "nothing happened, just wanted to"), that IS their answer. Do NOT push for more detail or rephrase the question. Affirm their answer and move to the next script item. Pushing makes the rep sound like an interrogator.
6. NEVER SKIP SCRIPT STAGES. The rep MUST go through discovery → collect_info → build_system in order.
   - Discovery gathers the emotional context (why they want security, who's in the home, prior system experience). Without this, the build_system stage will feel generic and impersonal.
   - collect_info gathers name, phone, email, address. Without this, we can't set up the account.
   - If the customer volunteers info early (e.g., mentions kids before being asked), mark that question as answered but STILL ask the remaining unanswered questions in that stage before moving on.
   - Only advance to the next stage when ALL questions in the current stage are either asked or already answered.

═══════════════════════════════
CALL SCRIPT STAGES
═══════════════════════════════

STAGE: intro
- Rep greets the customer and determines: existing customer or new?
- Key line: "Are you already a Cove customer, or are you looking to get a security system?"
- If new: "Perfect! I'll be the one to help you with that."

STAGE: discovery
- What has them looking into security? (moved, scare, just decided)
- Have they had a security system before? If yes: who, what did they like?
- Who are we protecting? Kids? Pets? (Build emotional connection)
- Key empathy: "I totally understand how important it is to keep them safe."

STAGE: collect_info
- Full name, best phone number, email
- Address (verify coverage)
- Key line: "Perfect! We have great coverage out there, so let's dive right in."

STAGE: build_system
- Ask how many doors lead to the outside → confirm EXACT number of door sensors ("I'll get you 3 door sensors")
- Ask how many ground-floor windows → confirm EXACT number of window sensors ("That's 4 window sensors")
- CRITICAL: When the customer says a number, USE THAT EXACT NUMBER in your response. "I have 2 doors" → "Perfect, 2 door sensors." NEVER say a different number.
- Chime feature (great for kids — tie to discovery)
- Free indoor camera (HD, night vision, two-way audio, motion sensor)
- 7-inch touchscreen panel with cellular backup
- 24/7 monitoring (police, fire, medical)
- Yard sign & window stickers
- Smartphone access
- Optional: smoke detector, additional cameras
- RULE: Present each item ONCE then move on. Do NOT repeat equipment already covered. Save the full list for recap.

STAGE: recap
- Walk through everything added: "Let me quickly recap everything..."
- Guide customer to add items to cart one by one
- Ask: "Is there anything else you were hoping I could add?"

STAGE: closing
- "Awesome! I'm able to get you a lot of extra discounts."
- No contract, no installation fee, wireless equipment sent to them
- Setup takes ~20 minutes
- State monthly monitoring fee ($32.99)
- State equipment cost and discount applied
- "Does that sound like it will work for you?"
- On yes: "Congratulations and welcome to the Cove family!"

═══════════════════════════════
APPROVED OBJECTION REBUTTALS
═══════════════════════════════

CRITICAL RULES:
- Focus ONLY on the customer's most recent concern. Do NOT combine objections.
- If an objection was already addressed, offer a fresh angle or redirect.
- Discounts CAN be offered on upfront cost. Ask what price they were hoping for.
- Discounts CANNOT be offered on the monthly bill ($32.99 is fixed). Steer to 60-day trial instead.

PRICE / GENERAL EXPENSE:
"I understand where you're coming from; it can definitely seem expensive at first. Just to clarify, are you referring to the upfront cost or the monthly fee?"

UPFRONT COST:
"I hear you, upfront costs can feel like a lot. Keep in mind, this covers everything you need: the equipment, app access, account setup, and more. Let me ask, what price were you hoping for? Maybe we can find a way to meet you halfway and make this work for you."

MONTHLY BILL ($32.99 — NO DISCOUNT):
"I hear you; the monthly monitoring fee is $32.99. Honestly, that's one of the most affordable rates on the market, especially considering all the equipment included in your system. That's basically just about a dollar a day for full protection for your family. It's peace of mind that's hard to put a price on."
→ Steer to 60-day trial if they resist.

NEEDS (not sure they need it):
"No worries! We have a 60-day return policy with a full refund, so you can try it and see if this system is the right fit for your needs."

TECHNICIAN / DIY CONCERNS:
"Most of our customers install the equipment themselves because it's really easy. This is a DIY system with wireless equipment. You can try installing it on your own, but if you need help, we also have a third-party technician service starting at $129."

WANTS TECHNICIAN TO ASSESS FIRST:
"No worries! I've been helping customers like you for quite some time, and I'm here to guide you step by step so we can find the right solution without needing a separate technician."

DOESN'T WANT TO PAY FOR INSTALLATION:
"No worries! Our equipment is really easy to install, it usually takes less than 20 minutes. Plus, our top-notch customer service team will guide you every step of the way if you need help."

DOESN'T WANT MONITORING:
"At Cove, we don't offer self-monitoring because we want to make sure our customers are fully protected 24/7, with police, fire, and medical support included. The monthly monitoring fee is just $32.99, about a dollar a day, for complete peace of mind that keeps your family safe without compromise."

ONLY WANTS CAMERAS:
"I understand where you're coming from. Cameras are great for keeping an eye on your home, but with our full system, including sensors, you get 24/7 protection with police, fire, and medical response, even when you're not watching."

SPOUSE (needs to talk to partner first):
"Sure, I completely understand, I'd want to check with my partner too before making a big decision like this. Just a heads up, the discount ends tonight, and I wouldn't want you to miss out. But don't worry, we have a 60-day return policy with a full refund, so you can try it and see if this system is the right fit for your needs."

NO URGENCY (wants to call back / think about it):
"No worries, you can definitely call us back when the time comes. Just keep in mind, the discount ends tonight, and I wouldn't want you to miss out. Is there anything else you'd like to know to help make your decision?"

═══════════════════════════════
VALUE BUILDING — BUILD SYSTEM STAGE
═══════════════════════════════

When suggesting equipment in build_system, connect each piece directly to what the customer shared in discovery.
Pull from their specific words — their household, their fear, their situation.
Paint a picture — give them a relatable scenario so they can SEE how it helps them.

Examples:
- Customer has teenagers → "What's great is with you having teenagers, these have the chime feature — so even when the system's unarmed and one of your teenagers tries to sneak out or something like that, not that they would, it'll alert you right away. You'll always know who's coming and going. Does that make sense, [NAME]?"
- Customer works away from home → "So imagine you're out of town or at work — you'll always be able to pull up your camera right on your phone and see what's going on no matter where you are. It'll give you that peace of mind. Does that make sense, [NAME]?"
- Customer mentioned a break-in nearby → "Given what happened down the street, I want to make sure every entry point is covered for you — how many doors go in and out of the home?"
- Customer mentioned little kids → "With you having little ones, the chime feature is huge — even when the system's unarmed it says 'front door open' every time a door opens, so if one of your kids were to get outside you'd know right away. Does that make sense, [NAME]?"
- Customer mentioned spouse home alone → "This way your [wife/husband] has eyes and ears on the whole house even when you're not there."
- Customer mentioned pets → "The sensors can be positioned above your [dog/dogs]' reach so they won't set anything off."

RULES:
- Never suggest generic equipment. Always tie it to a specific detail the customer gave you.
- USE THE CUSTOMER'S NAME. Once the rep has the customer's name (from collect_info), weave it into suggestions naturally — especially at the end of sentences and after "Does that make sense." Use it 2-3 times per suggestion, not every sentence. Use [NAME] as a placeholder in next_step and the system will replace it.
- End equipment items with "Does that make sense, [NAME]?" to check in and keep engagement.
- Paint scenarios: "imagine you're at work and..." or "so if one of your teenagers tries to..." — make it real.

═══════════════════════════════
OUTPUT FORMAT
═══════════════════════════════

Always identify the current call stage and suggest a specific next action, even when there's no objection.
When there IS an objection, include rebuttals and transitions too.

VERBIAGE RULES:
- next_step must include the EXACT words the rep should say, pulled directly from the script key lines provided in the user message.
- next_step MUST ALWAYS end with a QUESTION or call-to-action that moves the conversation forward. The rep should never finish speaking and have nothing to hand off to the customer. If the current script step is a statement (like "I totally understand..."), COMBINE it with the next script question so the rep keeps momentum.
- Before the script line in next_step, add a short natural transition phrase that acknowledges what the customer just said. Match the tone to the moment:
  • Customer gave info willingly → "Absolutely! ..." / "Perfect! ..." / "That's great to know! ..."
  • Customer asked a question → "Great question! ..." / "Of course! ..."
  • Customer expressed a concern → "Totally understandable! ..." / "I hear you! ..."
  • Moving to next topic → "Awesome! ..." / "Love that! ..."
- suggestions[].text must quote the APPROVED REBUTTAL verbatim. Do not paraphrase — give the rep the actual words to say.
- transitions must be complete, ready-to-say closing lines the rep can read out loud immediately.

Good transition examples:
- "Does that make sense?"
- "Would you like to go ahead and set this up for the 60-day trial?"
- "Does that sound like something that would work for you?"
- "Should we go ahead and get you protected today?"

Return ONLY valid JSON — no markdown, no explanation.

Example with objection:
{
  "call_stage": "closing",
  "next_step": "I totally hear you, and honestly that's a really smart way to think about it. Let me put it this way — the monthly monitoring is $32.99, that's basically a dollar a day to make sure your two kids and your wife are protected 24/7 with police, fire, and medical response. It's peace of mind that's hard to put a price on.",
  "triggered": true,
  "objection_type": "Monthly Bill",
  "objection_summary": "Customer thinks the monthly fee is too much",
  "suggestions": [
    {"label": "Monthly Fee Rebuttal", "text": "I hear you; the monthly monitoring fee is $32.99. Honestly, that's one of the most affordable rates on the market, especially considering all the equipment included in your system. That's basically just about a dollar a day for full protection for your family. It's peace of mind that's hard to put a price on."}
  ],
  "transitions": [
    "Does that sound reasonable to you?",
    "Would you like to go ahead and try it for the 60-day trial?",
    "Does that make sense?"
  ]
}

Example without objection (note: no opener fluff, direct, ends with question):
{
  "call_stage": "discovery",
  "next_step": "That's good to know, that past experience should help. Anything you liked about that system you'd like me to try to include in your new one?",
  "triggered": false
}

Example for intro (combine acknowledgement + next question — flow into discovery naturally):
{
  "call_stage": "intro",
  "next_step": "Perfect, well I'll be the one to walk you through the process and help you get set up. Have you ever had a security system before?",
  "triggered": false
}

Example for routine info (brief acknowledgement + next question):
{
  "call_stage": "collect_info",
  "next_step": "Got it. And what's the best phone number to reach you at?",
  "triggered": false
}

Example for build_system (confirm what customer said + move to next item):
{
  "call_stage": "build_system",
  "next_step": "Five doors, I'm gonna get you five door sensors that way all those doors are covered for you. And how many ground-floor windows do you have?",
  "triggered": false
}

INTRO & COLLECT_INFO RULE: During intro and collect_info stages, there is NO separate opener bubble shown.
Your next_step IS the only thing the rep sees, so include a brief natural acknowledgement at the start
(e.g., "Perfect,", "Got it,", "Alright,") followed immediately by the next script question — all in one line.
Keep it conversational and fluid, like you're talking to a friend.

BUILD_SYSTEM STYLE: When the customer gives a number (doors, windows), confirm it back naturally:
"[Number] doors, I'm gonna get you [number] door sensors that way all those doors are covered for you."
Then immediately ask the next question in the same breath. Don't stop after confirming — keep momentum.
Tie equipment to what the customer shared in discovery (kids, pets, living situation).

BAD (too much fluff for a simple question):
  "next_step": "I really appreciate you sharing that! That's wonderful. Now, could I get your email?"
GOOD (direct):
  "next_step": "And your email address?"
"""


# ── Topic-based question tracker ──────────────────────────────────────────
# Each topic has: phrases the rep would say to ask it, phrases the customer
# would say that answer it, and phrases to detect in next_step output.
# Once a topic is "done", any next_step containing its output phrases gets stripped.
QUESTION_TOPICS = {
    "existing_customer": {
        "rep_asks": ["already a customer", "already a cove", "existing customer"],
        "customer_answers": ["looking to get", "looking for", "interested in", "i want", "i need", "get a system", "get a security"],
        "output_detect": ["already a customer", "already a cove", "existing customer", "looking to get a security"],
    },
    "had_system_before": {
        "rep_asks": ["had a security system", "had a system before", "ever had a security", "ever had a system"],
        "customer_answers": ["never had", "first time", "no i haven", "i had", "i was with", "i used to have",
                             "we had", "i've had", "had one with", "had adt", "had alder", "had ring",
                             "had simplisafe", "had vivint", "nope", "no this", "no it", "this would be",
                             "this will be", "not yet"],
        "output_detect": ["had a security system", "had a system before", "ever had a security", "ever had a system"],
    },
    "prior_provider": {
        "rep_asks": ["who did you have", "who were you with", "who was your provider", "anything you liked"],
        "customer_answers": ["i had", "i was with", "we had", "i've had", "had adt", "had alder", "had ring", "had simplisafe", "had vivint", "nothing really"],
        "output_detect": ["who did you have", "who were you with", "anything you liked", "previous system"],
    },
    "who_protecting": {
        "rep_asks": ["who are we protecting", "who all are we", "who lives", "anyone else living", "kids or pets", "just you or"],
        "customer_answers": ["my wife", "my husband", "my kids", "my children", "my son", "my daughter", "just me", "my family", "my dogs", "my cat", "live alone", "by myself"],
        "output_detect": ["who are we protecting", "who all are we", "who lives", "kids or pets", "anyone else living"],
    },
    "kids_age": {
        "rep_asks": ["little kids or teenager", "how old are", "ages of your kids"],
        "customer_answers": ["teenager", "little kids", "toddler", "baby", "year old", "years old", "elementary", "middle school", "high school"],
        "output_detect": ["little kids or teenager", "how old are", "ages of your kids"],
    },
    "why_security": {
        "rep_asks": ["what has you looking", "what got you", "why are you looking", "something happen",
                     "did you just move", "what's going on", "what brought you", "what made you",
                     "looking into getting", "looking to get security", "interested in security",
                     "what's the reason", "reason for looking", "why do you want"],
        "customer_answers": ["break in", "broken into", "robbery", "robbed", "just moved", "new house",
                             "new home", "protect my family", "peace of mind", "while i'm working",
                             "while i'm gone", "saw an ad", "neighbor", "keep my family safe",
                             "want to feel safe", "want protection", "want security", "just want something",
                             "something to protect", "keep an eye on", "while im working", "while im gone",
                             "when im at work", "when i'm at work", "just something to", "feel safer",
                             "just realized", "just decided", "it was time", "figured it was time",
                             "thought it was time", "just wanted to", "nothing happened", "nothing going on",
                             "no reason", "no particular reason", "just because", "just want to",
                             "been thinking about", "been meaning to", "finally decided", "time to get",
                             "just bought", "bought a house", "new to the area", "moved here",
                             "moved from", "new place", "just purchased", "first house", "first home",
                             "looking to set up", "set up a security", "set up security"],
        "output_detect": ["what has you looking", "what got you", "why are you looking", "something happen",
                          "what brought you", "what made you", "looking into getting", "looking to get security",
                          "reason for", "why do you want", "interested in security",
                          "what's making you", "what prompted"],
    },
    "full_name": {
        "rep_asks": ["first and last name", "your name", "spell your name", "full name"],
        "customer_answers": ["my name is", "first name is", "last name is"],
        "output_detect": ["first and last name", "your name", "give me your name", "full name"],
    },
    "phone_number": {
        "rep_asks": ["phone number", "best number", "reach you at"],
        "customer_answers": [],  # customer just gives digits
        "output_detect": ["phone number", "best number", "reach you at"],
    },
    "email": {
        "rep_asks": ["email address", "email for you", "good email", "best email"],
        "customer_answers": ["@", "dot com", "gmail", "yahoo", "hotmail", "outlook"],
        "output_detect": ["email address", "email for you", "good email", "best email"],
    },
    "address": {
        "rep_asks": ["address", "where are we setting", "where are you looking to get"],
        "customer_answers": ["drive", "street", "avenue", "road", "lane", "boulevard", "florida", "texas", "california"],
        "output_detect": ["address", "where are we setting", "what's the address"],
    },
    "how_many_doors": {
        "rep_asks": ["how many doors", "doors go in and out"],
        "customer_answers": [],  # customer gives a number
        "output_detect": ["how many doors", "doors go in and out"],
    },
    "how_many_windows": {
        "rep_asks": ["how many windows", "windows on the ground", "ground floor windows"],
        "customer_answers": [],
        "output_detect": ["how many windows", "windows on the ground", "ground floor windows"],
    },
}


class CoachingEngine:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=30)
        self._history: list[dict] = []
        self.customer_name: str = ""  # first name, set when collected
        self._addressed: list[str] = []
        self._rep_questions: list[str] = []  # questions the rep has already asked
        self._customer_facts: list[str] = []  # facts the customer has already shared
        self._equipment_mentioned: list[str] = []  # equipment already covered in build_system
        self._last_opener: str = ""  # the instant opener shown for the current turn
        self._topics_done: set[str] = set()  # topic keys that are done (asked or answered)

    # Keywords that indicate the rep asked a question (transcription rarely has punctuation)
    _Q_KEYWORDS = [
        "how many", "what's your", "what is your", "could you", "do you have",
        "have you ever", "who are we", "who did you", "are you already",
        "what has you", "would you", "can you", "is there", "tell me",
        "give me your", "what's the", "what is the", "who else",
        "does that", "sound good", "work for you", "like to add",
        "what kind", "what type", "where do you", "where are you",
        "when did", "why do you", "why are you", "who is", "who are",
        "are there", "do they", "did you", "have they", "has anyone",
        "what brought", "what made", "anyone else", "anything else",
        "had a system", "had security", "looking into", "looking for",
        "what got you", "what's got", "your name", "your email",
        "your address", "your phone", "your number", "best number",
        "best email", "full name",
    ]

    def add_turn(self, speaker: str, text: str):
        self._history.append({"speaker": speaker, "text": text})
        if len(self._history) > 40:
            self._history = self._history[-40:]
        t = text.lower()
        # Track rep questions and equipment mentions
        if speaker == "rep":
            if any(kw in t for kw in self._Q_KEYWORDS):
                self._rep_questions.append(text.strip())
            for equip, keywords in self._EQUIPMENT_KEYWORDS.items():
                if equip not in self._equipment_mentioned:
                    if any(kw in t for kw in keywords):
                        self._equipment_mentioned.append(equip)
                        print(f"[coach] equipment tracked: {equip}")
            # Mark topics done when rep asks
            for topic, rules in QUESTION_TOPICS.items():
                if topic not in self._topics_done:
                    if any(phrase in t for phrase in rules["rep_asks"]):
                        self._topics_done.add(topic)
                        print(f"[coach] topic DONE (rep asked): {topic}")
        # Track customer facts and mark topics done when customer answers
        if speaker == "customer":
            self._customer_facts.append(text.strip())
            for topic, rules in QUESTION_TOPICS.items():
                if topic not in self._topics_done and rules["customer_answers"]:
                    if any(phrase in t for phrase in rules["customer_answers"]):
                        self._topics_done.add(topic)
                        print(f"[coach] topic DONE (customer answered): {topic}")
            # Try to extract customer's first name
            if not self.customer_name:
                t_lower = text.lower()
                for prefix in ["my name is ", "my first name is ", "first name is ", "i'm ", "this is ", "it's "]:
                    if prefix in t_lower:
                        after = text[t_lower.index(prefix) + len(prefix):].strip()
                        first_word = after.split()[0] if after.split() else ""
                        # Basic validation: capitalize, skip very short or non-alpha
                        if len(first_word) >= 2 and first_word.isalpha():
                            self.customer_name = first_word.capitalize()
                            print(f"[coach] customer name detected: {self.customer_name}")
                        break

    # Equipment keywords to track what's been presented in build_system
    _EQUIPMENT_KEYWORDS = {
        "door sensor": ["door sensor", "door sensors"],
        "window sensor": ["window sensor", "window sensors"],
        "camera": ["camera", "indoor camera"],
        "panel": ["panel", "touchscreen", "touch screen"],
        "monitoring": ["monitoring", "24/7", "police fire"],
        "chime": ["chime", "chime feature"],
        "yard sign": ["yard sign", "window sticker"],
        "smartphone": ["smartphone", "app access", "phone access", "remote"],
        "smoke detector": ["smoke detector", "smoke sensor"],
        "motion sensor": ["motion sensor", "motion detect"],
    }

    def check_repeated_topic(self, next_step: str) -> str | None:
        """Return the topic name if next_step is RE-ASKING a question already done.
        Only blocks if the next_step is actually asking the question (contains a question
        phrase), not just referencing the topic in a statement."""
        t = next_step.lower()
        for topic in self._topics_done:
            rules = QUESTION_TOPICS.get(topic)
            if not rules:
                continue
            for phrase in rules["output_detect"]:
                if phrase in t:
                    # Make sure it's actually a question, not a statement referencing the topic.
                    # Check if the phrase appears near a question mark or question words.
                    idx = t.index(phrase)
                    surrounding = t[max(0, idx - 20):idx + len(phrase) + 30]
                    is_question = any(qw in surrounding for qw in [
                        "?", "what ", "who ", "how ", "have you", "did you", "are you",
                        "tell me", "let me ask", "could you",
                    ])
                    if is_question:
                        return topic
        return None

    def set_opener(self, opener: str):
        """Record the instant opener shown for the current customer turn."""
        self._last_opener = opener

    def track_equipment_from_text(self, text: str):
        """Track equipment mentioned in any text (e.g. Claude's suggested next_step)."""
        t = text.lower()
        for equip, keywords in self._EQUIPMENT_KEYWORDS.items():
            if equip not in self._equipment_mentioned:
                if any(kw in t for kw in keywords):
                    self._equipment_mentioned.append(equip)
                    print(f"[coach] equipment tracked (from suggestion): {equip}")

    def mark_addressed(self, objection_type: str):
        if objection_type and objection_type not in self._addressed:
            self._addressed.append(objection_type)

    async def get_suggestion(self) -> dict:
        recent = self._history[-24:]
        transcript = "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in recent)

        # Build script reference — all stages so Claude can pick the right one
        script_ref_lines = []
        for stage, lines in STAGE_SCRIPT.items():
            script_ref_lines.append(f"STAGE {stage.upper()}:")
            for line in lines:
                script_ref_lines.append(f"  - {line}")
        script_ref = "\n".join(script_ref_lines)

        addressed_note = ""
        if self._addressed:
            addressed_note = (
                f"\n\nObjections already addressed this call: {', '.join(self._addressed)}. "
                "If the customer raises one again, offer a fresh angle. "
                "If they've moved to a new concern, focus only on that."
            )

        # Build explicit blocklist of questions already asked
        blocklist_note = ""
        if self._rep_questions:
            recent_qs = self._rep_questions[-10:]  # last 10 questions
            print(f"[coach] blocklist ({len(recent_qs)} questions): {[q[:40] for q in recent_qs]}")
            blocklist_note = (
                "\n\n⛔ QUESTIONS THE REP HAS ALREADY ASKED (NEVER suggest these again, even reworded):\n"
                + "\n".join(f"  - \"{q}\"" for q in recent_qs)
                + "\nBefore writing next_step, CHECK every question in the blocklist above AND "
                "the full transcript. If the question you are about to suggest — or any close "
                "variant — appears in EITHER list, SKIP it and move to the next unanswered script line."
            )

        # Add done topics to blocklist
        if self._topics_done:
            topics_list = ", ".join(sorted(self._topics_done))
            blocklist_note += (
                f"\n\n⛔ TOPICS ALREADY COVERED (do NOT ask about these): {topics_list}\n"
                "These have been asked or answered. Move to the next unanswered topic."
            )
            print(f"[coach] topics done: {topics_list}")

        # Build equipment blocklist for build_system
        equipment_note = ""
        if self._equipment_mentioned:
            print(f"[coach] equipment already covered: {self._equipment_mentioned}")
            equipment_note = (
                "\n\n⛔ EQUIPMENT ALREADY PRESENTED (do NOT mention these again — move to the next item):\n"
                + "\n".join(f"  - {e}" for e in self._equipment_mentioned)
                + "\nThe rep has already told the customer about the items above. Suggesting them "
                "again sounds repetitive and wastes time. Find the NEXT equipment item the rep "
                "has NOT yet covered and suggest that instead."
            )

        # Pass the instant opener so Claude avoids clashing language
        opener_note = ""
        if self._last_opener:
            opener_note = (
                f"\n\n⚠️ OPENER ALREADY SHOWN: \"{self._last_opener}\"\n"
                "The rep reads this opener, then reads your next_step RIGHT AFTER — as ONE paragraph.\n"
                "Your next_step must START with the substance (question or script line). "
                "NO extra acknowledgement, NO restating what the customer said, NO filler.\n"
                "The opener already handled warmth — your job is the NEXT question or action.\n\n"
                "EXAMPLES of what the combined paragraph should sound like:\n"
                "Opener: \"Keeping the family safe is what it's all about.\"\n"
                "  BAD next_step: \"I totally get it, that's exactly what a lot of parents deal with. So are we talking about little kids or teenagers?\"\n"
                "  GOOD next_step: \"Are we talking about little kids or teenagers?\"\n\n"
                "Opener: \"That makes a lot of sense — being able to keep an eye on things is key.\"\n"
                "  BAD next_step: \"I understand that completely. Let me get some info from you.\"\n"
                "  GOOD next_step: \"Let me get some information from you so I can help you get set up. Could you please give me your first and last name?\"\n\n"
                "Opener: \"Congrats on the new place!\"\n"
                "  BAD next_step: \"That's really exciting, a new home is a great time to think about security. Who all are we protecting?\"\n"
                "  GOOD next_step: \"Who all are we looking to protect — is it just you or is there anyone else living there with you?\""
            )
        else:
            opener_note = (
                "\n\n⚠️ NO OPENER SHOWN — your next_step is the ONLY bubble the rep sees. "
                "Include a brief acknowledgement (like 'Perfect,' or 'Got it,') at the start, "
                "then go straight into the next script question. Keep it in one smooth line."
            )

        user_content = (
            f"Live call transcript (most recent at bottom):\n\n{transcript}"
            f"{addressed_note}"
            f"{blocklist_note}"
            f"{equipment_note}"
            f"{opener_note}\n\n"
            "SCRIPT LINES BY STAGE (exact wording to use):\n"
            f"{script_ref}\n\n"
            "═══ INSTRUCTIONS ═══\n"
            "1. Read transcript. Note all customer facts and which questions are already asked/answered.\n"
            "2. Find the NEXT unanswered script line. Skip anything already covered.\n"
            "3. Write next_step as the exact words the rep should say. Must end with a question.\n"
            "4. For numbers (doors/windows), use the customer's EXACT number.\n"
            "5. In build_system: personalize equipment to discovery facts. Use [NAME]. Paint scenarios.\n"
            "6. Each equipment item ONCE only — check transcript before suggesting.\n"
            "7. For collect_info: be direct (just ask for the info, no fluff).\n"
            "8. Never re-ask a question the rep already asked, even reworded.\n\n"
            "Return ONLY valid JSON with call_stage and next_step."
        )

        raw = ""
        try:
            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_content}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except json.JSONDecodeError as e:
            print(f"[coach] JSON parse error: {e} — raw: {raw[:200]!r}")
            return {"triggered": False}
        except Exception as e:
            import traceback
            print(f"[coach] error: {e}\n{traceback.format_exc()}")
            return {"triggered": False}

    async def evaluate_response(self, objection_type: str, objection_summary: str, rep_response: str) -> dict | None:
        prompt = (
            f"You are evaluating a Cove Smart sales rep's response to a customer objection.\n\n"
            f"Objection type: {objection_type}\n"
            f"Customer said: \"{objection_summary}\"\n"
            f"Rep responded: \"{rep_response}\"\n\n"
            "Score 0-100 across three areas:\n"
            "- Verbiage (35pts): Clear, confident, empathetic. Matches approved rebuttal scripts.\n"
            "- Objection Handling (40pts): Addressed the specific concern. Did not offer a monthly discount.\n"
            "- Closing Attempt (25pts): Moved conversation forward, asked closing question, created urgency.\n\n"
            "Tone inferred from word choice only.\n\n"
            "Return ONLY valid JSON:\n"
            "{\"score\": 78, \"feedback\": \"One specific coaching sentence.\", "
            "\"breakdown\": {\"verbiage\": 28, \"handling\": 32, \"closing\": 18}}"
        )
        try:
            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            print(f"[coach] eval error: {e}")
            return None

    def reset(self):
        self._history = []
        self._addressed = []
        self._rep_questions = []
        self._customer_facts = []
        self._equipment_mentioned = []
        self._last_opener = ""
        self.customer_name = ""
        self._topics_done = set()
