from fastapi import FastAPI, Response, Request
from twilio.twiml.voice_response import VoiceResponse, Gather
from openai import OpenAI
import os, time, requests, re

app = FastAPI()

# API keys
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
GOOGLE_API_KEY = os.environ.get("GOOGLE_MAPS_KEY")

# Store details
STORE_INFO = {
    "name": "CPR Cell Phone Repair",
    "city": "Myrtle Beach",
    "address": "1000 South Commons Drive, Myrtle Beach, SC 29588",  # update with real address
    "hours": "Monday–Saturday 9am–6pm, Sunday we are closed",
    "phone": "(843) 750-0449"
}

# Per-call session data
call_activity = {}   # last interaction timestamp
call_mode = {}       # "normal", "gps", "awaiting_origin"
gps_routes = {}      # remaining GPS steps
call_memory = {}     # short-term conversation memory
caller_name = {}     # caller's name per call

MAX_MEMORY = 5

# === Helper functions ===

def slow_say(self, text, **kwargs):
    kwargs.setdefault("voice", "Polly.Matthew")
    self._original_say(
        f'<speak><prosody rate="90%">{text}</prosody></speak>',
        **kwargs
    )

# Monkey‑patch VoiceResponse.say to always use slow mode
VoiceResponse._original_say = VoiceResponse.say
VoiceResponse.say = slow_say

def remember(call_sid, role, content):
    """Store a message in short-term memory for this call."""
    if call_sid not in call_memory:
        call_memory[call_sid] = []
    call_memory[call_sid].append({"role": role, "content": content})
    call_memory[call_sid] = call_memory[call_sid][-MAX_MEMORY:]

def geocode_address(address):
    """Convert spoken address/landmark into lat,lng string, biased to Myrtle Beach."""
    print(f"[DEBUG] Geocoding request for: '{address}'")

    # Try Geocoding API first
    geo_url = "https://maps.googleapis.com/maps/api/geocode/json"
    geo_params = {
        "address": address,
        "components": "locality:Myrtle Beach|administrative_area:SC|country:US",
        "key": GOOGLE_API_KEY
    }
    r = requests.get(geo_url, params=geo_params)
    print(f"[DEBUG] Geocoding URL: {r.url}")
    geo_data = r.json()
    print(f"[DEBUG] Geocoding response: {geo_data}")

    if geo_data.get("status") == "OK" and geo_data.get("results"):
        loc = geo_data["results"][0]["geometry"]["location"]
        coords = f"{loc['lat']},{loc['lng']}"
        print(f"[DEBUG] Geocoding success: {coords}")
        return coords

    # Fallback to Places API text search
    print("[DEBUG] Falling back to Places API")
    places_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    places_params = {
        "query": f"{address}, Myrtle Beach, SC",
        "key": GOOGLE_API_KEY
    }
    r = requests.get(places_url, params=places_params)
    print(f"[DEBUG] Places URL: {r.url}")
    places_data = r.json()
    print(f"[DEBUG] Places response: {places_data}")

    if places_data.get("status") == "OK" and places_data.get("results"):
        loc = places_data["results"][0]["geometry"]["location"]
        coords = f"{loc['lat']},{loc['lng']}"
        print(f"[DEBUG] Places success: {coords}")
        return coords

    print("[DEBUG] No coordinates found from either Geocoding or Places.")
    return None

def get_directions(origin, destination):
    """Fetch directions from Google Directions API."""
    print(f"[DEBUG] Directions request: origin={origin}, destination={destination}")

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": origin,
        "destination": destination,
        "mode": "driving",
        "region": "us",
        "key": GOOGLE_API_KEY
    }
    r = requests.get(url, params=params)
    print(f"[DEBUG] Directions URL: {r.url}")
    directions_data = r.json()
    print(f"[DEBUG] Directions response: {directions_data}")

    if directions_data.get("status") == "OK" and directions_data.get("routes"):
        leg = directions_data["routes"][0]["legs"][0]
        steps = [re.sub(r"<[^>]*>", "", s["html_instructions"]) for s in leg["steps"]]
        print(f"[DEBUG] Directions success: {len(steps)} steps")
        return {
            "duration": leg["duration"]["text"],
            "distance": leg["distance"]["text"],
            "steps": steps
        }

    print(f"[DEBUG] Directions API returned no usable route. Status: {directions_data.get('status')}")
    return None

