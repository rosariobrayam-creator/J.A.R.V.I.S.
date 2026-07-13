"""Central configuration for J.A.R.V.I.S."""

# "claude-code" = free, routes through your Claude Code subscription (no API key)
# "api"         = direct Anthropic API (needs ANTHROPIC_API_KEY, pay-per-use)
BACKEND = "claude-code"

MODEL = "claude-haiku-4-5"  # used by the "api" backend only
MAX_TOKENS = 64000  # streaming, so a generous ceiling is safe
USER_NAME = "sir"  # how Jarvis addresses you; change to your name if preferred

# --- Voice ------------------------------------------------------------------
# Edge-TTS's "Multilingual" voices are a newer generation and sound far more
# human, but none of them are British. Samples of each are in voice_samples/.
#   "en-US-AndrewMultilingualNeural"  most human, warm/confident (default)
#   "en-US-BrianMultilingualNeural"   most human, casual
#   "en-GB-RyanNeural"                British — the classic Jarvis accent
#   "en-GB-ThomasNeural"              British, a touch lighter
VOICE = "en-GB-ThomasNeural"
VOICE_RATE = "+4%"  # older-gen British voices sit best slightly brisk and low;
VOICE_PITCH = "-2Hz"  # for the multilingual voices use "+3%" / "+0Hz"

FOLLOWUP_SECONDS = 6.0  # how long Jarvis keeps listening after he replies
HISTORY_RETENTION_DAYS = 30  # transcript entries older than this are pruned at startup
