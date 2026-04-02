import asyncio
import random
import httpx

from transcript_store import get_latest_tuning

# 5 Deepgram Aura voices — varied gender and accent
VOICES = [
    "aura-asteria-en",   # female, American
    "aura-luna-en",      # female, American (warmer)
    "aura-orion-en",     # male, American
    "aura-arcas-en",     # male, American (deeper)
    "aura-helios-en",    # male, British
]

# 100 customer scenarios — varied home, household, reason, and prior experience
SCENARIOS = [
    "You just moved into a 3-bedroom house in a suburb of Dallas with your spouse and two kids (ages 5 and 8). A neighbor mentioned there was a break-in two streets over last month.",
    "You live alone in a 2-bedroom condo in Chicago. You recently had a package stolen off your porch and you're tired of it.",
    "You and your husband just had your first baby. You're in a 4-bedroom house in Phoenix and you've never had a security system.",
    "You're a retiree living alone in a ranch-style home in Florida. Your kids keep telling you to get a system and you finally decided to listen.",
    "You moved into a townhouse in Atlanta 6 months ago. You travel for work 2 weeks out of every month and worry about leaving the place empty.",
    "You have a large home in suburban Denver with your wife and three teenagers. You had ADT years ago but canceled it because it was too expensive.",
    "You just bought a house in a rural area outside Nashville. The nearest police station is 20 minutes away and that makes you nervous.",
    "You're renting a house in Seattle with two roommates. You want security but you're not sure if renters can even get a system.",
    "You live in a 3-bedroom house in Las Vegas with your partner and a golden retriever. Your neighbor's car was broken into last week.",
    "You're a single mom with a 10-year-old daughter in a suburb of Minneapolis. You work nights twice a week and hate leaving her with a sitter.",
    "You just bought your first home in Charlotte — a 2-bedroom bungalow. You have no idea what kind of security system you need.",
    "You and your wife have a 5-bedroom home in suburban Houston. You've been meaning to get a system for years and a recent news story finally pushed you.",
    "You live in a condo in San Diego with your boyfriend and a cat. You've been looking at Ring but haven't pulled the trigger.",
    "You're a veteran living alone in a 3-bedroom house in Colorado Springs. You like DIY projects and are very hands-on.",
    "You just moved from an apartment to a house in Raleigh with your family — spouse, three kids (ages 3, 6, and 12), and a beagle.",
    "You're in your 60s, live alone in a single-story home in Scottsdale, and you've been watching too many crime documentaries.",
    "You and your partner just moved into a duplex in Portland. You share a wall and want to make sure only your unit is covered.",
    "You have a 2-story home in suburban St. Louis with your husband and twin boys (age 9). You heard about a home invasion in the next town.",
    "You're a small business owner who works from home in Sacramento. You have expensive equipment and want to protect the office space.",
    "You live in a farmhouse outside Kansas City on 5 acres. You've had issues with trespassers and want cameras for the property.",
    "You and your spouse just became empty nesters in a 4-bedroom home in suburban Boston. You're thinking about travel and want peace of mind.",
    "You're a grad student sharing a house in Austin with one roommate. You want a basic system that won't break the bank.",
    "You just moved to the United States from abroad and bought your first home in New Jersey. You're not familiar with home security options here.",
    "You have a 3-bedroom home in suburban Memphis with your girlfriend and her teenage son. There have been car break-ins on your street.",
    "You're a nurse who works 12-hour shifts in Detroit. You're worried about your home being empty for long stretches.",
    "You live in a 2-bedroom house in Albuquerque with your husband and elderly mother-in-law who has mobility issues.",
    "You and your wife have a vacation home in the mountains outside Asheville in addition to your primary home. You want to protect both.",
    "You're a landlord with a rental property in Cleveland and you want to put a system in it for your tenants.",
    "You just had a medical scare and your family convinced you to get monitoring that includes medical alert features. You live alone in Tampa.",
    "You're in a gated community in Boca Raton and thought you didn't need security — until your neighbor's garage was broken into.",
    "You have a 3-bedroom home in suburban Indianapolis with your wife and a newborn. Every little noise at night makes you anxious.",
    "You live in a high-rise apartment in Manhattan but you also have a townhouse in Long Island you use on weekends.",
    "You're a teacher in Omaha. You have a modest 2-bedroom home and a tight budget but safety is a priority.",
    "You and your husband have a home in suburban Cincinnati with a large backyard. You want cameras to keep an eye on the yard and driveway.",
    "You just retired and downsized to a 2-bedroom home in Tucson. You want a simple, easy-to-use system.",
    "You're a remote worker who moved to a rural area outside Boulder during the pandemic. You didn't think you'd stay but now you are.",
    "You have a 4-bedroom home in suburban Baltimore with your wife and three kids. You had SimpliSafe before but found the app frustrating.",
    "You live alone in a 1-bedroom house in Louisville. You're in your 30s and safety-conscious after your apartment building had a robbery.",
    "You and your partner just purchased a historic home in Savannah. You're renovating and want security during the construction phase.",
    "You live in a mobile home community in Albuquerque. You're not sure if wireless systems work in your setup.",
    "You have a 3-bedroom house in suburban Oklahoma City with your husband and two large dogs. You want sensors on the doors the dogs can't reach.",
    "You're a single dad in Fresno with two kids (ages 7 and 14). Your kids come home from school before you get off work.",
    "You have a weekend cabin in Wisconsin in addition to your primary home in Chicago. The cabin is remote and has been broken into before.",
    "You just bought a house in suburban Salt Lake City. You have a spouse and are expecting your first child in 3 months.",
    "You're a property manager in Miami overseeing several units and you want to standardize security across all of them.",
    "You live in a townhouse in suburban DC with your husband and a toddler. Your husband travels constantly for work.",
    "You're a college professor with a 3-bedroom home in Madison, WI. You're very research-oriented and want to understand everything before buying.",
    "You have a large home in suburban San Antonio with your wife, three kids, and your wife's parents living in the back unit.",
    "You just moved back to your hometown in rural Georgia and bought a fixer-upper. You're doing a lot of the renovation yourself.",
    "You live in a split-level home in suburban Cleveland with your partner and two cats. There's been a series of garage door thefts in your area.",
    "You're newly divorced and just moved into a house alone in suburban Phoenix for the first time. You want to feel safe.",
    "You have a home in a flood-prone area outside New Orleans and want monitoring that includes environmental alerts, not just burglary.",
    "You and your husband have a 4-bedroom home in suburban Minneapolis. You fostered a child last year and are thinking about doing it again.",
    "You're a firefighter in Columbus, OH who works 24-hour shifts. Your spouse is alone at home with two young kids during those shifts.",
    "You just moved into a house in suburban Richmond with your fiancé. You're getting married in 4 months and this is your first home together.",
    "You have a 3-bedroom condo in a high-rise in downtown San Francisco. You're worried about break-ins via the parking garage.",
    "You're semi-retired in suburban Raleigh, living alone since your spouse passed away last year. Your adult kids are pushing you to get a system.",
    "You have a 5-bedroom home in suburban Plano, TX with your wife and four kids. You had a system before but the contract was a nightmare.",
    "You're an Airbnb host in Nashville with two properties. You want to monitor them remotely without invading guest privacy.",
    "You live in a 2-bedroom home in Boise, Idaho. You moved from California last year and feel safer here but still want coverage.",
    "You're a first-generation homeowner in suburban Detroit. You bought a 3-bedroom house and want to protect your investment.",
    "You have a 4-bedroom home in suburban Fort Worth with your partner and a teenage daughter who's home alone after school.",
    "You live in a rural area outside Spokane. Internet is spotty, so you need something that works on cellular backup.",
    "You just moved to a new city for a job — you're in a 2-bedroom house in Columbus and don't know the neighborhood yet.",
    "You're a real estate agent in suburban Tampa with a home office. You meet clients you don't know and want security cameras at the entrance.",
    "You have a 3-bedroom home in suburban Jacksonville. Your spouse is a travel nurse and is away for weeks at a time.",
    "You live alone in a 3-bedroom home in suburban Sacramento. You're in your early 40s and just bought the house after renting for years.",
    "You have a home in suburban Charlotte with your husband. You have elderly parents nearby who you check on often and want them protected too.",
    "You're a military spouse at Fort Bragg living in a 3-bedroom off-base home with two kids. Your spouse is deployed.",
    "You live in a 2-bedroom house in suburban Albany with your girlfriend. The street has had some vandalism issues recently.",
    "You have a 4-bedroom home in suburban Kansas City. Your kids are in high school and you want to know when they come and go.",
    "You just moved from a condo to a house in suburban Orlando. You've never owned a home with a yard and feel exposed.",
    "You live in a Victorian home in a historic neighborhood of Richmond, VA. You're worried about break-ins given all the foot traffic nearby.",
    "You're a stay-at-home parent in suburban Denver with three kids under 6. Your biggest concern is someone getting in while the kids are home.",
    "You have a home in suburban Providence, RI with your husband and two teenagers. A nearby school had a lockdown recently and it shook you.",
    "You just bought a home in a new subdivision outside of Tampa. Construction is still happening all around and equipment has been stolen.",
    "You live in a 3-bedroom house in suburban Newark. There's been an uptick in crime in the city and it's spreading to your neighborhood.",
    "You're a landlord in Pittsburgh with two rental properties you want to put systems in for liability protection.",
    "You have a 2-bedroom house in suburban Tulsa with your partner. You're very price-conscious and comparison-shopping several companies.",
    "You live alone in a house in suburban Hartford, CT. You're a homebody and want to feel completely secure inside.",
    "You have a 4-bedroom home in suburban Richmond with your wife and two kids. You work late most nights and get home after everyone's asleep.",
    "You just moved into a house in Palm Springs. You split your time between there and your primary home in LA.",
    "You have a 3-bedroom home in suburban Lexington, KY with your husband. A house down the street was robbed in broad daylight last month.",
    "You're a college student who just moved off campus into a rental house in Tempe, AZ with two other students.",
    "You live in a 3-bedroom house in suburban Birmingham, AL with your wife and a newborn and a toddler.",
    "You have a home in suburban Buffalo and the long, dark winters make you nervous about the house when you travel south for the holidays.",
    "You live in a 2-bedroom ranch home in suburban Peoria, IL. You're in your 50s, your kids are grown, and the house feels big and quiet.",
    "You just purchased a fixer-upper duplex in New Haven, CT. You live in one unit and rent the other. You want to protect both.",
    "You have a 4-bedroom home in suburban Memphis with your wife and three kids under 10. You coach youth sports and are rarely home.",
    "You live in a 2-bedroom home in suburban Chattanooga with your girlfriend. You just got a dog and want to watch it during the day.",
    "You're a retiree in suburban Clearwater, FL. You travel a few months a year and want to monitor the house remotely.",
    "You have a 3-bedroom house in suburban Columbus with your husband and two kids. A child in your neighborhood went missing briefly last year.",
    "You live in a condo in suburban San Jose. You're very tech-savvy and want a system you can integrate with your smart home setup.",
    "You have a 4-bedroom home in suburban Aurora, CO with your wife and three kids. The neighborhood Facebook group has been buzzing with crime alerts.",
    "You just moved into a house in suburban Anchorage, AK. You're new to the state and feel isolated. Bears are less of a concern than break-ins.",
    "You live in a 2-bedroom house in suburban Little Rock with your partner. You were burglarized at your previous home and it still affects you.",
]


