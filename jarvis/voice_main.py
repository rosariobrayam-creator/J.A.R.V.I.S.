"""J.A.R.V.I.S. voice mode — full pipeline with UI.

Run with:  python -m jarvis.voice_main

Wake word ("hey jarvis") -> record -> local Whisper -> brain -> British TTS,
with the neural-net UI reflecting every stage. Mute via button or spacebar.
"""

from __future__ import annotations

import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import game, history, personality, replay
from .config import (
    BACKEND,
    FOLLOWUP_SECONDS,
    MUTE_HOTKEY,
    STARTUP_CLIP,
    TIMING_LOG,
    VOICE_CLONE,
    VOICE_CLONE_PYTHON,
    WATCH_GLANCE_FRAMES,
    WATCH_GLANCE_SECONDS,
    WATCH_MODEL,
)

ROOT = Path(__file__).resolve().parents[1]


def _start_clone_server() -> None:
    """Launch the XTTS voice-clone server in its own venv if it isn't already
    up. It binds its port only once the model is loaded, so an open port means
    a previous instance (e.g. from before a Jarvis restart) is ready — reuse
    it and skip the multi-second model load."""
    if not VOICE_CLONE:
        return
    python = ROOT / VOICE_CLONE_PYTHON
    server = ROOT / "voiceclone_server.py"
    if not python.exists() or not server.exists():
        return  # clone stack not installed; Edge-TTS handles everything
    try:
        with socket.create_connection(("127.0.0.1", 8766), timeout=0.3):
            return  # already running
    except OSError:
        pass
    try:
        log = open(ROOT / "clone_server.log", "w", encoding="utf-8")
        subprocess.Popen(
            [str(python), str(server)],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass  # clone voice is best-effort; the fallback voice always works
from .info import InfoHub
from .ui.app import JarvisUI

STATE_LABELS = {
    "idle": 'STANDBY — SAY "HEY JARVIS"',
    "listening": "LISTENING",
    "thinking": "THINKING",
    "speaking": "SPEAKING",
    "muted": "MUTED",
}


def _log_timing(listener, first_word_at: float | None) -> None:
    """One line per turn: where the seconds between 'you stopped talking' and
    'Jarvis started talking' actually went. The game-mode target is 15s — the
    user is usually acting on a 25s timer."""
    if not TIMING_LOG or not listener.speech_ended_at or first_word_at is None:
        return
    heard = listener.speech_ended_at
    stt = listener.transcribed_at - heard
    total = first_word_at - heard
    print(
        f"[timing] stt {stt:4.1f}s | brain+speech {total - stt:4.1f}s | "
        f"first word {total:5.1f}s"
        f"{'  << over 15s' if total > 15 and game.is_game_mode() else ''}",
        flush=True,
    )


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
        _start_clone_server()  # loads in parallel; Edge-TTS covers until ready

        # Tracks the listener's raw state so the watch-mode gate can tell when
        # the user is mid-dictation — the talk lock isn't held yet while a
        # command is still being recorded, and a call-out barging in there is
        # Jarvis talking over the user's sentence.
        listener_state = {"value": "idle"}

        def _on_listener_state(s: str) -> None:
            listener_state["value"] = s
            ui.set_state(STATE_LABELS.get(s, s.upper()))

        listener = Listener(
            on_level=ui.set_mic_level,
            on_state=_on_listener_state,
        )
        # Stamped the moment a reply's first block reaches the speakers; read
        # (and cleared) per turn by answer() for the latency log.
        first_audio: dict[str, float] = {}
        speaker = Speaker(
            on_level=ui.set_speak_level,
            on_state=ui.set_speaking,
            on_first_audio=lambda: first_audio.setdefault("at", time.monotonic()),
        )
        # The instant acks are a fixed set of lines — synthesize them once now
        # so they never compete with a real reply for the GPU.
        from .audio.tts import prewarm

        prewarm(personality.cacheable_lines())

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
        # game_mode tool (MCP, same process) -> throttle the UI while gaming
        game.on_game_mode(ui.set_performance_mode)
        # ...and free the clone voice's ~2 GB of VRAM for the game (the model
        # parks in system RAM and borrows the GPU only while speaking)
        if VOICE_CLONE:
            from .audio.tts import set_clone_vram_mode

            game.on_game_mode(set_clone_vram_mode)
            set_clone_vram_mode(game.is_game_mode())  # sync a reused server

        # Global mute hotkey: works with the game focused or Jarvis minimized.
        # Key-state polling, not a keyboard hook — hooks go deaf while an
        # elevated window (game launcher, anti-cheat) has focus.
        if MUTE_HOTKEY:
            try:
                from .hotkey import watch_combo

                watch_combo(MUTE_HOTKEY, lambda: ui.set_muted(toggle_mute()))
            except Exception as e:
                ui.set_reply_text(f"[Mute hotkey '{MUTE_HOTKEY}' unavailable: {e}]")

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
        boot_clip = ROOT / STARTUP_CLIP if STARTUP_CLIP else None
        if boot_clip is not None and boot_clip.exists():
            try:
                speaker.play_file(str(boot_clip))
            except Exception:
                speaker.speak(personality.wake_line(hub.weather))
        else:
            speaker.speak(personality.wake_line(hub.weather))

        def _speak_watched(speak_fn) -> str | None:
            """Run a (blocking) speech call while watching the mic for
            "hey jarvis". If the user barges in, speech is cut off, their new
            command is recorded and returned; None means the speech played out
            undisturbed."""
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
                speak_fn()
            except Exception:
                pass  # audio out failure shouldn't kill the loop
            finally:
                done.set()
            watcher.join(timeout=2.0)
            if not barged_in.is_set():
                return None
            ui.set_state(STATE_LABELS["listening"])
            return listener.record_command()

        def speak_interruptible(text: str) -> str | None:
            return _speak_watched(lambda: speaker.speak(text))

        def answer(command: str, followup: bool = False) -> str | None:
            """One exchange with the mic hot the whole way through. The reply
            streams: speech starts on the first sentence out of the brain
            while the rest is still being generated and synthesized. Barge in
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
                deltas: queue.Queue = queue.Queue()
                text_started = threading.Event()  # first delta OR turn over

                # NB: everything a thread touches is pinned via default args —
                # these names are rebound on a barge-in retry, and an abandoned
                # thread must keep talking to ITS queue, not the new one.
                def _push(piece: str, dq=deltas, started=text_started) -> None:
                    dq.put(piece)
                    started.set()

                def _think(
                    q: str = question,
                    out: dict = outcome,
                    dq: queue.Queue = deltas,
                    started: threading.Event = text_started,
                    push=_push,
                ) -> None:
                    try:
                        if getattr(brain, "supports_streaming", False):
                            out["reply"] = brain.ask(q, on_delta=push)
                        else:
                            reply = brain.ask(q)
                            push(reply)
                            out["reply"] = reply
                    except Exception as e:
                        out["error"] = e
                    finally:
                        dq.put(None)  # end of stream
                        started.set()

                thinker = threading.Thread(target=_think, daemon=True, name="brain")
                thinker.start()
                if ack:
                    # Instant "Opening Spotify." while the brain works; plays
                    # once, in parallel with the request being processed.
                    ui.set_reply_text(ack)
                    try:
                        speaker.speak(ack)
                    except Exception:
                        pass
                    ack = None
                if not listener.wake_interrupt_monitor(text_started):
                    text_started.wait()  # monitor can exit early on mic errors
                    break  # text is flowing (or the turn already ended)

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

            # Speak while the brain keeps generating: pull deltas as they
            # arrive, mirror them into the UI, feed them to the speaker.
            spoken: list[str] = []

            def _delta_iter():
                while True:
                    piece = deltas.get()
                    if piece is None:
                        return
                    spoken.append(piece)
                    ui.set_reply_text("".join(spoken))
                    yield piece

            ui.set_state(STATE_LABELS["speaking"])
            first_audio.clear()  # measure the answer, not the ack covering it
            interrupt = _speak_watched(
                lambda: speaker.speak_stream(_delta_iter())
            )
            if interrupt is not None:
                # Barged in mid-reply: stop generating too, not just talking.
                getattr(brain, "cancel", lambda: None)()
            thinker.join(timeout=5.0)
            _log_timing(listener, first_audio.get("at"))

            reply = (outcome.get("reply") or "".join(spoken)).strip()
            if not reply and "error" in outcome:
                reply = "I ran into a problem with that one, sir."
                ui.set_reply_text(f"[{outcome.get('error')}]")
                ui.set_state(STATE_LABELS["speaking"])
                interrupt = speak_interruptible(reply)
            ui.set_reply_text(reply)
            ui.add_history(history.log("jarvis", reply))
            return interrupt

        # Held for the whole of a conversation; watch-mode call-outs grab it
        # non-blocking so Jarvis never talks over an exchange in progress.
        talk_lock = threading.Lock()

        # The gate: "PASS" is the watcher's stay-quiet reply. The prefix check
        # is case-sensitive so "Passing that truck, sir" survives — but a
        # WHOLE reply that is just the word pass in any casing ("Pass.") is
        # never a real call-out, so that form is caught case-insensitively.
        def _watch_pass(text: str) -> bool:
            head = text.lstrip()
            if head[:4] == "PASS" and (len(head) == 4 or not head[4].isalpha()):
                return True
            return head.strip(" .!?\n").lower() == "pass"

        _WATCH_GLANCE = (
            f"capture_screen with seconds={round(WATCH_GLANCE_SECONDS)} and "
            f"frames={WATCH_GLANCE_FRAMES}"
        )
        _REASON_LINES = {
            "settled": (
                "Something on screen just changed and settled — something "
                "finished happening."
            ),
            "spike": (
                "A sudden big change just hit the screen — an impact, kill, "
                "crash or result. React fast."
            ),
            "heartbeat": (
                "The action has been running for a while — check if the user "
                "is pulling off anything worth a comment."
            ),
        }

        def _watch_prompt(focus: str, reason: str) -> str:
            reason_line = _REASON_LINES.get(
                reason, "Something just happened on screen."
            )
            prompt = (
                "You are the live gameplay watcher for J.A.R.V.I.S. — a "
                "sharp, easygoing gaming buddy on the couch next to the "
                f"user, watching them play. {reason_line}\n\n"
                f"Call {_WATCH_GLANCE} and read the filmstrip as motion: the "
                "early frames are the build-up, the final frame is right "
                "now. Comment ONLY when you can point at a concrete moment "
                "in the frames — a clean overtake, a filthy shot, a crash, a "
                "near-miss, a health, score or objective swing, an obvious "
                "mistake worth a quick tip. If a voice line or radio callout "
                "would tell you more than the frames, you may also call "
                "hear_game_audio — silently, like every tool. Never write any "
                "words before or between tool calls; your entire text output "
                "comes after your final tool call. Then reply with the one or two "
                "short sentences Jarvis should say out loud right now, about "
                "THAT specific moment ('Clean overtake on that truck, sir', "
                "'That headshot was filthy'). Plain spoken English, no "
                "markdown, no narrating your own process, address the user "
                "as 'sir'. You have memory of this session: never repeat a "
                "line you've already used, don't re-describe what you "
                "already commented on, and call back to earlier moments when "
                "it fits ('still holding first place').\n\n"
                "If nothing notable happened — menus, loading or pause "
                "screens, routine uneventful play, or you'd just repeat "
                "yourself — reply with exactly: PASS"
            )
            if focus.strip():
                prompt += (
                    f"\n\nThe user gave a specific watch focus: {focus}\n"
                    "Follow it precisely — it overrides the general "
                    "commentary style. For a blackjack focus: state the "
                    "player's total and the dealer's upcard, then the "
                    "basic-strategy play, like 'Fourteen against a dealer "
                    "six — stand, sir.'"
                )
            return prompt

        def on_watch_alert(focus: str, reason: str) -> None:
            """Watch mode fired (spike / settled / heartbeat). Take a look and
            speak if the moment merits it — the reply streams straight into
            TTS so the reaction lands fast. Skips silently when muted or
            mid-conversation — the next trigger fires again."""
            # Skip while the user is dictating a command: the talk lock isn't
            # held yet during that recording, and a call-out landing there
            # talks straight over their sentence.
            if (
                listener.muted.is_set()
                or listener_state["value"] == "listening"
                or not talk_lock.acquire(blocking=False)
            ):
                return
            try:
                from .claude_code_brain import (
                    QUICK_TIMEOUT,
                    has_watch_session,
                    quick_look_stream,
                )

                if has_watch_session():
                    # The persona already lives in the resumed session; a
                    # short nudge keeps the prefill almost entirely cached.
                    prompt = (
                        f"[trigger: {reason}] Take another look — "
                        f"{_WATCH_GLANCE} — and comment on anything notable, "
                        "or reply exactly PASS. No words before or between "
                        "tool calls — text only after your last tool call."
                    )
                else:
                    prompt = _watch_prompt(focus, reason)

                # Only text written AFTER the model's last tool call is the
                # call-out; anything earlier is narration ("Let me check the
                # audio.") and must stay silent, as must any block that is or
                # ends with PASS. A block can't be judged final until the next
                # event, so completed blocks are held and a following tool_use
                # discards them; survivors flush when the stream ends, which
                # arrives within milliseconds of the last block — for a
                # one-or-two-sentence call-out that costs ~a second versus the
                # old speak-on-first-characters, and never speaks garbage.
                deltas: queue.Queue[str | None] = queue.Queue()
                pending: list[str] = []
                held: list[str] = []
                decided = threading.Event()
                verdict = {"speak": False}
                outcome: dict[str, str] = {}

                def on_delta(piece: str) -> None:
                    pending.append(piece)

                def on_event(kind: str) -> None:
                    if kind == "tool_use":
                        pending.clear()
                        held.clear()  # everything so far was pre-tool narration
                        return
                    if kind != "block_stop":
                        return
                    block = "".join(pending).strip()
                    pending.clear()
                    if not block or _watch_pass(block):
                        return
                    # a stray sign-off PASS tacked onto a real line
                    block = re.sub(r"\s*\bPASS\b[.!]?\s*$", "", block).strip()
                    if block:
                        held.append(block)

                def _think() -> None:
                    try:
                        outcome["reply"] = quick_look_stream(
                            prompt,
                            model=WATCH_MODEL,
                            on_delta=on_delta,
                            on_event=on_event,
                        )
                    except Exception:
                        held.clear()  # failed look = say nothing
                    finally:
                        if held:
                            verdict["speak"] = True
                            for block in held:
                                deltas.put(block + " ")
                        deltas.put(None)
                        decided.set()

                thinker = threading.Thread(
                    target=_think, daemon=True, name="watch-look"
                )
                thinker.start()
                decided.wait(timeout=QUICK_TIMEOUT + 10)
                if not verdict["speak"] or listener.muted.is_set():
                    thinker.join(timeout=QUICK_TIMEOUT)
                    return

                spoken: list[str] = []

                def _delta_iter():
                    while True:
                        piece = deltas.get()
                        if piece is None:
                            return
                        spoken.append(piece)
                        ui.set_reply_text("".join(spoken))
                        yield piece

                ui.set_state(STATE_LABELS["speaking"])
                try:
                    speaker.speak_stream(_delta_iter())
                except Exception:
                    pass
                thinker.join(timeout=5.0)
                # spoken already has narration and PASS filtered out; the raw
                # CLI result may not, so it's only the fallback.
                line = ("".join(spoken) or outcome.get("reply") or "").strip()
                if line:
                    ui.set_reply_text(line)
                    ui.add_history(history.log("jarvis", line))
                ui.set_state(
                    STATE_LABELS["muted" if listener.muted.is_set() else "idle"]
                )
            finally:
                talk_lock.release()

        if BACKEND == "claude-code":
            replay.set_alert_handler(on_watch_alert)

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
            with talk_lock:
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
                    command = interrupt or listener.wait_for_followup(
                        FOLLOWUP_SECONDS
                    )
                    followup = True

            ui.set_state(
                STATE_LABELS["muted" if listener.muted.is_set() else "idle"]
            )

    def on_ready() -> None:
        threading.Thread(target=worker, daemon=True, name="jarvis-pipeline").start()

    ui.run(on_ready)  # blocks until the window closes


if __name__ == "__main__":
    main()
