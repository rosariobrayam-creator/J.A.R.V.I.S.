"""J.A.R.V.I.S. voice mode — full pipeline with UI.

Run with:  python -m jarvis.voice_main

Wake word ("hey jarvis") -> record -> local Whisper -> brain -> British TTS,
with the neural-net UI reflecting every stage. Mute via button or spacebar.
"""

from __future__ import annotations

import sys
import threading
import time

from . import history, personality
from .config import BACKEND, FOLLOWUP_SECONDS
from .info import InfoHub
from .ui.app import JarvisUI

STATE_LABELS = {
    "idle": 'STANDBY — SAY "HEY JARVIS"',
    "listening": "LISTENING",
    "thinking": "THINKING",
    "speaking": "SPEAKING",
    "muted": "MUTED",
}


def _is_shutdown_command(command: str) -> bool:
    """Voice off-switch. "stand down" alone works; anything else must name
    Jarvis AND be short — so "shut down the PC" still reaches the
    power_control tool and "Jarvis, turn off the lights" stays a command."""
    c = command.lower().strip(" .,!?")
    if "stand down" in c or "go offline" in c:
        return True
    if "jarvis" not in c or len(c.replace(",", " ").split()) > 4:
        return False
    return any(
        p in c for p in ("power down", "shut down", "shut yourself down",
                         "goodnight", "good night", "turn off")
    )


def main() -> None:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ui = JarvisUI()

    def worker() -> None:
        from .audio.listener import Listener
        from .audio.tts import Speaker

        # Ambient panels (weather, inbox, system) refresh in the background
        # from here on; starting before the model load means the weather is
        # usually in by the time the greeting line is picked.
        hub = InfoHub(
            on_weather=ui.set_weather,
            on_emails=ui.set_emails,
            on_system=ui.set_system,
        )
        hub.start()
        history.prune()
        ui.set_history(history.recent(80))

        ui.set_state("LOADING MODELS")

        listener = Listener(
            on_level=ui.set_mic_level,
            on_state=lambda s: ui.set_state(STATE_LABELS.get(s, s.upper())),
        )
        speaker = Speaker(on_level=ui.set_speak_level, on_state=ui.set_speaking)

        def toggle_mute() -> bool:
            now_muted = not listener.muted.is_set()
            listener.set_muted(now_muted)
            if now_muted and speaker.is_speaking:
                speaker.stop()
            return now_muted

        def restart_now() -> None:
            from .tools import restart_jarvis

            ui.set_state("RESTARTING")
            speaker.stop()
            restart_jarvis(1)

        ui.api.on_mute_toggle = toggle_mute
        ui.api.on_restart = restart_now

        # Warm the heavy pieces before going live
        listener._get_whisper()

        if BACKEND == "claude-code":
            from .claude_code_brain import ClaudeCodeBrain
            from .mcp_server import start_in_background

            start_in_background()
            brain = ClaudeCodeBrain(on_text=lambda _t: None)
        else:
            from .brain import JarvisBrain

            brain = JarvisBrain(on_text=lambda _t: None)

        ui.set_state(STATE_LABELS["idle"])
        speaker.speak(personality.wake_line(hub.weather))

        def speak_interruptible(text: str) -> str | None:
            """Speak while watching the mic for "hey jarvis". If the user
            barges in, speech is cut off, their new command is recorded and
            returned; None means the reply played out undisturbed."""
            done = threading.Event()
            barged_in = threading.Event()

            def _watch() -> None:
                if listener.wake_interrupt_monitor(done):
                    barged_in.set()
                    speaker.stop()

            watcher = threading.Thread(
                target=_watch, daemon=True, name="wake-barge-in"
            )
            watcher.start()
            try:
                speaker.speak(text)
            except Exception:
                pass  # audio out failure shouldn't kill the loop
            finally:
                done.set()
            watcher.join(timeout=2.0)
            if not barged_in.is_set():
                return None
            ui.set_state(STATE_LABELS["listening"])
            return listener.record_command()

        def answer(command: str, followup: bool = False) -> str | None:
            """One exchange with the mic hot the whole way through. Barge in
            with "hey jarvis" while Jarvis is THINKING and the pending answer
            is abandoned — the addition/correction is folded into the question
            and asked again. Barge in while he's SPEAKING and the new command
            is returned to the conversation loop."""
            ui.set_user_text(command)
            ui.set_reply_text("")
            ui.add_history(history.log("user", command))
            ack = personality.quick_ack(command) or (
                personality.followup_ack() if followup else None
            )

            question = command
            while True:
                ui.set_state(STATE_LABELS["thinking"])
                outcome: dict = {}
                done = threading.Event()

                def _think(q: str = question) -> None:
                    try:
                        outcome["reply"] = brain.ask(q)
                    except Exception as e:
                        outcome["error"] = e
                    done.set()

                threading.Thread(target=_think, daemon=True, name="brain").start()
                if ack:
                    # Instant "Opening Spotify." while the brain works; plays
                    # once, in parallel with the request being processed.
                    ui.set_reply_text(ack)
                    try:
                        speaker.speak(ack)
                    except Exception:
                        pass
                    ack = None
                if not listener.wake_interrupt_monitor(done):
                    done.wait()  # monitor can exit early on mic errors
                    break  # reply (or error) is ready

                # Barge-in while thinking: kill the turn, take the addition.
                getattr(brain, "cancel", lambda: None)()
                ui.set_state(STATE_LABELS["listening"])
                extra = listener.record_command()
                if extra:
                    ui.set_user_text(extra)
                    ui.add_history(history.log("user", extra))
                    question = (
                        f"{question}\n\n[Before you could answer, the user "
                        f'interrupted to add or correct: "{extra}". Respond '
                        "to the updated request as one answer.]"
                    )
                # nothing heard -> just ask the original question again

            if "reply" in outcome:
                reply = outcome["reply"]
            else:
                reply = "I ran into a problem with that one, sir."
                ui.set_reply_text(f"[{outcome.get('error')}]")
            ui.set_reply_text(reply)
            ui.add_history(history.log("jarvis", reply))
            ui.set_state(STATE_LABELS["speaking"])
            return speak_interruptible(reply)

        while True:
            try:
                command = listener.wait_for_command()
            except Exception as e:
                # A dead pipeline thread looks like Jarvis going deaf with the
                # window still up — never die here, always retry.
                ui.set_reply_text(f"[Microphone error: {e} — retrying]")
                time.sleep(2.0)
                continue
            if command is None:
                if listener.muted.is_set():
                    continue  # mute interrupted a recording; go back around
                break  # shutdown

            # Conversation: keep exchanging until a follow-up window passes
            # in silence — then drop back to wake-word standby.
            followup = False
            while command:
                if _is_shutdown_command(command):
                    farewell = personality.farewell_line(hub.weather)
                    ui.set_user_text(command)
                    ui.set_reply_text(farewell)
                    history.log("user", command)
                    history.log("jarvis", farewell)
                    ui.set_state("GOING OFFLINE")
                    try:
                        speaker.speak(farewell)
                    except Exception:
                        pass
                    ui.close()
                    return
                interrupt = answer(command, followup=followup)
                if listener.muted.is_set():
                    break
                command = interrupt or listener.wait_for_followup(FOLLOWUP_SECONDS)
                followup = True

            ui.set_state(
                STATE_LABELS["muted" if listener.muted.is_set() else "idle"]
            )

    def on_ready() -> None:
        threading.Thread(target=worker, daemon=True, name="jarvis-pipeline").start()

    ui.run(on_ready)  # blocks until the window closes


if __name__ == "__main__":
    main()
