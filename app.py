from fastapi import FastAPI, Response, Request
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from openai import OpenAI
import os, time, requests, re
from urllib.parse import quote_plus

app = FastAPI()

# === API keys / config ===
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
GOOGLE_API_KEY = os.environ.get("google_maps_key") or os.environ.get("GOOGLE_MAPS_KEY")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")  # SMS-capable Twilio number (+1XXXXXXXXXX)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# === Store details ===
STORE_INFO = {
    "name": "CPR Cell Phone Repair",
    "city": "Myrtle Beach",
    "address": "1000 South Commons Drive, Myrtle Beach, SC 29588",
    "hours": "Monday–Saturday 9am–6pm, Sunday we are closed",
    "phone": "(843) 750-0449"
}

# === Per-call session data ===
call_activity = {}     # last interaction timestamp
call_mode = {}         # "normal", "awaiting_origin", "offer_sms"
call_memory = {}       # short-term conversation memory
caller_numbers = {}    # call_sid -> '+1XXXXXXXXXX'
pending_maps = {}      # call_sid -> {'link': str}

MAX_MEMORY = 5

# === Voice helper (slow speech) ===
def slow_say(self, text, **kwargs):
    kwargs.setdefault("voice", "Polly.Matthew")
    self._original_say(
        f'<speak><prosody rate="90%">{text}</prosody></speak>',
        **kwargs
    )

VoiceResponse._original_say = VoiceResponse.say
VoiceResponse.say = slow_say

# === Memory helper ===
def remember(call_sid, role, content):
    if call_sid not in call_memory:
        call_memory[call_sid] = []
    call_memory[call_sid].append({"role": role, "content": content})
    call_memory[call_sid] = call_memory[call_sid][-MAX_MEMORY:]

# === Geo helpers ===
def geocode_address(address):
    print(f"[DEBUG] Geocoding request for: '{address}'")
    geo_url = "https://maps.googleapis.com/maps/api/geocode/json"
    geo_params = {
        "address": address,
        "components": "locality:Myrtle Beach|administrative_area:SC|country:US",
        "key": GOOGLE_API_KEY
    }
    r = requests.get(geo_url, params=geo_params)
    geo_data = r.json()

    if geo_data.get("status") == "OK" and geo_data.get("results"):
        loc = geo_data["results"][0]["geometry"]["location"]
        return f"{loc['lat']},{loc['lng']}"

    places_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    places_params = {
        "query": f"{address}, Myrtle Beach, SC",
        "key": GOOGLE_API_KEY
    }
    r = requests.get(places_url, params=places_params)
    places_data = r.json()
    if places_data.get("status") == "OK" and places_data.get("results"):
        loc = places_data["results"][0]["geometry"]["location"]
        return f"{loc['lat']},{loc['lng']}"
    return None

def get_directions(origin, destination):
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": origin,
        "destination": destination,
        "mode": "driving",
        "region": "us",
        "key": GOOGLE_API_KEY
    }
    r = requests.get(url, params=params)
    directions_data = r.json()
    if directions_data.get("status") == "OK" and directions_data.get("routes"):
        leg = directions_data["routes"][0]["legs"][0]
        return {
            "duration": leg["duration"]["text"],
            "distance": leg["distance"]["text"]
        }
    return None

def build_maps_link(origin_coords: str, destination_addr: str) -> str:
    origin_param = quote_plus(origin_coords)
    dest_param = quote_plus(destination_addr)
    return f"https://www.google.com/maps/dir/?api=1&origin={origin_param}&destination={dest_param}&travelmode=driving"

# === Voice endpoints ===
@app.post("/voice/outbound/intro")
async def intro(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    from_number = form.get("From")
    if from_number:
        caller_numbers[call_sid] = from_number
    call_activity[call_sid] = time.time()
    call_mode[call_sid] = "normal"
    call_memory[call_sid] = []

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/voice/outbound/process",
        method="POST",
        timeout=15,
        speech_timeout="auto",
        hints="repair, screen, battery, directions, hours, location, address, phone number, iphone, samsung, price, motorola, lg, google, pixel"
    )
    gather.say(f"Thank you for calling {STORE_INFO['name']} in {STORE_INFO['city']}. How can I help you today?")
    vr.append(gather)
    return Response(str(vr), media_type="application/xml")

