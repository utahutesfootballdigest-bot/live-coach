import asyncio
import json
import httpx

from transcript_store import get_latest_tuning

# Exact script lines per stage — these override Claude's next_step so the rep
# always sees verbatim script language, not a paraphrase.
STAGE_SCRIPT: dict[str, list[str]] = {
    "intro": [
        "Hi, this is [AGENT NAME] with Cove Security on a recorded line. How are you doing today?",
        "I'm excited to help you out. Are you already a Cove customer, or are you looking to get a security system?",
        "[If existing customer] Okay, great. Let me get you to the right place, just hold on for me. (Transfer/End)",
        "[If new customer] Perfect! I'll be the one to help you with that. Are you currently on the Cove website?",
        "[If on website] Awesome! Where are you in the process right now?",
        "[If at payment stage] Proceed with payment script.",
        "[If building system] Proceed with initial discovery.",
        "[If not on website] No problem. (Then continue with the script.)",
    ],
    "discovery": [
        "What has you looking into security? Did something happen, did you just move, or is there something else going on?",
        "I'm glad you decided to get a security system; it's a smart move. I'm here to make sure you get the protection and peace of mind you deserve.",
        "Let me ask you, have you ever had a security system before?",
        "[If yes] Okay, that's perfect. That'll save us some time building everything out. Who did you have for security before?",
        "[If yes] Is there anything you liked about the system you used to have that you'd like me to try and include in the new system for you?",
        "[If no] No problem, I'll walk you through the process, help you get set up, and I'm going to take great care of you.",
        "Who are we looking to protect? Is it just you, or is there anyone else living there with you?",
        "I understand how important it is for you to make sure they're safe.",
        "[If children] Are we talking about little kids or teenagers? I can relate to that because... (open ended)",
        "[If pets] Are you referring to a cat or a dog? I can relate because... (open ended)",
        "Like I said, I totally understand how important it is for you to make sure they're safe. I'll make sure we take great care of you all.",
    ],
    "collect_info": [
        "Alright! I'm just going to get some information from you before we start building out your system. Could you please spell your first and last name for me?",
        "And the best phone number for you?",
        "And an email so I can send all this information over to you at the end of the call?",
        "And before we get ahead of ourselves, I just want to verify that we have coverage. What is the address you're looking to get the security set up at?",
        "Perfect! We have great coverage out there, so let's dive right in.",
    ],
    "build_system": [
        "How many doors are there that go in and out of the house?",
        "[After customer answers doors] I'm going to give you [EXACT NUMBER] door sensors, in that way all the doors are covered for you.",
        "How many windows are on the ground floor of the house?",
        "[After customer answers windows] I'm going to give you [EXACT NUMBER] window sensors, in that way all the windows downstairs are also covered for you.",
        "With all those door and window sensors, obviously, when the system is armed, they'll trigger the alarm. But when the system is unarmed, they'll activate the chime feature. This is the feature that says, 'Front door open' or 'Bedroom window open'. That way, if your kids ever get out without you knowing, you'll be alerted right away and can bring them back inside. Crisis averted. Does that make sense?",
        "I'm also going to give you a free indoor camera. It's live with HD recording, night vision, two-way audio, and a motion sensor. So, wherever you are, you'll always have eyes and ears in your home.",
        "[If kids] On top of that, this is a mobile camera. This means that if you're in the living room watching a movie with your wife and the kids want to play in another room, you can move the camera there and still keep an eye on them. Do you think one is enough, or would you like to add another?",
        "[If pets] That way, even if you're at work, you can easily check your pet from your phone and even say hi if you'd like.",
        "I'm also getting you a 7-inch colored touchscreen panel to connect everything and run the system. With cellular connections, even if there's no power or Wi-Fi, you're still protected. With 24/7 live monitoring, including police, medical, and fire support, you're covered no matter what. Does that make sense?",
        "I'm also getting you a yard sign and window stickers. This way, everyone knows that you have security in place.",
        "With smartphone access, you can arm and disarm your system, view the camera, speak through it, and access your system from your phone, no matter where you are.",
        "I'm also going to get you a smoke detector. I know you may already have a regular one in place, which only makes a noise when there's a fire, but what I'm getting you is a fully monitored smoke detector connected to your system. Unlike a standard smoke alarm, our monitored detectors immediately alert both you and the fire department, ensuring that help will arrive quickly.",
        "[If pets] This way, if there's ever a fire in your home while you're at work, you'll know we're sending the fire department right away. Your pet will have the highest chance of getting out safely, and you'll always have peace of mind. Fair enough?",
        "We also have a doorbell camera and outdoor cameras. Would you like to add any of those?",
        "Would you also like a key fob? It's like a remote where you can arm and disarm the system without going to the panel.",
    ],
    "recap": [
        "[If on website] Great! Let me quickly recap everything to make sure we've got exactly what you need. Since you're already on the Cove website, please add these items to your cart.",
        "[If not on website] Alright, let me quickly recap everything to make sure we've got exactly what you're looking for. Could you please open the Cove website and add these items to your cart?",
        "I'll be getting you [EXACT NUMBER] door sensors to cover all doors. Could you please add [EXACT NUMBER] door sensors to your cart and let me know once you're done?",
        "I'll be getting you [EXACT NUMBER] window sensors to cover all windows downstairs. Could you please add [EXACT NUMBER] window sensors to your cart and let me know once you're done?",
        "[For each additional item] Could you please add [ITEM] to your cart and let me know once you're done?",
        "Could you also add the free indoor camera? That way, wherever you are, you'll always have eyes and ears in your home.",
        "I also include a 7-inch color touchscreen panel to connect and control everything. A yard sign and window stickers to show everyone your home is protected. With smartphone access, you can control your system from your phone, anywhere.",
        "Personally, I believe we've got you fully protected but is there anything else you were hoping I could add for you?",
    ],
    "closing": [
        "Awesome! It looks like I'll be able to get you a lot of extra discounts.",
        "First, all of our systems come with no contract and one of the best customer services in the industry.",
        "Here's how it works: We don't charge anything for installation. Everything is wireless, so we'll send the equipment straight to you, and you'll set it up yourself. Because it's all wireless, the entire setup should only take about 20 minutes — super easy.",
        "Then, I'm going to get you a bunch of extra discounts here. On the monthly monitoring cost, that'll just be ___ per month. The equipment we have for you would usually cost $600, but I'm going to get your total equipment cost today all the way down to just ___. So, to get you set up today, all you'll have to do is pay the ___ for the equipment, and from then on, it'll just be ___ per month.",
        "Does that sound like it will work for you?",
        "Congratulations and welcome to the Cove family! You'll get tracking information as soon as your package ships, so you'll know exactly when it's on the way. Once your equipment arrives, you'll find simple, step-by-step setup instructions inside. If you need help, our friendly customer service team is just a call away. Be sure to install your system as soon as it arrives so you can get your alarm certificate quickly — that certificate might even help you qualify for a discount on your home insurance. Enjoy your system, and remember, we're always here if you need anything.",
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
- Rep greets: "Hi, this is [Agent Name] with Cove Security on a recorded line. How are you doing today?"
- "I'm excited to help you out. Are you already a Cove customer, or are you looking to get a security system?"
- If EXISTING customer: "Okay, great. Let me get you to the right place, just hold on for me." (Transfer/End)
- If NEW customer: "Perfect! I'll be the one to help you with that. Are you currently on the Cove website?"
- If on website: "Awesome! Where are you in the process right now?" (Route to payment or discovery)
- If not on website: "No problem." (Continue with script)

STAGE: discovery
- "What has you looking into security? Did something happen, did you just move, or is there something else going on?"
- Affirm: "I'm glad you decided to get a security system; it's a smart move. I'm here to make sure you get the protection and peace of mind you deserve."
- "Let me ask you, have you ever had a security system before?"
  - If YES: "Okay, that's perfect. That'll save us some time building everything out. Who did you have for security before?" → "Is there anything you liked about the system you used to have that you'd like me to try and include in the new system for you?"
  - If NO: "No problem, I'll walk you through the process, help you get set up, and I'm going to take great care of you."
- "Who are we looking to protect? Is it just you, or is there anyone else living there with you?"
- "I understand how important it is for you to make sure they're safe."
  - If children: "Are we talking about little kids or teenagers? I can relate to that because..." (open ended, build rapport)
  - If pets: "Are you referring to a cat or a dog? I can relate because..." (open ended, build rapport)
- "Like I said, I totally understand how important it is for you to make sure they're safe. I'll make sure we take great care of you all."

STAGE: collect_info
- "Alright! I'm just going to get some information from you before we start building out your system."
- "Could you please spell your first and last name for me?"
- "And the best phone number for you?"
- "And an email so I can send all this information over to you at the end of the call?"
- "And before we get ahead of ourselves, I just want to verify that we have coverage. What is the address you're looking to get the security set up at?"
- "Perfect! We have great coverage out there, so let's dive right in."

STAGE: build_system
- Doors: "How many doors are there that go in and out of the house?" → "I'm going to give you [NUMBER] door sensors, in that way all the doors are covered for you."
- Windows: "How many windows are on the ground floor of the house?" → "I'm going to give you [NUMBER] window sensors, in that way all the windows downstairs are also covered for you."
- CRITICAL: When the customer says a number, USE THAT EXACT NUMBER. "I have 2 doors" → "I'm going to give you 2 door sensors." NEVER say a different number.
- Chime feature: "With all those door and window sensors, when the system is armed, they'll trigger the alarm. But when the system is unarmed, they'll activate the chime feature — 'Front door open' or 'Bedroom window open'. That way, if your kids ever get out without you knowing, you'll be alerted right away. Crisis averted. Does that make sense?"
- Free indoor camera: "I'm also going to give you a free indoor camera. It's live with HD recording, night vision, two-way audio, and a motion sensor. So, wherever you are, you'll always have eyes and ears in your home."
  - If kids: Pitch the mobile camera angle — move it room to room to watch kids
  - If pets: "Even if you're at work, you can easily check your pet from your phone and even say hi"
- Touchscreen panel: "I'm also getting you a 7-inch colored touchscreen panel to connect everything and run the system. With cellular connections, even if there's no power or Wi-Fi, you're still protected. With 24/7 live monitoring, including police, medical, and fire support, you're covered no matter what. Does that make sense?"
- Yard sign & stickers: "I'm also getting you a yard sign and window stickers. This way, everyone knows you have security in place."
- Smartphone access: "With smartphone access, you can arm and disarm your system, view the camera, speak through it, and access your system from your phone, no matter where you are."
- Smoke detector (optional): "I'm also going to get you a smoke detector... a fully monitored smoke detector connected to your system. Unlike a standard smoke alarm, our monitored detectors immediately alert both you and the fire department."
  - If pets: "If there's ever a fire while you're at work, we're sending the fire department right away. Your pet will have the highest chance of getting out safely."
- Optional cameras: doorbell camera, outdoor cameras
- Optional key fob: arm/disarm without going to the panel
- RULE: Present each item ONCE then move on. Do NOT repeat equipment already covered. Save the full list for recap.

STAGE: recap
- If on website: "Great! Let me quickly recap everything to make sure we've got exactly what you need. Since you're already on the Cove website, please add these items to your cart."
- If not on website: "Alright, let me quickly recap everything. Could you please open the Cove website and add these items to your cart?"
- Walk through each item one by one, asking customer to add to cart and confirm after each
- "Personally, I believe we've got you fully protected but is there anything else you were hoping I could add for you?"

STAGE: closing
- "Awesome! It looks like I'll be able to get you a lot of extra discounts."
- No contract, one of the best customer services in the industry
- No installation charge — everything is wireless, sent to customer, ~20 minutes setup
- Monthly monitoring cost: ___ per month
- Equipment usually $600 but discounted to ___
- "Does that sound like it will work for you?"
- On yes: "Congratulations and welcome to the Cove family!" + onboarding details (tracking, setup instructions, alarm certificate for insurance discount)

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

SHOPPING AROUND / COMPARING:
"I totally understand — it's smart to compare. Just so you know, a lot of people compare us with the big names and end up choosing Cove because of the no-contract, the pricing, and the customer service. And with the 60-day trial, you can try it risk-free. If it's not the right fit, you send it back for a full refund."

PAYMENT METHOD DECLINED:
"Oh no problem — we accept any standard Visa, Mastercard, or Discover credit or debit card. Unfortunately we can't accept prepaid cards, Cash App cards, or PayPal. Do you have another card you could use?"

EXISTING SYSTEM / TAKEOVER:
"Great news — you can actually keep using the equipment that's already installed there. What we'll do is set you up with a new account, get you a new hub and panel, and then we'll reset the existing cameras and sensors to connect to your new account. The ones already there can still work — we just register everything under your information."

AUTOPAY / BILLING DATE CONCERNS:
"The autopay runs on the 5th of each month by default, but if you need a different billing date, you can call our customer service team after setup and they'll adjust it for you. Today you just pay for the equipment, and the monthly monitoring starts after your first month."

DOESN'T WANT TO GIVE PERSONAL INFO:
"I totally understand being careful with your information. How about this — let me walk you through the equipment and pricing first so you can see if it's the right fit, and then we can get your information once you're ready to move forward."

═══════════════════════════════
VALUE BUILDING — BUILD SYSTEM STAGE
═══════════════════════════════

When suggesting equipment in build_system, connect each piece directly to what the customer shared in discovery.
Pull from their specific words — their household, their fear, their situation.
Paint a picture — give them a relatable scenario so they can SEE how it helps them.

Examples:
- Customer has TEENAGERS (they specifically said "teenagers") → "With all those door and window sensors, when the system is armed, they'll trigger the alarm. But when the system is unarmed, they'll activate the chime feature — 'Front door open' or 'Bedroom window open'. That way, if one of your teenagers ever tries to sneak out, not that they would, you'll be alerted right away. Crisis averted. Does that make sense, [NAME]?"
- Customer has LITTLE KIDS (they specifically said "little kids", "toddler", etc.) → "With you having little ones, the chime feature is huge — when the system is unarmed it says 'Front door open' every time a door opens. That way, if your kids ever get out without you knowing, you'll be alerted right away and can bring them back inside. Crisis averted. Does that make sense, [NAME]?"
- Customer mentioned KIDS but didn't specify age → Use generic "kids" language: "With all those door and window sensors, when the system is unarmed, they'll activate the chime feature — 'Front door open' or 'Bedroom window open'. That way, if your kids ever get out without you knowing, you'll be alerted right away. Crisis averted. Does that make sense, [NAME]?" Do NOT mention teenagers or sneaking out unless they specifically said they have teenagers.
- Customer has KIDS + indoor camera → "On top of that, this is a mobile camera. This means that if you're in the living room watching a movie and the kids want to play in another room, you can move the camera there and still keep an eye on them. Do you think one is enough, or would you like to add another?"
- Customer works away from home → "So wherever you are, you'll always have eyes and ears in your home. You can pull up the camera right on your phone and see what's going on no matter where you are. Does that make sense, [NAME]?"
- Customer mentioned a break-in nearby → "Given what happened, I want to make sure every entry point is covered for you — how many doors are there that go in and out of the house?"
- Customer mentioned spouse home alone → "This way your [wife/husband] has eyes and ears on the whole house even when you're not there."
- Customer mentioned pets + camera → "That way, even if you're at work, you can easily check your pet from your phone and even say hi if you'd like."
- Customer mentioned pets + smoke detector → "This way, if there's ever a fire in your home while you're at work, you'll know we're sending the fire department right away. Your pet will have the highest chance of getting out safely, and you'll always have peace of mind. Fair enough?"

RULES:
- Never suggest generic equipment. Always tie it to a specific detail the customer gave you.
- MATCH THE CUSTOMER'S WORDS. If they said "kids", say "kids". Only say "teenagers" if THEY said "teenagers". Only say "little ones" if THEY said "little kids". Never assume or upgrade what they told you.
- USE THE CUSTOMER'S NAME. Once the rep has the customer's name (from collect_info), weave it into suggestions naturally — especially at the end of sentences and after "Does that make sense." Use it 2-3 times per suggestion, not every sentence. Use [NAME] as a placeholder in next_step and the system will replace it.
- End equipment items with "Does that make sense, [NAME]?" to check in and keep engagement.
- Paint scenarios: "imagine you're at work and..." — make it real. But only reference specific household details (teens, little kids, pets, etc.) if the customer actually mentioned them.

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
  "next_step": "Perfect! I'll be the one to help you with that. Are you currently on the Cove website?",
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
  "next_step": "I'm going to give you five door sensors, in that way all the doors are covered for you. How many windows are on the ground floor of the house?",
  "triggered": false
}

INTRO RULE: During the intro stage, there is NO separate opener bubble shown.
Your next_step IS the only thing the rep sees, so include a brief natural acknowledgement at the start
(e.g., "Perfect,", "Got it,", "Alright,") followed immediately by the next script question — all in one line.

COLLECT_INFO RULE: During collect_info, a "SAY FIRST" opener bubble is shown above your next_step (e.g., "Got it, thank you." or "Perfect, I have your number.").
Your next_step should flow directly from that opener — NO acknowledgement, just the next question.
Example: Opener: "Got it, I have your number." → next_step: "And your email so I can send all this information over to you?"

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
        "rep_asks": ["already a customer", "already a cove", "existing customer",
                     "new customer", "looking to get a security"],
        "customer_answers": ["looking to get", "looking for", "interested in", "i want", "i need",
                             "get a system", "get a security", "i'm new", "i am new", "new customer",
                             "not a customer", "no i'm not", "looking into", "want to get",
                             "want to set up", "want a system", "shopping for", "trying to get",
                             "need a system", "need security", "looking at getting",
                             "yes i am", "yeah i am", "i'm already", "i already have",
                             "current customer", "existing customer", "have an account"],
        "output_detect": ["already a customer", "already a cove", "existing customer", "looking to get a security"],
    },
    "had_system_before": {
        "rep_asks": ["had a security system", "had a system before", "ever had a security", "ever had a system",
                     "ever had security", "had security before", "had a security before",
                     "security system before", "system before", "let me ask you"],
        "customer_answers": ["never had", "first time", "no i haven", "i had", "i was with", "i used to have",
                             "we had", "i've had", "had one with", "had adt", "had alder", "had ring",
                             "had simplisafe", "had vivint", "this would be",
                             "this will be", "no i have not", "no i haven't", "this is my first",
                             "had a system", "had security", "had one before",
                             "no never", "never before", "not before", "no this is",
                             "this is the first", "don't have one", "never owned"],
        # NOTE: Generic short answers ("not yet", "nope", "no sir") removed —
        # they were triggering from existing_customer context. These are
        # handled by the short-answer heuristic which checks recent rep speech.
        "output_detect": ["had a security system", "had a system before", "ever had a security", "ever had a system"],
    },
    "prior_provider": {
        "rep_asks": ["who did you have", "who were you with", "who was your provider", "anything you liked"],
        "customer_answers": ["i had", "i was with", "we had", "i've had",
                             "had adt", "had alder", "had ring", "had simplisafe", "had vivint",
                             "from adt", "from alder", "from ring", "from simplisafe", "from vivint",
                             "with adt", "with alder", "with ring", "with simplisafe", "with vivint",
                             "have adt", "have alder", "have ring", "have simplisafe", "have vivint",
                             "got adt", "got alder", "got ring", "got simplisafe", "got vivint",
                             "nothing really", "nothing special", "it was okay", "it was fine",
                             "liked the cameras", "liked the app", "didn't like"],
        "output_detect": ["who did you have", "who were you with", "anything you liked", "previous system"],
    },
    "who_protecting": {
        "rep_asks": ["who are we protecting", "who all are we", "who lives", "anyone else living",
                     "kids or pets", "just you or", "who are we looking to protect",
                     "who else is", "who's in the home", "who's in the house",
                     "anybody else", "anyone else in"],
        "customer_answers": ["my wife", "my husband", "my kids", "my children", "my son", "my daughter",
                             "just me", "my family", "my dogs", "my cat", "live alone", "by myself",
                             "me and my", "and the kids", "and my kids", "and my wife", "and my husband",
                             "two kids", "three kids", "the wife", "the kids", "the family",
                             "couple dogs", "couple cats", "a dog", "a cat", "two dogs", "my dog",
                             "my girlfriend", "my boyfriend", "my fiance", "my fiancee",
                             "my roommate", "my mom", "my mother", "my dad", "my father",
                             "my parents", "my grandmother", "my grandfather", "my grandma", "my grandpa",
                             "we have kids", "got kids", "have kids", "have a dog", "have dogs",
                             "have a cat", "have cats", "have pets", "got dogs", "got a dog",
                             "there's four of us", "there's three of us", "there's two of us",
                             "four of us", "three of us", "two of us", "the whole family",
                             "entire family", "wife and kids", "husband and kids",
                             "i live with", "it's just me", "only me", "nobody else",
                             "i'm alone", "no one else", "no kids", "no pets"],
        "output_detect": ["who are we protecting", "who all are we", "who lives", "kids or pets",
                          "anyone else living", "who are we looking to protect",
                          "who else is", "who's in the home", "anybody else"],
    },
    "kids_age": {
        "rep_asks": ["little kids or teenager", "how old are", "ages of your kids"],
        "customer_answers": ["teenager", "little kids", "toddler", "baby", "year old", "years old",
                             "elementary", "middle school", "high school",
                             "little ones", "young kids", "small kids", "grown", "adult",
                             "in school", "preschool", "kindergarten"],
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
                             "looking to set up", "set up a security", "set up security",
                             "safety", "for safety", "for protection", "want cameras",
                             "want to protect", "want to keep", "keep safe", "stay safe",
                             "feel safe", "protect the house", "protect the home", "protect my home",
                             "want to be safe", "need protection", "need to protect",
                             "saw on tv", "saw online", "saw your ad", "saw the commercial",
                             "scared", "nervous", "worried", "crime", "theft", "stolen",
                             "burglar", "someone broke", "car was broken", "package stolen",
                             "travel a lot", "work a lot", "gone a lot", "not home a lot",
                             "away a lot", "work nights", "night shift",
                             "moving in", "closing on", "buying a home", "new apartment",
                             "just because i want", "always wanted", "been wanting",
                             "never had one", "about time", "it's about time"],
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
        "rep_asks": ["email address", "email for you", "good email", "best email", "your email", "and your email"],
        "customer_answers": ["@", "dot com", "gmail", "yahoo", "hotmail", "outlook"],
        "output_detect": ["email address", "email for you", "good email", "best email", "your email", "and your email"],
    },
    "address": {
        "rep_asks": ["what's the address", "what is the address", "what address", "your address",
                     "the address you", "home address", "shipping address", "verify the address",
                     "where are we setting", "where are you looking to get",
                     "where is the home", "where is your home"],
        "customer_answers": ["drive", "street", "avenue", "road", "lane", "boulevard", "florida", "texas", "california"],
        "output_detect": ["what's the address", "what is the address", "your address",
                          "where are we setting", "the address you"],
    },
    "how_many_doors": {
        "rep_asks": ["how many doors", "doors go in and out", "doors are there that go"],
        "customer_answers": [],  # customer gives a number
        "output_detect": ["how many doors", "doors go in and out"],
    },
    "how_many_windows": {
        "rep_asks": ["how many windows", "windows on the ground", "ground floor windows", "windows are on the ground"],
        "customer_answers": [],
        "output_detect": ["how many windows", "windows on the ground", "ground floor windows"],
    },
    "on_website": {
        "rep_asks": ["on the website", "on the cove website", "on covesmart", "pull up the website",
                     "are you on the site", "on the site right now"],
        "customer_answers": ["yes i'm on", "yeah i'm on", "i'm on the website", "i'm looking at it",
                             "i have it up", "i have it pulled up", "not yet", "not on the website",
                             "no i'm not", "i can pull it up", "i'll pull it up", "let me pull it up"],
        "output_detect": ["on the website", "on the cove website", "on covesmart", "pull up the website"],
    },
}