@app.post("/voice/outbound/intro")
async def intro(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    call_activity[call_sid] = time.time()
    call_mode[call_sid] = "normal"  # start immediately in normal mode
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
    user_input = (form.get("SpeechResult") or "").strip()
    lower_input = user_input.lower()
    vr = VoiceResponse()
    call_activity[call_sid] = time.time()

    # Exit phrases
    exit_phrases = [
        "thank you, bye", "thank you bye", "goodbye", "bye", "that's all", "hang up"
    ]
    if any(phrase in lower_input for phrase in exit_phrases):
        vr.say(f"Thank you for calling {STORE_INFO['name']}. Goodbye.")
        vr.hangup()
        return Response(str(vr), media_type="application/xml")

    # GPS mode step delivery
    if call_mode.get(call_sid) == "gps":
        steps = gps_routes.get(call_sid, [])
        if steps:
            vr.say(steps.pop(0))
            gps_routes[call_sid] = steps
            if steps:
                gather = Gather(
                    input="speech",
                    action="/voice/outbound/process",
                    method="POST",
                    timeout=20,
                    speech_timeout="auto"
                )
                vr.append(gather)
            else:
                vr.say("You have arrived at your destination. Goodbye.")
                vr.hangup()
            return Response(str(vr), media_type="application/xml")
        else:
            vr.say("No more directions available. Goodbye.")
            vr.hangup()
            return Response(str(vr), media_type="application/xml")

    # Awaiting origin for GPS
    if call_mode.get(call_sid) == "awaiting_origin":
        origin_coords = geocode_address(user_input)
        if not origin_coords:
            vr.say("I couldn't find that location. Could you try giving me a street address or another well-known place nearby?")
            gather = Gather(
                input="speech",
                action="/voice/outbound/process",
                method="POST",
                timeout=20,
                speech_timeout="auto"
            )
            vr.append(gather)
            return Response(str(vr), media_type="application/xml")

        directions = get_directions(origin_coords, STORE_INFO["address"])
        if directions:
            vr.say(f"It is {directions['distance']} away, about {directions['duration']} drive. Let's start directions.")
            gps_routes[call_sid] = directions["steps"]
            call_mode[call_sid] = "gps"
            vr.say(gps_routes[call_sid].pop(0))
            gather = Gather(
                input="speech",
                action="/voice/outbound/process",
                method="POST",
                timeout=20,
                speech_timeout="auto"
            )
            vr.append(gather)
        else:
            vr.say("I couldn't get directions from that location. Could you try a different starting point?")
            gather = Gather(
                input="speech",
                action="/voice/outbound/process",
                method="POST",
                timeout=20,
                speech_timeout="auto"
            )
            vr.append(gather)
        return Response(str(vr), media_type="application/xml")

    # === Hard-coded store info (order adjusted) ===
    if "directions" in lower_input or "how do i get there" in lower_input:
        vr.say("Sure, what is your starting address or location?")
        call_mode[call_sid] = "awaiting_origin"
        gather = Gather(
            input="speech",
            action="/voice/outbound/process",
            method="POST",
            timeout=20,
            speech_timeout="auto"
        )
        vr.append(gather)
        return Response(str(vr), media_type="application/xml")

    elif "hours" in lower_input:
        vr.say(f"Our hours are {STORE_INFO['hours']}.")

    elif "address" in lower_input or "location" in lower_input:
        vr.say(f"We are located at {STORE_INFO['address']}.")

    elif re.search(r"\b(phone|number)\b", lower_input):
        vr.say(f"Our phone number is {STORE_INFO['phone']}.")

    elif "landmark" in lower_input or "nearby" in lower_input or "close to" in lower_input:
        vr.say(
            "We are near Goodwill and Lowes Home Improvement, "
            "in the strip mall where Chipotle, McCalisters, Sports Clips, "
            "and the UPS Store are all at. If that doesn't help, "
            "the only other big landmark is that we are not far down the road "
            "from the East Coast Honda Dealership."
        )

    # === Fallback to AI for other questions ===
    else:
        try:
            system_prompt = f"""
            You are a warm, knowledgeable receptionist for {STORE_INFO['name']} in {STORE_INFO['city']}.
            You can chat naturally, answer open-ended repair or product questions, but DO NOT guess store details like hours, address, phone, or landmarks.
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
        except:
            vr.say("I'm having trouble responding right now. Please call again.")

    # Listen again
    gather = Gather(
        input="speech",
        action="/voice/outbound/process",
        method="POST",
        timeout=20,
        speech_timeout="auto"
    )
    vr.append(gather)

    return Response(str(vr), media_type="application/xml")