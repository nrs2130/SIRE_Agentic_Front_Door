"""Voice Gateway (L1) implementations.

The gateway's **only** outputs are (1) a normalized :class:`IntentEnvelope` per
utterance and (2) a :meth:`speak` channel the orchestrator uses to stream spoken
acknowledgments and progress updates. It contains **no** business logic — it does
not resolve people, place orders, or page anyone; it classifies urgency cheaply
and hands the envelope to L2.

Two implementations share :class:`VoiceGatewayBase`:

* :class:`TextStubGateway` — no mic, no network. Type an utterance; get an
  envelope. This is the CI/test path so the whole system runs without audio.
* :class:`VoiceLiveGateway` — wraps a real Azure Voice Live session
  (``gpt-realtime``), following the same SDK pattern as SIRE_demo's ``main.py``.
  Voice Live SDK: ``azure-ai-voicelive[aiohttp]==1.2.0`` (pinned in
  requirements.txt; GA per https://pypi.org/project/azure-ai-voicelive/).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

from .intent_envelope import IntentEnvelope, Urgency
from .urgency import ROUTE_INTENT_TOOL, classify, classify_urgency

if TYPE_CHECKING:  # avoid importing heavy config/Azure types at runtime import
    from config import AppConfig

logger = logging.getLogger("nightingale.gateway")

# System instructions for the routing model: classify, don't resolve.
ROUTING_INSTRUCTIONS = """\
You are Nightingale, the voice front door for hospital nurses.

For each nurse utterance:
1. Call the `route_intent` function EXACTLY ONCE, as soon as you understand the
   request, before doing anything else.
2. Return the canonical `intent`, the `urgency`, and any `entities` together in
   that single call. Decide urgency cheaply, right here — do not reason it out.
   Classify time-critical clinical events (sepsis, code blue, cardiac or
   respiratory arrest, fall, stroke, STEMI, hemorrhage, rapid response) as
   EMERGENCY; everything else is ROUTINE.
3. Do NOT resolve the request yourself, call other tools, or give medical orders.
   The orchestrator handles routing and will speak back to the nurse.