# ── Keyword-based objection fallback ─────────────────────────────────────
# When Claude fails to return triggered=true, this lookup provides the
# approved rebuttal so the rep ALWAYS sees objection coaching.
OBJECTION_REBUTTALS = {
    "price_general": {
        "signals": [
            # Textbook
            "too expensive", "too much money", "that's a lot", "can't afford",
            "costs a lot", "pricey", "cost too much", "lot of money",
            "that's expensive", "kind of expensive", "kinda expensive",
            "pretty expensive", "really expensive", "so expensive",
            # Conversational / confused
            "just want the price", "just wanted the price", "just wanted to know the price",
            "only wanted the price", "just want to know how much",
            "what's the total", "what is the total", "how much is it",
            "how much does it cost", "how much is everything", "how much total",
            "what's the price", "what is the price", "what's the cost",
            "what am i paying", "what do i pay", "what would i pay",
            "just give me the price", "it doesn't matter i just",
            "skip to the price", "get to the price",
            # Sticker shock after hearing price
            "why do i have to pay", "why am i paying", "why is it",
            "i thought it was free", "thought the equipment was free",
            "are for free", "said it was free", "was supposed to be free",
            "that's more than", "more than i expected", "wasn't expecting",
            "didn't expect to pay", "didn't think i'd have to pay",
        ],
        "type": "Price / General Expense",
        "summary": "Customer is concerned about the price",
        "suggestions": [{"label": "Price Rebuttal", "text": "I understand where you're coming from; it can definitely seem expensive at first. Just to clarify, are you referring to the upfront cost or the monthly fee?"}],
        "transitions": ["Does that make sense?", "Would you like to try it for the 60-day trial?"],
    },
    "upfront_cost": {
        "signals": [
            "upfront cost", "up front cost", "equipment cost", "pay that much today",
            "that much upfront", "that much up front", "down payment",
            # Conversational
            "pay for the equipment", "pay for equipment", "equipment is how much",
            "why do i have to pay for the equipment", "that much for equipment",
            "hundred dollars", "ninety nine dollars", "two hundred",
            "three hundred", "pay all that", "pay that today",
            "that's a lot for equipment", "a lot just for equipment",
        ],
        "type": "Upfront Cost",
        "summary": "Customer thinks the upfront equipment cost is too high",
        "suggestions": [{"label": "Upfront Cost Rebuttal", "text": "I hear you, upfront costs can feel like a lot. Keep in mind, this covers everything you need: the equipment, app access, account setup, and more. Let me ask, what price were you hoping for? Maybe we can find a way to meet you halfway and make this work for you."}],
        "transitions": ["What price were you hoping for?", "Would the 60-day trial help?"],
    },
    "monthly_bill": {
        "signals": [
            "monthly fee is", "per month is too", "a month is too", "monthly cost",
            "monthly is too", "monthly is a lot", "thirty three a month",
            "thirty two a month",
            # Conversational
            "a month for monitoring", "month just for monitoring",
            "every month on top", "monthly on top of", "plus a monthly",
            "and then monthly", "monthly too", "pay every month too",
        ],
        "type": "Monthly Bill",
        "summary": "Customer thinks the monthly monitoring fee is too much",
        "suggestions": [{"label": "Monthly Fee Rebuttal", "text": "I hear you; the monthly monitoring fee is $32.99. Honestly, that's one of the most affordable rates on the market, especially considering all the equipment included in your system. That's basically just about a dollar a day for full protection for your family. It's peace of mind that's hard to put a price on."}],
        "transitions": ["Does that sound reasonable?", "Would you like to try it for the 60-day trial?"],
    },
    "spouse": {
        "signals": [
            "talk to my wife", "talk to my husband", "ask my wife", "ask my husband",
            "check with my wife", "check with my husband", "talk to my partner",
            "run it by my", "discuss with my", "talk to my spouse",
            "my wife needs to", "my husband needs to", "wife first",
            "husband first", "partner first",
            # Conversational
            "let me ask my wife", "let me ask my husband", "my wife would",
            "my husband would", "without my wife", "without my husband",
            "wife is not here", "husband is not here", "wife isn't here",
            "husband isn't here", "need my wife", "need my husband",
        ],
        "type": "Spouse",
        "summary": "Customer wants to talk to their partner first",
        "suggestions": [{"label": "Spouse Rebuttal", "text": "Sure, I completely understand, I'd want to check with my partner too before making a big decision like this. Just a heads up, the discount ends tonight, and I wouldn't want you to miss out. But don't worry, we have a 60-day return policy with a full refund, so you can try it and see if this system is the right fit for your needs."}],
        "transitions": ["Does that sound fair?", "Would the 60-day trial help with that decision?"],
    },
    "no_urgency": {
        "signals": [
            "call you back", "think about it", "let me think", "need to think",
            "want to think", "sleep on it", "not ready yet", "not sure yet",
            "give me some time", "come back later", "i'll get back",
            "get back to you", "call back later", "maybe later",
            "not right now", "not today",
            # Conversational
            "i'll call back", "call back tomorrow", "call back another",
            "need some time", "give me a day", "give me a few days",
            "think it over", "think on it", "sit on it", "mull it over",
            "need a minute", "need a moment", "let me process",
            "let me look into", "need to do more research",
        ],
        "type": "No Urgency",
        "summary": "Customer wants to think about it or call back later",
        "suggestions": [{"label": "No Urgency Rebuttal", "text": "No worries, you can definitely call us back when the time comes. Just keep in mind, the discount ends tonight, and I wouldn't want you to miss out. Is there anything else you'd like to know to help make your decision?"}],
        "transitions": ["Is there anything else I can help clarify?", "Would the 60-day trial help?"],
    },
    "shopping_around": {
        "signals": [
            "shopping around", "other companies", "calling other", "few different companies",
            "do some research", "do my research", "due diligence", "look around",
            "check other", "other options", "other quotes", "send me a quote",
            "send over a quote", "looking at other", "hear them out",
            "compare prices", "comparing", "shop around", "check around",
            "talk to other", "see what else", "other providers",
            # Conversational
            "look at other", "checking out other", "trying other",
            "want to see other", "looking at adt", "looking at ring",
            "looking at simplisafe", "looking at vivint",
            "what about adt", "what about ring", "what about simplisafe",
            "checked with", "spoke with", "talking to", "been looking at",
        ],
        "type": "Shopping Around",
        "summary": "Customer wants to compare with other companies",
        "suggestions": [{"label": "Shopping Around Rebuttal", "text": "I totally understand \u2014 it's smart to compare. Just so you know, a lot of people compare us with the big names and end up choosing Cove because of the no-contract, the pricing, and the customer service. And with the 60-day trial, you can try it risk-free. If it's not the right fit, you send it back for a full refund."}],
        "transitions": ["Does that make sense?", "Would you like to go ahead and try it risk-free?"],
    },
    "needs": {
        "signals": [
            "not sure i need", "don't think i need", "do i really need",
            "is it worth", "don't know if i need", "not convinced",
            "don't really need",
            # Conversational
            "do i need all this", "do i need all that", "that's a lot of stuff",
            "seems like a lot", "is all that necessary", "do i need that many",
            "more than i need", "i don't need all",
        ],
        "type": "Needs",
        "summary": "Customer isn't sure they need the system",
        "suggestions": [{"label": "Needs Rebuttal", "text": "No worries! We have a 60-day return policy with a full refund, so you can try it and see if this system is the right fit for your needs."}],
        "transitions": ["Would you like to try it risk-free?", "Does that sound fair?"],
    },
    "technician": {
        "signals": [
            "install it myself", "too hard to install", "can someone install",
            "send someone to install", "need a technician", "want a technician",
            "professional install", "hard to set up",
            # Conversational
            "how do i set it up", "who installs it", "who sets it up",
            "i'm not good with", "not good with technology", "not tech savvy",
            "someone come out", "send somebody", "come set it up",
        ],
        "type": "Technician / DIY",
        "summary": "Customer has concerns about installation",
        "suggestions": [{"label": "DIY Rebuttal", "text": "Most of our customers install the equipment themselves because it's really easy. This is a DIY system with wireless equipment. You can try installing it on your own, but if you need help, we also have a third-party technician service starting at $129."}],
        "transitions": ["Does that sound doable?", "Would you like to give it a try?"],
    },
    "no_monitoring": {
        "signals": [
            "self monitor", "self-monitor", "don't want monitoring",
            "no monitoring", "monitor myself", "don't need monitoring",
            "without monitoring",
            # Conversational
            "just use the app", "just the cameras", "use my phone",
            "monitor from my phone", "i'll monitor it myself",
            "do i have to have monitoring", "is monitoring required",
        ],
        "type": "Doesn't Want Monitoring",
        "summary": "Customer doesn't want professional monitoring",
        "suggestions": [{"label": "Monitoring Rebuttal", "text": "At Cove, we don't offer self-monitoring because we want to make sure our customers are fully protected 24/7, with police, fire, and medical support included. The monthly monitoring fee is just $32.99, about a dollar a day, for complete peace of mind that keeps your family safe without compromise."}],
        "transitions": ["Does that make sense?", "Would you like to try it for 60 days?"],
    },
    "only_cameras": {
        "signals": [
            "just want cameras", "only want cameras", "just the cameras",
            "only need cameras", "cameras only", "just cameras",
            # Conversational
            "only interested in cameras", "came for the cameras",
            "don't need sensors", "don't need the sensors",
            "don't want sensors", "skip the sensors",
        ],
        "type": "Only Wants Cameras",
        "summary": "Customer only wants cameras without the full system",
        "suggestions": [{"label": "Cameras Only Rebuttal", "text": "I understand where you're coming from. Cameras are great for keeping an eye on your home, but with our full system, including sensors, you get 24/7 protection with police, fire, and medical response, even when you're not watching."}],
        "transitions": ["Does that make sense?", "Would you like the full protection?"],
    },
    "personal_info": {
        "signals": [
            "don't want to give", "why do you need my", "don't give out my",
            "not comfortable giving", "rather not give my",
            # Conversational
            "skip that part", "can we skip", "just want the price first",
            "it doesn't matter", "that's not important", "not giving you my",
            "don't need my information", "why do you need that",
        ],
        "type": "Personal Info Resistance",
        "summary": "Customer doesn't want to share personal information yet",
        "suggestions": [{"label": "Personal Info Rebuttal", "text": "I totally understand being careful with your information. How about this \u2014 let me walk you through the equipment and pricing first so you can see if it's the right fit, and then we can get your information once you're ready to move forward."}],
        "transitions": ["Does that work for you?", "Should we look at the equipment first?"],
    },
    "payment_declined": {
        "signals": [
            "card declined", "card didn't work", "payment failed", "won't go through",
            "didn't go through", "card not working", "card was declined",
            # Conversational
            "not going through", "keeps declining", "says declined",
            "error with my card", "card isn't working", "payment won't",
        ],
        "type": "Payment Declined",
        "summary": "Customer's payment method was declined",
        "suggestions": [{"label": "Payment Rebuttal", "text": "Oh no problem \u2014 we accept any standard Visa, Mastercard, or Discover credit or debit card. Unfortunately we can't accept prepaid cards, Cash App cards, or PayPal. Do you have another card you could use?"}],
        "transitions": ["Do you have another card?", "Would you like to try a different payment method?"],
    },
    "existing_system": {
        "signals": [
            "already have a system", "use my existing", "keep my current",
            "already have equipment", "use the equipment i have",
            "existing equipment",
            # Conversational
            "already have cameras", "already have sensors", "already installed",
            "equipment from before", "keep what i have", "use what i have",
        ],
        "type": "Existing System",
        "summary": "Customer wants to use their existing security equipment",
        "suggestions": [{"label": "Existing System Rebuttal", "text": "Great news \u2014 you can actually keep using the equipment that's already installed there. What we'll do is set you up with a new account, get you a new hub and panel, and then we'll reset the existing cameras and sensors to connect to your new account."}],
        "transitions": ["Does that make sense?", "Should we get that set up for you?"],
    },
    "autopay": {
        "signals": [
            "when do i get charged", "billing date", "when is the payment",
            "autopay", "auto pay", "when does it charge", "first payment",
            # Conversational
            "when do they charge", "when does billing start",
            "when is the first charge", "what day do i pay",
        ],
        "type": "Autopay / Billing",
        "summary": "Customer has questions about billing date or autopay",
        "suggestions": [{"label": "Autopay Rebuttal", "text": "The autopay runs on the 5th of each month by default, but if you need a different billing date, you can call our customer service team after setup and they'll adjust it for you. Today you just pay for the equipment, and the monthly monitoring starts after your first month."}],
        "transitions": ["Does that work for you?", "Should we go ahead and get you set up?"],
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
        self._discovery_facts: list[str] = []  # customer facts from intro/discovery only (for personalization)
        self._equipment_mentioned: list[str] = []  # equipment already covered in build_system
        self.current_stage: str = "intro"  # synced from Session
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
            # Don't track equipment during recap/closing — the rep is reading
            # back items already in the system, not pitching new ones.
            # Also guard when _section_build_system is done (rep is in recap
            # territory even if the stage hasn't formally advanced yet).
            _in_recap_or_closing = (self.current_stage in ("closing",)
                                    or "recap_done" in self._topics_done
                                    or "_section_build_system" in self._topics_done)
            # Don't track equipment when the rep is ASKING about it
            # ("we have a motion detector... do you think you'd need any?")
            # Only track when the rep is CONFIRMING/ADDING it.
            _is_question = any(q in t for q in [
                "do you think", "would you like", "do you need", "you'd need",
                "you think you", "want to add", "like to add", "need any",
                "we also have a", "we have a motion", "we have a glass",
                "percent off", "% off", "either of those",
            ])
            for equip, keywords in self._EQUIPMENT_KEYWORDS.items():
                if equip not in self._equipment_mentioned and not _in_recap_or_closing:
                    # Don't count "built-in motion sensor" or "with a motion sensor"
                    # in camera descriptions as a separate motion sensor
                    if equip == "motion sensor" and any(p in t for p in [
                        "built-in motion sensor", "built in motion sensor",
                        "with a motion sensor", "with a built in",
                    ]):
                        continue
                    # Don't track optional items from rep questions/pitches —
                    # only from confirmations like "I'll add a motion detector".
                    # These items are presented as options, so the rep mentioning
                    # them doesn't mean the customer accepted.
                    if equip in ("motion sensor", "glass break", "co detector", "outdoor camera", "key fob", "medical pendant", "smoke detector"):
                        # Only track if the rep is clearly CONFIRMING (not asking/pitching)
                        _is_confirming = any(c in t for c in [
                            "i'll add", "i'll get you", "i'll throw in", "i'm adding",
                            "let me add", "adding a", "adding the", "i got you",
                            "i'll include", "included",
                        ])
                        if not _is_confirming:
                            continue
                    if any(kw in t for kw in keywords):
                        self._equipment_mentioned.append(equip)
                        self._sync_equipment_to_topics(equip)
                        print(f"[coach] equipment tracked: {equip}")
            # Mark topics done when rep asks
            for topic, rules in QUESTION_TOPICS.items():
                if topic not in self._topics_done:
                    if any(phrase in t for phrase in rules["rep_asks"]):
                        self._topics_done.add(topic)
                        self._sync_topic_aliases(topic)
                        print(f"[coach] topic DONE (rep asked): {topic}")
        # Track customer facts and mark topics done when customer answers
        if speaker == "customer":
            self._customer_facts.append(text.strip())
            # Only capture discovery-phase speech for personalization context.
            # This prevents address numbers ("fifteen north state street") and
            # phone digits from being misread as teen ages or other context.
            if self.current_stage in ("intro", "discovery"):
                self._discovery_facts.append(text.strip())
            # Track equipment the customer explicitly requests
            _CUSTOMER_EQUIP_PHRASES = {
                "outdoor camera": ["doorbell camera", "doorbell cam", "outdoor camera",
                                   "outdoor cam", "camera outside", "outside camera",
                                   "add the doorbell", "add a doorbell", "want the doorbell",
                                   "want a doorbell"],
                "motion sensor": ["motion sensor", "motion detector", "add a motion",
                                  "want a motion", "add the motion"],
                "glass break": ["glass break", "add a glass", "want a glass"],
                "co detector": ["carbon monoxide", "co detector", "add a carbon",
                                "add a co", "want a co"],
                "smoke detector": ["smoke detector", "add a smoke", "want a smoke"],
                "key fob": ["key fob", "key fobs", "keyfob", "add a key fob",
                             "want a key fob", "want key fob"],
                "medical pendant": ["medical pendant", "medical alert", "panic pendant",
                                    "medical button", "add a medical", "want a medical"],
            }
            for equip, phrases in _CUSTOMER_EQUIP_PHRASES.items():
                if equip not in self._equipment_mentioned:
                    if any(p in t for p in phrases):
                        self._equipment_mentioned.append(equip)
                        self._sync_equipment_to_topics(equip)
                        print(f"[coach] equipment tracked (customer requested): {equip}")
            # Discovery topics require the rep to have asked the question first
            # (or the call to be in discovery+ stage) before customer speech can
            # mark them done. This prevents "looking to get" in intro from
            # marking why_security as done before it's ever asked.
            _GATED_TOPICS = {"why_security", "had_system_before", "prior_provider",
                             "who_protecting", "kids_age"}
            _PAST_INTRO = self.current_stage not in ("intro",)
            # Build set of topics the rep has actually asked about
            _rep_asked_topics = set()
            for _topic, _rules in QUESTION_TOPICS.items():
                for _h in self._history:
                    if _h["speaker"] == "rep":
                        if any(phrase in _h["text"].lower() for phrase in _rules["rep_asks"]):
                            _rep_asked_topics.add(_topic)
                            break

            for topic, rules in QUESTION_TOPICS.items():
                if topic not in self._topics_done and rules["customer_answers"]:
                    if any(phrase in t for phrase in rules["customer_answers"]):
                        # Gate: discovery topics need rep to have asked OR be past intro
                        if topic in _GATED_TOPICS:
                            if not _PAST_INTRO and topic not in _rep_asked_topics:
                                print(f"[coach] BLOCKED premature topic '{topic}' (still in intro, rep hasn't asked)")
                                continue
                        self._topics_done.add(topic)
                        self._sync_topic_aliases(topic)
                        print(f"[coach] topic DONE (customer answered): {topic}")
            # Short answer heuristic: if customer says just "no"/"yes"/"yeah" etc.
            # OR a short answer followed by brief elaboration, mark the recent
            # rep question's topic as done.
            _short = t.strip().rstrip(".,!?")
            _SHORT_ANSWERS = {
                "no", "yes", "yeah", "yep", "nope", "sure", "okay", "ok",
                "no sir", "no ma'am", "yes sir", "yes ma'am", "yeah definitely",
                "no not yet", "not yet", "yes i do", "no i don't", "correct",
                "right", "exactly", "absolutely", "for sure", "of course",
                "that's right", "that's correct", "mm hmm", "uh huh", "nah",
                "no i do not", "yeah i do", "yep i do", "no i don't think so",
                "i don't", "we don't", "i do", "we do", "i have", "we have",
                "i haven't", "we haven't", "i haven't had", "we haven't had",
                "never", "never had", "never have", "first time",
                "yes we do", "no we don't", "yes we have", "no we haven't",
            }

            # Tokens that commonly START a discovery answer (short affirmative/negative)
            _ANSWER_STARTERS = (
                "no ", "yes ", "yeah ", "yep ", "nope ", "sure ", "okay ", "ok ",
                "we ", "i ", "nah ", "never ", "not ",
            )

            _should_match_topic = False
            if _short in _SHORT_ANSWERS:
                _should_match_topic = True
            elif len(_short.split()) <= 8 and _short.startswith(_ANSWER_STARTERS):
                # Short-to-medium answer starting with a common answer word
                _should_match_topic = True

            if _should_match_topic:
                # Look at last 4 rep turns for a topic match
                # This heuristic requires the rep to have recently asked the question,
                # so it's inherently gated. But still respect the intro gate for safety.
                _recent_rep = [h["text"].lower() for h in self._history[-8:]
                               if h["speaker"] == "rep"]
                for topic, rules in QUESTION_TOPICS.items():
                    if topic not in self._topics_done:
                        # Gate: don't mark gated discovery topics during intro
                        if topic in _GATED_TOPICS and not _PAST_INTRO:
                            continue
                        for rep_text in _recent_rep:
                            if any(phrase in rep_text for phrase in rules["rep_asks"]):
                                self._topics_done.add(topic)
                                self._sync_topic_aliases(topic)
                                print(f"[coach] topic DONE (contextual answer to rep question): {topic}")
                                break
            # Try to extract customer's first name
            if not self.customer_name:
                # Strip leading fillers so "yeah my name is Thomas" parses correctly
                _cleaned = text.strip()
                for _filler_prefix in ["yeah ", "yes ", "yep ", "sure ", "okay ", "ok ",
                                        "alright ", "so ", "um ", "uh ", "well ",
                                        "yeah so ", "yes so ", "ok so "]:
                    if _cleaned.lower().startswith(_filler_prefix):
                        _cleaned = _cleaned[len(_filler_prefix):]
                        break
                t_lower = _cleaned.lower()
                # Common words that follow "I'm" but are NOT names
                _NOT_NAMES = {"fine", "good", "great", "well", "ok", "okay", "alright",
                              "doing", "interested", "looking", "calling", "here", "ready",
                              "not", "just", "also", "really", "very", "so", "the", "me",
                              "actually", "already", "currently", "still", "honestly",
                              "basically", "literally", "probably", "definitely", "maybe",
                              "trying", "thinking", "wondering", "hoping", "getting",
                              "having", "going", "coming", "working", "living", "using",
                              "planning", "switching", "moving", "buying", "checking",
                              "being", "feeling", "sitting", "standing", "running",
                              "glad", "happy", "sure", "sorry", "curious", "new",
                              "fantastic", "wonderful", "terrible", "awesome", "amazing",
                              "perfect", "excellent", "pretty", "super", "blessed", "only",
                              "telling", "around", "sitting",
                              "processing", "done", "finished", "waiting", "holding",
                              "my", "your", "his", "her", "our", "their", "its",
                              "home", "here", "there", "now", "then", "about", "over",
                              "in", "on", "up", "out", "at", "from", "with", "for",
                              "so", "if", "or", "an", "no", "oh", "uh", "um",
                              "locate", "located", "like", "wanting", "waiting",
                              "zero", "one", "two", "three", "four", "five", "six",
                              "seven", "eight", "nine", "ten", "eleven", "twelve",
                              "windows", "doors", "gonna", "hundred", "thousand"}
                for prefix in ["my name is ", "my first name is ", "first name is ", "i'm ", "this is ", "it's "]:
                    if prefix in t_lower:
                        after = _cleaned[t_lower.index(prefix) + len(prefix):].strip()
                        first_word = after.split()[0] if after.split() else ""
                        # Basic validation: capitalize, skip very short or non-alpha or common words
                        if (len(first_word) >= 2 and first_word.isalpha()
                                and first_word.lower() not in _NOT_NAMES):
                            self.customer_name = first_word.capitalize()
                            print(f"[coach] customer name detected: {self.customer_name}")
                        break

    # Equipment keywords to track what's been presented in build_system
    _EQUIPMENT_KEYWORDS = {
        "door sensor": ["door sensor", "door sensors"],
        "window sensor": ["window sensor", "window sensors"],
        "camera": ["camera", "indoor camera"],
        "outdoor camera": ["outdoor camera", "doorbell camera", "solar camera", "solar powered"],
        "panel": ["panel", "touchscreen", "touch screen"],
        "monitoring": ["monitoring", "24/7", "police fire"],
        "chime": ["chime", "chime feature"],
        "yard sign": ["yard sign", "window sticker"],
        "smartphone": ["smartphone", "app access", "phone access", "remote"],
        "smoke detector": ["smoke detector", "smoke sensor"],
        "motion sensor": ["motion sensor", "motion detect"],
        "glass break": ["glass break", "glass sensor"],
        "co detector": ["carbon monoxide", "co detector", "c o detector"],
        "key fob": ["key fob", "keyfob", "key fobs"],
        "medical pendant": ["medical pendant", "medical alert", "panic pendant"],
    }

    # Map equipment names → _STAGE_ITEM_ORDER keys so _topics_done stays in sync
    _EQUIP_TO_TOPIC = {
        "door sensor": "door_sensors",
        "window sensor": "window_sensors",
        "camera": "indoor_camera",
        "outdoor camera": "outdoor_camera",
        "panel": "panel_hub",
        "monitoring": "panel_hub",
        "chime": "door_sensors",
        "yard sign": "yard_sign",
        "smartphone": "yard_sign",
        "smoke detector": "extra_equip",
        "motion sensor": "extra_equip",
        "glass break": "extra_equip",
        "co detector": "extra_equip",
        "key fob": "extra_equip",
        "medical pendant": "extra_equip",
    }

    def _sync_equipment_to_topics(self, equip: str):
        """When equipment is tracked, also mark the corresponding topic as done
        so _fallback_next_step skips it."""
        topic = self._EQUIP_TO_TOPIC.get(equip)
        if topic and topic not in self._topics_done:
            self._topics_done.add(topic)
            print(f"[coach] topic synced from equipment: {topic}")

    # QUESTION_TOPICS keys ↔ _STAGE_ITEM_ORDER keys that refer to the same thing
    _TOPIC_ALIASES = {
        "how_many_doors": "door_sensors",
        "door_sensors": "how_many_doors",
        "how_many_windows": "window_sensors",
        "window_sensors": "how_many_windows",
        "prior_provider": "had_system_before",
        "had_system_before": "prior_provider",
    }

    def _sync_topic_aliases(self, topic: str):
        """When a topic is marked done, also mark its alias so both key systems
        (QUESTION_TOPICS and _STAGE_ITEM_ORDER) stay in sync."""
        alias = self._TOPIC_ALIASES.get(topic)
        if alias and alias not in self._topics_done:
            self._topics_done.add(alias)
            print(f"[coach] topic alias synced: {topic} -> {alias}")

    def check_repeated_topic(self, next_step: str) -> str | None:
        """Return the topic name if next_step is RE-ASKING a question already done.
        Only blocks if the next_step is actually asking the question (contains a question
        phrase), not just referencing the topic in a statement."""
        t = next_step.lower()
        # Check entire text for question indicators (? can be far from the key phrase)
        has_question_mark = "?" in t
        for topic in self._topics_done:
            rules = QUESTION_TOPICS.get(topic)
            if not rules:
                continue
            for phrase in rules["output_detect"]:
                if phrase in t:
                    # Make sure it's actually a question, not a statement referencing the topic.
                    idx = t.index(phrase)
                    surrounding = t[max(0, idx - 20):idx + len(phrase) + 60]
                    is_question = has_question_mark or any(qw in surrounding for qw in [
                        "what ", "who ", "how ", "have you", "did you", "are you",
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
        _is_question = any(q in t for q in [
            "do you think", "would you like", "do you need", "you'd need",
            "want to add", "like to add", "need any",
        ])
        for equip, keywords in self._EQUIPMENT_KEYWORDS.items():
            if equip not in self._equipment_mentioned:
                # Don't count camera-context motion sensor
                if equip == "motion sensor" and any(p in t for p in [
                    "built-in motion sensor", "built in motion sensor",
                    "with a motion sensor", "with a built in",
                ]):
                    continue
                # Don't track extras or optional items from questions about them
                if _is_question and equip in ("motion sensor", "glass break", "co detector", "outdoor camera"):
                    continue
                if any(kw in t for kw in keywords):
                    self._equipment_mentioned.append(equip)
                    self._sync_equipment_to_topics(equip)
                    print(f"[coach] equipment tracked (from suggestion): {equip}")

    def mark_addressed(self, objection_type: str):
        if objection_type and objection_type not in self._addressed:
            self._addressed.append(objection_type)

    def detect_objection(self, text: str) -> dict | None:
        """Fast keyword-based objection detection as fallback when Claude misses.
        Returns rebuttal data dict if an objection is found, None otherwise."""
        t = text.lower()
        for _key, data in OBJECTION_REBUTTALS.items():
            if any(sig in t for sig in data["signals"]):
                if data["type"] in self._addressed:
                    continue
                return {
                    "triggered": True,
                    "objection_type": data["type"],
                    "objection_summary": data["summary"],
                    "suggestions": data["suggestions"],
                    "transitions": data["transitions"],
                }
        return None

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

        opener_note = ""
        if self._last_opener:
            opener_note = (
                f"\n\n⚠️ A 'SAY FIRST' bubble is already showing the rep this opener: \"{self._last_opener}\"\n"
                "Your next_step appears as a 'THEN' bubble BELOW the opener. "
                "Do NOT repeat or paraphrase the opener in your next_step — go straight to the next "
                "script question or action. The rep reads the opener first, then your next_step."
            )
        else:
            opener_note = (
                "\n\n⚠️ Your next_step is the ONLY thing the rep sees. "
                "Start with a brief, natural transition (like 'Perfect,' or 'Got it,') then go straight "
                "into the next script question. Keep it in one smooth line."
            )

        # Inject tuning notes from auto-analysis (if any)
        tuning_note = ""
        tuning = get_latest_tuning()
        if tuning:
            all_additions = []
            for a in tuning.get("coaching_additions", []):
                all_additions.append(a)
            for a in tuning.get("user_feedback_actions", []):
                all_additions.append(a)
            if all_additions:
                tuning_note = (
                    "\n\n═══ COACHING ADJUSTMENTS (from call analysis) ═══\n"
                    + "\n".join(f"- {a}" for a in all_additions)
                    + "\nApply these adjustments when relevant.\n"
                )

        user_content = (
            f"Live call transcript (most recent at bottom):\n\n{transcript}"
            f"{addressed_note}"
            f"{blocklist_note}"
            f"{equipment_note}"
            f"{opener_note}"
            f"{tuning_note}\n\n"
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
                    "system": [{
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
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
            # Log response body for HTTP errors to diagnose 400s
            resp_body = ""
            if hasattr(e, 'response') and e.response is not None:
                try:
                    resp_body = e.response.text[:500]
                except Exception:
                    pass
            print(f"[coach] error: {e}\n{resp_body}\n{traceback.format_exc()}")
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
        self._discovery_facts = []
        self._equipment_mentioned = []
        self._last_opener = ""
        self.customer_name = ""
        self._topics_done = set()
        self.current_stage = "intro"