@app.post("/voice/outbound/process")
async def process(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    from_number = form.get("From")
    if from_number:
        caller_numbers[call_sid] = from_number

    user_input = (form.get("SpeechResult") or "").strip()
    lower_input = user_input.lower()
    vr = VoiceResponse()
    call_activity[call_sid] = time.time()

    # End call
    if any(p in lower_input for p in ["thank you, bye", "thank you bye", "goodbye", "bye", "that's all", "hang up"]):
        vr.say(f"Thank you for calling {STORE_INFO['name']}. Goodbye.")
        vr.hangup()
        return Response(str(vr), media_type="application/xml")

    yes_phrases = ["yes", "yeah", "yep", "sure", "please", "ok", "okay", "send it", "text me", "send me the link", "that would help"]
    no_phrases = ["no", "nope", "not now", "don't", "do not", "i'm good", "i am good"]

    # === Directions intent (expanded triggers) ===
    if (
        re.search(r"\bdirections?\b", lower_input) or
        re.search(r"\b(i\s+need|looking\s+for|get|send|give|can\s+you\s+send\s+me)\s+directions?\b", lower_input) or
        re.search(r"how\s+do\s+i\s+get\s+(there|to\s+you|to\s+the\s+store|to\s+your\s+location)", lower_input) or
        re.search(r"how\s+to\s+get\s+(there|to\s+you|to\s+the\s+store)", lower_input) or
        re.search(r"where\s+(are\s+(you|y['’]all|ya['’]ll|yall)|ya['’]ll|yall)\s+at", lower_input)
    ):
        vr.say("Sure, what is your starting address or location?")
        call_mode[call_sid] = "awaiting_origin"
        gather = Gather(input="speech", action="/voice/outbound/process",
                        method="POST", timeout=20, speech_timeout="auto")
        vr.append(gather)
        return Response(str(vr), media_type="application/xml")

    # Handle SMS offer response
    if call_mode.get(call_sid) == "offer_sms":
        if any(p in lower_input for p in yes_phrases):
            to_number = caller_numbers.get(call_sid)
            link_info = pending_maps.get(call_sid)
            if to_number and link_info and twilio_client and TWILIO_FROM_NUMBER:
                try:
                    twilio_client.messages.create(
                        to=to_number,
                        from_=TWILIO_FROM_NUMBER,
                        body=f"Directions to {STORE_INFO['name']}: {link_info['link']}"
                    )
                    vr.say("Sent. Tap the link in the text to open Google Maps and start navigation.")
                except Exception as e:
                    print(f"[ERROR] SMS send failed: {e}")
                    vr.say("I couldn't send the text just now.")
            else:
                vr.say("I couldn't send the text right now. Search Google Maps for our address.")
            pending_maps.pop(call_sid, None)
            call_mode[call_sid] = "normal"
            g = Gather(input="speech", action="/voice/outbound/process", method="POST", timeout=20, speech_timeout="auto")
            vr.append(g)
            return Response(str(vr), media_type="application/xml")

        if any(p in lower_input for p in no_phrases):
            vr.say("Okay. If you change your mind, just say 'text me the directions'.")
            pending_maps.pop(call_sid, None)
            call_mode[call_sid] = "normal"
            g = Gather(input="speech", action="/voice/outbound/process", method="POST", timeout=20, speech_timeout="auto")
            vr.append(g)
            return Response(str(vr), media_type="application/xml")

        vr.say("Sorry, I didn't catch that. Do you want me to text you the Google Maps link?")
        g = Gather(input="speech", action="/voice/outbound/process", method="POST", timeout=20, speech_timeout="auto")
        vr.append(g)
        return Response(str(vr), media_type="application/xml")

        # === Awaiting origin → compute ETA + offer SMS ===
    if call_mode.get(call_sid) == "awaiting_origin":
        origin_coords = geocode_address(user_input)
        if not origin_coords:
            vr.say("I couldn't find that location. Try a street address or a well-known place nearby.")
            g = Gather(input="speech", action="/voice/outbound/process", method="POST", timeout=20, speech_timeout="auto")
            vr.append(g)
            return Response(str(vr), media_type="application/xml")

        directions = get_directions(origin_coords, STORE_INFO["address"])
        if not directions:
            vr.say("I couldn't get directions from that location. Could you try a different starting point?")
            g = Gather(input="speech", action="/voice/outbound/process", method="POST", timeout=20, speech_timeout="auto")
            vr.append(g)
            return Response(str(vr), media_type="application/xml")

        # Speak ETA
        vr.say(f"We are about {directions['distance']}, roughly a {directions['duration']} drive from there.")

        # Store link + offer SMS
        maps_link = build_maps_link(origin_coords, STORE_INFO["address"])
        pending_maps[call_sid] = {"link": maps_link}
        call_mode[call_sid] = "offer_sms"

        vr.say("Would you like me to text you a Google Maps link to start navigation?")
        g = Gather(input="speech", action="/voice/outbound/process", method="POST", timeout=20, speech_timeout="auto")
        vr.append(g)
        return Response(str(vr), media_type="application/xml")

    # === Main store-info intents ===
    if re.search(r"\bhours?\b|\bwhen\s+are\s+you\s+open\b|\bwhat\s+time\s+do\s+you\b", lower_input):
        vr.say(f"Our hours are {STORE_INFO['hours']}.")
    elif re.search(r"((where\s+(are\s+(you|y['’]all|ya['’]ll|yall)|y['’]all|ya['’]ll|yall)\s+at)|(is\s+the\s+store\s+located)|(address)|(location))", lower_input):
        vr.say(f"We are located at {STORE_INFO['address']}.")
    elif re.search(r"\b(phone|number)\b", lower_input):
        vr.say(f"Our phone number is {STORE_INFO['phone']}.")
    elif re.search(r"(landmark|nearby|close\s+to|around\s+(you|there)|what'?s\s+(near|around)\s+(you|there))", lower_input):
        vr.say(
            "We are near Goodwill and Lowe's Home Improvement, in the strip mall with Chipotle, McAlister's, "
            "Sport Clips, and the UPS Store. We're also not far down the road from East Coast Honda."
        )
    # === AI fallback for anything else ===
    else:
        try:
            system_prompt = f"""
            You are a warm, knowledgeable receptionist for {STORE_INFO['name']} in {STORE_INFO['city']}.
            You can chat naturally, answer open-ended repair or product questions,
            but DO NOT guess store details like hours, address, phone, or landmarks.
            """
            messages = [{"role": "system", "content": system_prompt}]
            if call_sid in call_memory:
                messages.extend(call_memory[call_sid])
            messages.append({"role": "user", "content": user_input})

            ai_reply = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )
            reply_text = ai_reply.choices[0].message.content
            remember(call_sid, "user", user_input)
            remember(call_sid, "assistant", reply_text)
            vr.say(reply_text)
        except Exception as e:
            print(f"[ERROR] AI fallback failed: {e}")
            vr.say("I'm having trouble responding right now. Please call again.")

    # Always gather again unless the call is ending
    g = Gather(input="speech", action="/voice/outbound/process", method="POST", timeout=20, speech_timeout="auto")
    vr.append(g)
    return Response(str(vr), media_type="application/xml")