4. After calling `route_intent`, stay silent until the next user turn.
"""


class VoiceGatewayBase:
    """Shared contract for all gateways: emit envelopes, ``speak``, ``panic``.

    Envelopes are delivered through an :class:`asyncio.Queue`; the orchestrator
    consumes them via :meth:`envelopes`. Subclasses call :meth:`_emit` to publish.
    """

    def __init__(
        self,
        config: "AppConfig | None" = None,
        *,
        default_emergency_intent: str = "panic_button",
    ) -> None:
        self._config = config
        self._default_emergency_intent = default_emergency_intent
        self._queue: asyncio.Queue[IntentEnvelope | None] = asyncio.Queue()

    # -- envelope delivery ---------------------------------------------------

    def _emit(self, envelope: IntentEnvelope) -> None:
        """Publish an envelope to the orchestrator."""
        logger.info(
            "envelope emitted correlation_id=%s intent=%s urgency=%s",
            envelope.correlation_id,
            envelope.intent,
            envelope.urgency.value,
        )
        self._queue.put_nowait(envelope)

    async def envelopes(self) -> AsyncIterator[IntentEnvelope]:
        """Yield envelopes as they are produced, until :meth:`close` is called."""
        while True:
            envelope = await self._queue.get()
            if envelope is None:
                return
            yield envelope

    def close(self) -> None:
        """Signal end-of-stream to any :meth:`envelopes` consumer."""
        self._queue.put_nowait(None)

    # -- panic override ------------------------------------------------------

    async def panic(
        self,
        *,
        intent: str | None = None,
        entities: dict[str, str] | None = None,
    ) -> IntentEnvelope:
        """Force an EMERGENCY envelope, bypassing model classification.

        Maps the badge panic button (or a keyboard/UI stand-in) to a hard
        ``urgency=EMERGENCY`` override.
        """
        envelope = IntentEnvelope.create(
            intent=intent or self._default_emergency_intent,
            urgency=Urgency.EMERGENCY,
            entities=entities or {},
            utterance="[PANIC BUTTON]",
            spoken_ack_required=True,
        )
        logger.warning("PANIC override correlation_id=%s", envelope.correlation_id)
        self._emit(envelope)
        return envelope

    # -- spoken channel ------------------------------------------------------

    async def speak(self, text: str) -> None:
        """Speak ``text`` back to the nurse (acknowledgments / streamed updates)."""
        raise NotImplementedError


class TextStubGateway(VoiceGatewayBase):
    """Mic-less gateway: submit a typed utterance, get an :class:`IntentEnvelope`.

    Urgency/intent come from the deterministic :func:`classify` (emulating the
    realtime model's single ``route_intent`` call). Spoken output is captured in
    :attr:`spoken` so tests and the demo cockpit can assert on it.
    """

    def __init__(self, config: "AppConfig | None" = None, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.spoken: list[str] = []

    async def submit(self, utterance: str) -> IntentEnvelope:
        """Classify a typed utterance and emit its envelope."""
        intent, urgency, entities = classify(utterance)
        envelope = IntentEnvelope.create(intent, urgency, entities, utterance)
        self._emit(envelope)
        return envelope

    async def speak(self, text: str) -> None:
        self.spoken.append(text)
        logger.info("speak (stub): %s", text)

    async def run_repl(self) -> None:  # pragma: no cover - interactive helper
        """Simple REPL for manual demos. Type ``!panic`` to force EMERGENCY."""
        loop = asyncio.get_event_loop()
        print("Nightingale text gateway. Type an utterance, '!panic', or 'quit'.")
        while True:
            line = (await loop.run_in_executor(None, input, "> ")).strip()
            if line.lower() in {"quit", "exit"}:
                self.close()
                return
            if line == "!panic":
                env = await self.panic()
            elif line:
                env = await self.submit(line)
            else:
                continue
            print(f"  → {env.to_dict()}")


class VoiceLiveGateway(VoiceGatewayBase):
    """Wraps a live Azure Voice Live session; emits envelopes from ``route_intent``.

    Follows the same SDK usage as SIRE_demo's ``main.py`` (the working Voice Live
    loop is reused, not rewritten). Heavy dependencies (``azure-ai-voicelive``,
    ``pyaudio``, and ``main.AudioProcessor``) are imported lazily so this module
    stays importable in CI without a mic or the SDK installed.
    """

    def __init__(self, config: "AppConfig", credential: Any) -> None:
        super().__init__(config)
        if config is None:  # defensive: VoiceLive needs real config
            raise ValueError("VoiceLiveGateway requires an AppConfig")
        self._credential = credential
        self._conn: Any = None
        self._audio: Any = None
        self._active_response = False
        self._response_done = False
        self._last_utterance = ""
        # Serialize spoken talk-back so successive updates don't render overlapping
        # Voice Live responses (which sound like two voices talking over each other).
        self._speak_lock = asyncio.Lock()
        self._response_complete = asyncio.Event()
        self._response_complete.set()
        # Function-call argument accumulation for route_intent.
        self._fn_call_id: str | None = None
        self._fn_call_name: str | None = None
        self._fn_call_args = ""

    async def run(self) -> None:
        """Connect to Voice Live, configure the routing session, and pump events."""
        # Lazy imports keep the module CI-safe (no SDK/mic needed to import).
        from azure.ai.voicelive.aio import connect  # noqa: PLC0415
        from main import AudioProcessor  # noqa: PLC0415  (reuse the working audio loop)

        vc = self._config.voicelive  # type: ignore[union-attr]
        logger.info("connecting to Voice Live endpoint=%s model=%s", vc.endpoint, vc.model)
        try:
            async with connect(
                endpoint=vc.endpoint,
                credential=self._credential,
                model=vc.model,
            ) as conn:
                self._conn = conn
                self._audio = AudioProcessor(conn)
                await self._configure_session()
                self._audio.start_playback()
                async for event in conn:
                    await self._on_event(event)
        finally:
            if self._audio:
                self._audio.shutdown()
            self.close()

    async def _configure_session(self) -> None:
        from azure.ai.voicelive.models import (  # noqa: PLC0415
            AudioEchoCancellation,
            AudioNoiseReduction,
            AzureStandardVoice,
            InputAudioFormat,
            Modality,
            OutputAudioFormat,
            RequestSession,
            ServerVad,
        )

        vc = self._config.voicelive  # type: ignore[union-attr]
        voice_cfg: Any = AzureStandardVoice(name=vc.voice) if "-" in vc.voice else vc.voice
        session = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=ROUTING_INSTRUCTIONS,
            voice=voice_cfg,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=ServerVad(
                threshold=0.5, prefix_padding_ms=300, silence_duration_ms=800
            ),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(
                type="azure_deep_noise_suppression"
            ),
            tools=[ROUTE_INTENT_TOOL],
        )
        await self._conn.session.update(session=session)
        logger.info("Voice Live session configured with route_intent tool")

    async def _on_event(self, event: Any) -> None:
        from azure.ai.voicelive.models import (  # noqa: PLC0415
            FunctionCallOutputItem,
            ServerEventType,
        )

        etype = event.type
        audio, conn = self._audio, self._conn

        if etype == ServerEventType.SESSION_UPDATED:
            audio.start_capture()
            logger.info("Voice Live session ready; listening")

        # Barge-in: the nurse can interrupt a spoken update at any time.
        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            audio.skip()
            if self._active_response and not self._response_done:
                try:
                    await conn.response.cancel()
                except Exception:  # pragma: no cover - benign "no active response"
                    pass
            # Unblock any speak() waiting on the (now-canceled) response.
            self._active_response = False
            self._response_complete.set()

        elif etype == ServerEventType.RESPONSE_CREATED:
            self._active_response, self._response_done = True, False
            self._response_complete.clear()
        elif etype == ServerEventType.RESPONSE_AUDIO_DELTA:
            audio.enqueue(event.delta)
        elif etype == ServerEventType.RESPONSE_DONE:
            self._active_response, self._response_done = False, True
            self._response_complete.set()

        # Capture the verbatim utterance for the envelope.
        elif str(etype) == "conversation.item.input_audio_transcription.completed":
            self._last_utterance = getattr(event, "transcript", "") or ""

        # route_intent function call: accumulate args, then build the envelope.
        elif etype == ServerEventType.CONVERSATION_ITEM_CREATED:
            item = getattr(event, "item", None)
            if item and getattr(item, "type", None) == "function_call":
                self._fn_call_id = getattr(item, "call_id", None)
                self._fn_call_name = getattr(item, "name", None)
                self._fn_call_args = ""
        elif str(etype) == "response.function_call_arguments.delta":
            self._fn_call_args += getattr(event, "delta", "")
        elif str(etype) == "response.function_call_arguments.done":
            call_id = getattr(event, "call_id", self._fn_call_id)
            name = getattr(event, "name", self._fn_call_name)
            args_str = getattr(event, "arguments", self._fn_call_args)
            if name == "route_intent":
                envelope = self._build_envelope(args_str)
                self._emit(envelope)
                # Ack the tool call so the model doesn't hang. We deliberately do
                # NOT call response.create() — the orchestrator owns spoken output.
                await conn.conversation.item.create(
                    item=FunctionCallOutputItem(
                        call_id=call_id,
                        output=json.dumps(
                            {"status": "accepted", "correlation_id": envelope.correlation_id}
                        ),
                    )
                )
            self._fn_call_id = self._fn_call_name = None
            self._fn_call_args = ""

        elif etype == ServerEventType.ERROR:
            logger.error("Voice Live error: %s", getattr(event.error, "message", event))

    def _build_envelope(self, args_str: str) -> IntentEnvelope:
        """Build an :class:`IntentEnvelope` from a ``route_intent`` tool call."""
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}
        utterance = args.get("utterance") or self._last_utterance
        # Defensive: if the model omitted urgency, decide it cheaply here.
        urgency_raw = args.get("urgency")
        try:
            urgency = Urgency(urgency_raw)
        except ValueError:
            urgency = classify_urgency(utterance)
        return IntentEnvelope.create(
            intent=args.get("intent") or "general_request",
            urgency=urgency,
            entities=args.get("entities") or {},
            utterance=utterance,
        )

    async def speak(self, text: str) -> None:
        """Speak ``text`` verbatim via the Voice Live session.

        Inserts an assistant message and asks the service to render it to audio.
        Barge-in still works because a new user turn cancels the active response
        in :meth:`_on_event`.

        Serialized: each call waits for the previous spoken response to finish before
        starting the next, so streamed updates never render overlapping audio (which
        sounds like two voices). A soft timeout keeps a stuck response from blocking.
        """
        conn = self._conn
        if conn is None:
            logger.warning("speak() called before the Voice Live session connected")
            return
        async with self._speak_lock:
            # Wait for any in-flight response to finish rendering before the next.
            try:
                await asyncio.wait_for(self._response_complete.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("prior spoken response did not complete in time; proceeding")
            try:
                # OutputTextContentPart is part of azure-ai-voicelive 1.2.0; the
                # assistant message item shape is guarded in case the symbol moves.
                from azure.ai.voicelive.models import (  # noqa: PLC0415
                    AssistantMessageItem,
                    OutputTextContentPart,
                )

                self._response_complete.clear()
                await conn.conversation.item.create(
                    item=AssistantMessageItem(content=[OutputTextContentPart(text=text)])
                )
                await conn.response.create()
            except Exception:  # pragma: no cover - depends on the live SDK/runtime
                # TODO: verify assistant-message API against
                # https://learn.microsoft.com/azure/ai-services/speech-service/voice-live-how-to
                self._response_complete.set()  # don't deadlock the next speak()
                logger.exception("speak() failed; verify Voice Live assistant-message API")
