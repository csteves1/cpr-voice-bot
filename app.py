from fastapi import FastAPI, Response, Request
from twilio.twiml.voice_response import VoiceResponse, Gather
from openai import OpenAI
import os

# --- Set up OpenAI client using your environment variable ---
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

app = FastAPI()

# --- Central system prompt ---
system_prompt = (
    "You are an AI phone receptionist for CPR Cell Phone Repair in Myrtle Beach. "
    "Answer concisely in a friendly, professional tone. "
    "Business details: "
    "Hours: 9 AM–6 PM weekdays, closed on Sunday. "
    "Location: 1000 South Commons Drive, Myrtle Beach, south Carolina 29588. Highway 17 Business near Surfside. "
    "Pricing: Screen repairs range from $99.99-499.99 depending on model. "
    "Turnaround: Same day, often within 1-2 hours."
)

# --- AI reply function that calls the model ---
def ai_reply(user_text: str) -> str:
    if not user_text:
        return "Sorry, I didn't catch that. Could you repeat your question?"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",  # small + fast model for voice calls
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ],
            max_tokens=80
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("OpenAI error:", e)
        return "I'm having trouble accessing information right now. Could you ask again?"

def should_end(resp_text: str) -> bool:
    return resp_text.lower().startswith("thanks") or "goodbye" in resp_text.lower()

def strip_end(resp_text: str) -> str:
    return resp_text.strip()

# --- 1) Intro: greet + listen for speech ---
@app.post("/voice/outbound/intro")
async def voice_intro():
    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/voice/outbound/process",
        method="POST",
        timeout=2,
        speech_timeout="auto"
    )
    gather.say("Hi, this is Chris from CPR Myrtle Beach. How can I help you today?")
    vr.append(gather)

    # If no speech detected, re‑prompt
    vr.redirect("/voice/outbound/intro")
    return Response(content=str(vr), media_type="application/xml")

# --- 2) Process: handle speech, reply, and loop ---
@app.post("/voice/outbound/process")
async def voice_process(request: Request):
    form = await request.form()
    said = form.get("SpeechResult", "")

    bot = ai_reply(said)

    vr = VoiceResponse()
    if should_end(said):
        vr.say(strip_end(bot))
        vr.hangup()
    else:
        # Speak first, then start listening
        vr.say(bot)

        gather = Gather(
            input="speech",
            action="/voice/outbound/process",
            method="POST",
            timeout=2,  # shorter window to reduce noise pickup
            speech_timeout="auto"
        )
        gather.say("What else can I help you with?")
        vr.append(gather)

        vr.pause(length=1)  # optional: slight delay before looping
        vr.redirect("/voice/outbound/intro")  # safety net on silence

    return Response(content=str(vr), media_type="application/xml")