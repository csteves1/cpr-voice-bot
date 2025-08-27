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
    "address": "1000 South Commons Drive Unit 103, Myrtle Beach, SC 29577",  # update with real address
    "hours": "Mon–Sat 9am–6pm, Sun we are closed",
    "phone": "(843) 750-0449"
}

# Per-call session data
call_activity = {}   # last interaction timestamp
call_mode = {}       # "normal", "gps", "awaiting_origin"
gps_routes = {}      # remaining GPS steps
call_memory = {}     # short-term conversation memory
caller_name = {}     # caller's name per call

MAX_MEMORY = 5

def remember(call_sid, role, content):
    """Store a message in short-term memory for this call."""
    if call_sid not in call_memory:
        call_memory[call_sid] = []
    call_memory[call_sid].append({"role": role, "content": content})
    call_memory[call_sid] = call_memory[call_sid][-MAX_MEMORY:]

def get_directions(origin, destination):
    """Fetch directions from Google Directions API."""
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": origin,
        "destination": destination,
        "key": GOOGLE_API_KEY
    }
    r = requests.get(url, params=params).json()
    if r.get("status") == "OK":
        leg = r["routes"][0]["legs"][0]
        steps = [re.sub(r"<[^>]*>", "", s["html_instructions"]) for s in leg["steps"]]
        return {
            "duration": leg["duration"]["text"],
            "distance": leg["distance"]["text"],
            "steps": steps
        }
    return None

@app.post("/voice/outbound/intro")
async def intro(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    call_activity[call_sid] = time.time()
    call_mode[call_sid] = "normal"
    call_memory[call_sid] = []
    caller_name[call_sid] = None

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/voice/outbound/process",
        method="POST",
        timeout=20,
        speech_timeout="auto"
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
    exit_phrases = ["thank you, bye", "thank you bye", "goodbye", "bye", "that's all", "hang up"]
    if any(phrase in lower_input for phrase in exit_phrases):
        vr.say(f"Thank you for calling {STORE_INFO['name']}. Goodbye.")
        vr.hangup()
        return Response(str(vr), media_type="application/xml")

    # Name detection
    name_match = re.search(r"my name is (\w+)", lower_input)
    if name_match:
        caller_name[call_sid] = name_match.group(1).capitalize()
        vr.say(f"Nice to meet you, {caller_name[call_sid]}.")

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
        origin_addr = user_input
        directions = get_directions(origin_addr, STORE_INFO["address"])
        if directions:
            name_part = f"{caller_name[call_sid]}, " if caller_name.get(call_sid) else ""
            vr.say(f"{name_part}It is {directions['distance']} away, about {directions['duration']} drive. Let's start directions.")
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
            vr.say("Sorry, I couldn't get directions from that location.")
        return Response(str(vr), media_type="application/xml")

    # Store info quick replies
    if "hours" in lower_input:
        vr.say(f"Our hours are {STORE_INFO['hours']}.")
    elif "address" in lower_input or "location" in lower_input:
        vr.say(f"We are located at {STORE_INFO['address']}.")
    elif "phone" in lower_input or "number" in lower_input:
        vr.say(f"Our phone number is {STORE_INFO['phone']}.")
    elif "directions" in lower_input or "how do i get there" in lower_input:
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
    else:
        # General AI answer
        try:
            system_prompt = f"""
            You are a warm, knowledgeable receptionist for {STORE_INFO['name']} in {STORE_INFO['city']}.
            You can chat naturally, give store info, repair advice, or directions.
            Always be friendly and concise, but add detail if asked.
            """
            if caller_name.get(call_sid):
                system_prompt += f" Address the caller by their name: {caller_name[call_sid]}."

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