CUSTOMER_PERSONA_TEMPLATE = """You are roleplaying as a customer who has called Cove Smart home security to inquire about getting a security system.

YOUR SITUATION: {scenario}

Rules:
- YOU called THEM. You're the one looking for security. Open with something natural like "Hi, yeah I'm calling about getting a home security system" or "Hey, I saw an ad online and wanted to get some info."
- Be cooperative. Answer the rep's questions DIRECTLY with the specific details from your situation above.
- When asked for specific information (name, phone, email, address) — give it. Do not deflect or ask what else they need.
- React naturally. If the rep is warm and helpful, warm up. If pushy, pull back slightly.
- Raise concerns ONE AT A TIME, naturally woven into the conversation — not as a list.
- Keep every response SHORT — 1 to 3 sentences. Speak like a real person on the phone, not a script.
- Do not use sales language or formal phrasing.
- NEVER output stage directions, actions, or text in parentheses like "(waiting for the rep to respond)" or "(pauses)". Only speak actual dialogue.
- If the rep asks a question and you have nothing new to add, give a brief natural acknowledgment like "Yeah, sounds good" or "Okay, sure."
- If the rep handles your concerns well, agree to move forward.

Concerns to raise naturally as the conversation progresses (pick the right moment, don't force them all):
1. Monthly fee — react when monitoring cost is mentioned
2. Upfront cost — react when equipment pricing comes up
3. Installation — react when equipment is mentioned
4. Needing to check with a spouse/partner — raise before committing (skip if your situation has no partner)

Be a realistic, reasonable person who is genuinely interested but has normal hesitations."""


class RoleplayCustomer:
    _API_URL = "https://api.anthropic.com/v1/messages"
    _HEADERS_BASE = {"anthropic-version": "2023-06-01", "content-type": "application/json"}

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=30)
        self._history: list[dict] = []
        self.voice = random.choice(VOICES)
        scenario = random.choice(SCENARIOS)
        self._persona = self._build_persona(scenario)

    @staticmethod
    def _build_persona(scenario: str) -> str:
        persona = CUSTOMER_PERSONA_TEMPLATE.format(scenario=scenario)
        tuning = get_latest_tuning()
        if tuning:
            additions = tuning.get("roleplay_additions", [])
            if additions:
                persona += (
                    "\n\nADDITIONAL BEHAVIOR RULES (from call analysis):\n"
                    + "\n".join(f"- {a}" for a in additions)
                )
        return persona

    async def _call(self, messages, max_tokens=120):
        resp = await self._http.post(
            self._API_URL,
            headers={**self._HEADERS_BASE, "x-api-key": self._api_key},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "system": self._persona,
                "messages": messages,
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()

    async def opening_line(self) -> str:
        msg = "You just called Cove Smart security. Give your short, natural opening line as the customer."
        text = await self._call([{"role": "user", "content": msg}], max_tokens=80)
        self._history = [
            {"role": "user", "content": msg},
            {"role": "assistant", "content": text},
        ]
        return text

    async def respond(self, rep_speech: str) -> str:
        self._history.append({"role": "user", "content": rep_speech})
        # Keep history manageable — last 20 messages plus the opening exchange
        if len(self._history) > 22:
            self._history = self._history[:2] + self._history[-20:]
        text = await self._call(self._history)
        self._history.append({"role": "assistant", "content": text})
        return text

    def reset(self):
        self._history = []
        self.voice = random.choice(VOICES)
        scenario = random.choice(SCENARIOS)
        self._persona = self._build_persona(scenario)
