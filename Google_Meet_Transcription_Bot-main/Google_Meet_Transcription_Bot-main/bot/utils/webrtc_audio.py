"""
WebRTC in-browser audio capture — deployment-safe, no system audio device needed.

Architecture:
  1. RTCPeerConnection interceptor (injected via add_init_script) tracks all
     peer connections Meet creates.
  2. MediaRecorder cycles: start() → stop() every CHUNK_SEC seconds, producing
     ONE complete self-contained webm file per cycle (avoids EBML header issues).
  3. Encoded audio chunks (base64 webm/opus) are queued in window.__audioChunks.
  4. WebRTCAudioCapturer.chunks() drains the buffer every CHUNK_SEC seconds
     and yields (participant_name, wav_bytes) tuples.

Speaker attribution: each RTCPeerConnection receiver maps to one participant.
Meet stores participant display names in the DOM — we correlate by data-ssrc
or by order of track appearance vs participant list.
"""

import asyncio
import base64
import logging
import os
from typing import AsyncIterator, Optional

logger = logging.getLogger("bot")

CHUNK_SEC = 15         # seconds per chunk for mixed-audio fallback recorder
SAMPLE_RATE = 16_000   # Sarvam expects 16 kHz
_CHUNK_MS = CHUNK_SEC * 1000
_DRAIN_SEC = 3         # Python polls JS __audioChunks every 3s (VAD cuts chunks earlier)

# Google Meet internal processing tracks — these carry no real speech audio.
# They appear as participant names in WebRTC tracks but are virtual pipelines
# (background blur, face detection, etc.).  Sending them to STT wastes API
# calls and produces empty/noise transcripts.
_INTERNAL_TRACK_NAMES: frozenset = frozenset({
    "visual_effects",
    "frame_person",
    "keep_outline",
    "audio_effects",
})

# Bot's own display name — read once at import time from env.
# The bot's own mic/camera tracks appear with this label in the WebRTC stream.
# We must skip them: the bot has no real speech to transcribe (mic is muted).
_BOT_DISPLAY_NAME: str = os.getenv("BOT_DISPLAY_NAME", "oneClarity AI").lower()


# ── JavaScript injected via context.add_init_script() ────────────────────────
# Must run BEFORE Meet's own JS so RTCPeerConnection is intercepted first.
#
# Key design: MediaRecorder uses stop()+start() cycling instead of timeslice.
# Each cycle produces ONE complete webm file with valid EBML headers that
# ffmpeg can decode. The timeslice approach (start(intervalMs)) only puts EBML
# headers in the first chunk — all subsequent chunks fail ffmpeg decoding.

WEBRTC_INTERCEPTOR_JS = r"""
(function() {
    if (window.__webrtcInterceptorActive) return;
    window.__webrtcInterceptorActive = true;

    // Buffer: [{trackId, participantName, b64, ts}]
    window.__audioChunks   = [];
    window.__peerConns     = [];   // all RTCPeerConnection instances
    window.__trackRecorders = {};  // trackId → {recorder, participantName, stream, mimeType, track, pc}

    // ── 1. Intercept RTCPeerConnection ────────────────────────────────────────
    const _OrigPC = window.RTCPeerConnection;
    window.RTCPeerConnection = function(...args) {
        const pc = new _OrigPC(...args);
        window.__peerConns.push(pc);

        pc.addEventListener('track', function(ev) {
            const track = ev.track;
            if (track.kind !== 'audio') return;
            // Delay: DOM participant list may not have rendered yet
            setTimeout(() => _startTrackRecorder(track, pc), 1500);
        });

        return pc;
    };
    Object.assign(window.RTCPeerConnection, _OrigPC);
    window.RTCPeerConnection.prototype = _OrigPC.prototype;

    // ── 2. Start recording for a new track ───────────────────────────────────
    function _startTrackRecorder(track, pc) {
        const trackId = track.id;
        if (window.__trackRecorders[trackId]) return; // already recording

        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus'
            : 'audio/webm';

        const stream = new MediaStream([track]);

        // Start as 'Unknown' — DO NOT resolve name at track creation.
        // At creation time the DOM only shows the meeting title (e.g. "testing",
        // "Shubham / Vaibhav"), not participant tiles. Name is re-resolved on
        // every dataavailable event once the meeting UI is fully loaded.
        window.__trackRecorders[trackId] = {
            recorder: null,
            participantName: 'Unknown',
            stream, mimeType, track, pc,
            active: true
        };

        _cycleRecorder(trackId);
        console.log('[WebRTC capture] Started cycling recorder for track', trackId,
                    '(name will resolve once meeting loads)');
    }

    // ── 3. Cycling recorder: stop()+start() produces complete webm files ──────
    // Each stop() finalizes the webm container — ffmpeg can decode each file
    // independently. The timeslice approach puts EBML headers only in chunk #1.
    function _cycleRecorder(trackId) {
        const info = window.__trackRecorders[trackId];
        if (!info || !info.active) return;

        const { stream, mimeType, track, pc } = info;

        let recorder;
        try {
            recorder = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 64000 });
        } catch(e) {
            console.warn('[WebRTC capture] MediaRecorder failed for cycle', trackId, e);
            return;
        }

        // Poll active speaker every 3s during this recording window.
        // Google Meet's SFU reuses 3 track slots as speakers change — the name
        // on a track must be refreshed within each chunk, not just at chunk start.
        var _namePoller = setInterval(function() {
            var _polledName = _resolveParticipantName(track, pc);
            if (_polledName && _polledName !== 'Unknown') {
                info.participantName = _polledName;
            }
        }, 3000);

        recorder.addEventListener('dataavailable', function(ev) {
            clearInterval(_namePoller);
            if (!ev.data || ev.data.size < 512) return;
            const reader = new FileReader();
            reader.onloadend = function() {
                const b64 = reader.result.split(',')[1];
                // Final resolve at chunk boundary — captures speaker at end of window
                const name = _resolveParticipantName(track, pc);
                if (name !== 'Unknown') info.participantName = name;
                // Measured speaker identity: the RTP source (SSRC/CSRC) on this
                // receiver right now. Meet keeps a stable source id per participant,
                // so this is the TRUE speaker — far better than guessing from
                // captions. audioLevel confirms they actually produced sound.
                var _ssrc = null, _csrc = null, _lvl = 0;
                try {
                    var _recv = pc.getReceivers().find(function(r){ return r.track === track; });
                    if (_recv) {
                        var _ss = (_recv.getSynchronizationSources && _recv.getSynchronizationSources()) || [];
                        if (_ss.length) {
                            _ssrc = _ss[0].source;
                            if (typeof _ss[0].audioLevel === 'number') _lvl = _ss[0].audioLevel;
                        }
                        var _cs = (_recv.getContributingSources && _recv.getContributingSources()) || [];
                        if (_cs.length) _csrc = _cs[0].source;
                    }
                } catch(_e) {}
                window.__audioChunks.push({
                    trackId: trackId,
                    participantName: info.participantName,
                    b64: b64,
                    ts: Date.now(),
                    ssrc: _ssrc,
                    csrc: _csrc,
                    level: _lvl
                });
                // Keep buffer bounded (200 max, trim to 120)
                if (window.__audioChunks.length > 200) {
                    window.__audioChunks = window.__audioChunks.slice(-120);
                }
            };
            reader.readAsDataURL(ev.data);
        });

        // When stop() fires dataavailable then 'stop' event — restart the cycle
        recorder.addEventListener('stop', function() {
            clearInterval(_namePoller);
            // Clean up VAD poller and cap from this cycle to prevent leaks
            _vadStopped = true;
            clearInterval(_vadPoller);
            clearTimeout(_vadCap);
            if (window.__trackRecorders[trackId] && window.__trackRecorders[trackId].active) {
                setTimeout(() => _cycleRecorder(trackId), 50);
            }
        });

        recorder.start();  // No timeslice — records until stop() is called
        info.recorder = recorder;

        // VAD-based stop: cut the chunk when the speaker finishes or changes.
        // Triggers on:
        //   1. Sustained silence (audioLevel < 0.01 for 1s) = utterance ended
        //   2. SSRC change on this track = different speaker took over the SFU slot
        // Falls back to a 30s hard cap so long continuous utterances are still flushed.
        var _vadStopped = false;
        var _silenceStart = null;
        var _learnedSSRC = null;
        var _vadPoller, _vadCap;

        function _vadStop(reason) {
            if (_vadStopped) return;
            _vadStopped = true;
            clearInterval(_vadPoller);
            clearTimeout(_vadCap);
            if (recorder.state === 'recording') {
                console.log('[WebRTC capture] VAD stop (' + reason + ') track=' + trackId.slice(0, 8));
                recorder.stop();
            }
        }

        _vadPoller = setInterval(function() {
            if (_vadStopped) { clearInterval(_vadPoller); return; }
            try {
                var _recv = pc.getReceivers().find(function(r) { return r.track === track; });
                if (!_recv) return;
                var _srcs = (_recv.getSynchronizationSources && _recv.getSynchronizationSources()) || [];
                var _level = (_srcs.length && typeof _srcs[0].audioLevel === 'number') ? _srcs[0].audioLevel : -1;
                var _ssrc  = _srcs.length ? _srcs[0].source : null;

                // Learn SSRC on first active sample, then watch for speaker-slot change
                if (_ssrc != null) {
                    if (_learnedSSRC === null) {
                        _learnedSSRC = _ssrc;
                    } else if (_ssrc !== _learnedSSRC) {
                        _vadStop('ssrc-change');
                        return;
                    }
                }

                // Silence: audioLevel below threshold for 1s = speaker finished utterance
                if (_level >= 0 && _level < 0.01) {
                    if (!_silenceStart) _silenceStart = Date.now();
                    else if (Date.now() - _silenceStart >= 1000) _vadStop('silence');
                } else {
                    _silenceStart = null;
                }
            } catch(_e) {}
        }, 250);

        // Hard cap at 30s — flush long utterances that never go fully silent
        _vadCap = setTimeout(function() { _vadStop('cap-30s'); }, 30000);
    }

    // ── 4. Map audio track → participant display name ─────────────────────────

    // Words that appear in meeting titles but never in person names.
    // Used to reject meeting titles like "Morning Standup", "Strategic Discussion".
    var _MEETING_TITLE_WORDS = [
        'standup', 'stand-up', 'meeting', 'discussion', 'sync', 'weekly',
        'daily', 'scrum', 'sprint', 'retrospective', 'planning', 'review',
        'strategic', 'morning', 'evening', 'afternoon', 'all-hands', 'allhands',
        'kickoff', 'onboarding', 'interview', 'testing', 'demo', 'presentation',
    ];

    // Returns true if a string looks like a real person name.
    // Rejects meeting titles (contain '/'), UI strings, meeting-title words, and long text.
    function _looksLikePersonName(n) {
        if (!n || n.length < 2 || n.length > 40) return false;
        if (n.indexOf('/') >= 0) return false;   // "Shubham / Vaibhav" = meeting title
        if (n.indexOf('has joined') >= 0) return false;
        if (n.indexOf('has left') >= 0) return false;
        if (n.indexOf('camera') >= 0) return false;
        if (n.indexOf('microphone') >= 0) return false;
        if (n.indexOf('You ') === 0) return false;
        if (n === 'You') return false;
        if (n.indexOf('(') >= 0) return false;   // "Shubham (host)" — strip or skip
        // Reject strings that contain common meeting-title words
        var lower = n.toLowerCase();
        for (var _wi = 0; _wi < _MEETING_TITLE_WORDS.length; _wi++) {
            if (lower.indexOf(_MEETING_TITLE_WORDS[_wi]) >= 0) return false;
        }
        return true;
    }

    // Detect the currently-speaking participant from Meet DOM.
    // Returns a name string or null.  Called on every recorder cycle so we pick
    // up speaker changes even within a single 15-second chunk window.
    function _getActiveSpeakerName() {
        var speakingSelectors = [
            '[data-is-speaking="true"]',
            '[aria-label*=" is speaking"]',
            '[data-active-speaker="true"]',
        ];
        for (var _si = 0; _si < speakingSelectors.length; _si++) {
            try {
                var _el = document.querySelector(speakingSelectors[_si]);
                if (_el) {
                    var _n = (_el.getAttribute('data-display-name') || _el.innerText || '').trim().split('\n')[0];
                    if (_looksLikePersonName(_n)) return _n;
                    // Check parent tile
                    var _tile = _el.closest('[data-participant-id]') || _el.closest('[data-display-name]');
                    if (_tile) {
                        _n = (_tile.getAttribute('data-display-name') || _tile.innerText || '').trim().split('\n')[0];
                        if (_looksLikePersonName(_n)) return _n;
                    }
                }
            } catch(_e) {}
        }
        return null;
    }
    // Expose for Python to call at drain time
    window.__getActiveSpeakerName = _getActiveSpeakerName;

    // SSRC → find element with matching ssrc in DOM
    function _resolveBySSRC(track, pc) {
        try {
            var _recv = pc.getReceivers().find(function(r) { return r.track === track; });
            if (_recv) {
                var _srcs = _recv.getSynchronizationSources();
                if (_srcs.length) {
                    var _ssrc = _srcs[0].source;
                    var _ssrcSelectors = [
                        '[data-ssrc="' + _ssrc + '"]',
                        '[data-audio-muted][data-ssrc="' + _ssrc + '"]',
                    ];
                    for (var _i = 0; _i < _ssrcSelectors.length; _i++) {
                        var _ssrcEl = document.querySelector(_ssrcSelectors[_i]);
                        if (_ssrcEl) {
                            var _ssrcName = (_ssrcEl.getAttribute('data-display-name') ||
                                            _ssrcEl.getAttribute('data-name') ||
                                            _ssrcEl.innerText || '').trim().split('\n')[0];
                            if (_looksLikePersonName(_ssrcName)) return _ssrcName;
                        }
                    }
                }
            }
        } catch(_e) {}
        return null;
    }

    // Re-resolved fresh on every dataavailable event — NO permanent cache.
    // Google Meet's SFU reuses 3 audio track slots as different participants speak,
    // so caching track→name for the meeting lifetime gives wrong attribution.
    function _resolveParticipantName(track, pc) {
        // 1. Active speaker from DOM (changes in real-time)
        var _active = _getActiveSpeakerName();
        if (_active) return _active;

        // 2. SSRC attribute lookup
        var _ssrcName = _resolveBySSRC(track, pc);
        if (_ssrcName) return _ssrcName;

        // 3. Participant tile with data-display-name scoped to participant containers only
        try {
            var _tiles = document.querySelectorAll('[data-participant-id][data-display-name]');
            if (_tiles.length === 1) {
                // Only one remote participant → must be them
                var _tn = _tiles[0].getAttribute('data-display-name').trim();
                if (_looksLikePersonName(_tn)) return _tn;
            }
        } catch(_e2) {}

        return 'Unknown';
    }

    // ── 5. Expose drain function to Python ────────────────────────────────────
    window.__drainAudioChunks = function() {
        const chunks = window.__audioChunks;
        window.__audioChunks = [];
        return chunks;
    };

    // ── 6. Expose track info for debugging ────────────────────────────────────
    window.__getTrackInfo = function() {
        return Object.entries(window.__trackRecorders).map(([id, info]) => ({
            trackId: id,
            participantName: info.participantName,
            state: info.recorder ? info.recorder.state : 'none',
            active: info.active
        }));
    };

    // ── 7. Per-source audio-level poller (MEASURED speaker signal) ─────────────
    // Every 250ms, read each receiver's getSynchronizationSources() — each entry
    // has the RTP source id (SSRC) and a live audioLevel (0..1). Meet keeps a
    // stable source id per participant, so this tells us WHO actually produced
    // audio at each moment (not a caption guess). We also snapshot the DOM
    // active-speaker name so Python can LEARN source-id -> real name over time.
    window.__speakerSamples = [];   // [{t, ssrc, csrc, level, name}]
    function _pollAudioLevels() {
        try {
            var now = Date.now();
            var activeName = null;
            try { activeName = _getActiveSpeakerName(); } catch(_e) {}
            for (var pi = 0; pi < window.__peerConns.length; pi++) {
                var recvs;
                try { recvs = window.__peerConns[pi].getReceivers(); } catch(_e) { continue; }
                for (var ri = 0; ri < recvs.length; ri++) {
                    var r = recvs[ri];
                    if (!r.track || r.track.kind !== 'audio') continue;
                    var tid = r.track.id;
                    var srcs = [];
                    try { srcs = (r.getSynchronizationSources && r.getSynchronizationSources()) || []; } catch(_e) {}
                    var csrcs = [];
                    try { csrcs = (r.getContributingSources && r.getContributingSources()) || []; } catch(_e) {}
                    var csrc = csrcs.length ? csrcs[0].source : null;
                    for (var si = 0; si < srcs.length; si++) {
                        var s = srcs[si];
                        if (s.source == null) continue;
                        var lvl = (typeof s.audioLevel === 'number') ? s.audioLevel : 0;
                        window.__speakerSamples.push({
                            t: now, track: tid, ssrc: s.source, csrc: csrc,
                            level: lvl, name: activeName || null
                        });
                    }
                }
            }
            if (window.__speakerSamples.length > 4000) {
                window.__speakerSamples = window.__speakerSamples.slice(-2000);
            }
        } catch(_e) {}
    }
    setInterval(_pollAudioLevels, 250);

    // Drain accumulated samples (Python builds the source->name map + timeline).
    window.__drainSpeakerSamples = function() {
        var s = window.__speakerSamples;
        window.__speakerSamples = [];
        return s;
    };

    console.log('[WebRTC capture] Interceptor installed (cycling recorder mode + audio-level poller)');
})();
"""


# ── Mixed-audio fallback: record the entire tab's output ─────────────────────
# Used when per-track interception has no tracks yet (e.g., first 10 seconds).
# Records everything the tab plays — all speakers mixed, no per-speaker identity.
# Also uses stop()+start() cycling for complete webm files.

MIXED_AUDIO_JS = (
    r"""
(function() {
    if (window.__mixedRecorderActive) return;

    const CHUNK_MS = """
    + str(_CHUNK_MS)
    + r""";

    try {
        const ctx = new AudioContext();
        const dest = ctx.createMediaStreamDestination();

        function hookAudioElements() {
            document.querySelectorAll('audio').forEach(el => {
                if (el.__hooked) return;
                el.__hooked = true;
                const src = ctx.createMediaElementSource(el);
                src.connect(dest);
                src.connect(ctx.destination);
            });
        }

        hookAudioElements();
        new MutationObserver(hookAudioElements).observe(document.body, {
            childList: true, subtree: true
        });

        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus' : 'audio/webm';

        window.__mixedRecorderActive = true;

        function _cycleMixed() {
            const recorder = new MediaRecorder(dest.stream, {
                mimeType, audioBitsPerSecond: 64000
            });
            recorder.addEventListener('dataavailable', ev => {
                if (!ev.data || ev.data.size < 512) return;
                const reader = new FileReader();
                reader.onloadend = () => {
                    window.__audioChunks = window.__audioChunks || [];
                    window.__audioChunks.push({
                        trackId: 'mixed',
                        participantName: null,
                        b64: reader.result.split(',')[1],
                        ts: Date.now()
                    });
                };
                reader.readAsDataURL(ev.data);
            });
            recorder.addEventListener('stop', () => {
                if (window.__mixedRecorderActive) setTimeout(_cycleMixed, 50);
            });
            recorder.start();
            setTimeout(() => {
                if (recorder.state === 'recording') recorder.stop();
            }, CHUNK_MS);
        }

        _cycleMixed();
        console.log('[WebRTC capture] Mixed audio cycling recorder started');
    } catch(e) {
        console.warn('[WebRTC capture] Mixed audio recorder failed:', e);
    }
})();
"""
)


def webm_to_wav(webm_bytes: bytes) -> bytes:
    """
    Convert a complete webm/opus blob to 16kHz mono WAV for Sarvam.
    Each blob must be a self-contained webm file (has EBML header).
    Requires ffmpeg on PATH.
    """
    import subprocess
    import tempfile
    import os

    # Sanity check: webm files start with EBML magic bytes 0x1A 0x45 0xDF 0xA3
    if len(webm_bytes) < 4 or webm_bytes[:4] != b'\x1a\x45\xdf\xa3':
        logger.warning(
            "webm_to_wav: data does not start with EBML magic (got %s) — skipping %d bytes",
            webm_bytes[:4].hex(), len(webm_bytes)
        )
        return b""

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp_in:
        tmp_in.write(webm_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".webm", ".wav")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                "-fflags", "+discardcorrupt",   # tolerate chunks cut mid-frame by MediaRecorder
                "-i", tmp_in_path,
                "-ar", str(SAMPLE_RATE),
                "-ac", "1",
                "-threads", "1",                # bound CPU so concurrent chunks don't starve
                "-f", "wav",
                tmp_out_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            with open(tmp_out_path, "rb") as f:
                return f.read()
        else:
            logger.warning("ffmpeg conversion failed: %s", result.stderr.decode()[:300])
            return b""
    except FileNotFoundError:
        logger.warning("ffmpeg not found — install ffmpeg for WebRTC audio STT")
        return b""
    except Exception as exc:
        logger.warning("webm→wav conversion error: %s", exc)
        return b""
    finally:
        for p in (tmp_in_path, tmp_out_path):
            try:
                os.unlink(p)
            except Exception:
                pass


class WebRTCAudioCapturer:
    """
    Drains the browser's __audioChunks buffer and yields
    (participant_name, wav_bytes) tuples for STT processing.

    Each drained chunk is a COMPLETE webm file (produced by the cycling
    recorder) — no concatenation needed, no EBML header issues.

    Usage in meet_bot.py:
        capturer = WebRTCAudioCapturer(self.page)
        async for participant_name, wav in capturer.chunks():
            result = await sarvam.transcribe(wav)
    """

    def __init__(self, page) -> None:
        self.page = page
        self._running = False

    async def start_mixed_recorder(self) -> None:
        """Inject mixed-audio recorder as fallback when per-track has no data."""
        try:
            await self.page.evaluate(MIXED_AUDIO_JS)
            logger.info("WebRTC mixed-audio recorder started")
        except Exception as exc:
            logger.warning("Mixed-audio recorder failed: %s", exc)

    async def drain_speaker_samples(self) -> list:
        """Drain the per-source audio-level samples collected by the JS poller.
        Each sample: {t (JS epoch ms), ssrc, csrc, level (0..1), name (DOM active
        speaker or None)}. Used to build a measured speaker timeline + learn the
        source-id -> participant-name mapping."""
        try:
            return await self.page.evaluate(
                "() => window.__drainSpeakerSamples ? window.__drainSpeakerSamples() : []"
            ) or []
        except Exception as exc:
            logger.debug("drain_speaker_samples failed: %s", exc)
            return []

    async def chunks(self) -> AsyncIterator[dict]:
        """
        Drain audio chunks from browser every CHUNK_SEC seconds.
        Yields a dict: {participant_name, wav, track_id, ssrc, csrc, level, ts}.
          - participant_name: DOM-resolved name or None
          - ssrc/csrc: RTP source ids = the MEASURED true speaker for this chunk
          - level: audioLevel 0..1 at chunk boundary
          - ts: JS epoch ms when the chunk was finalized (≈ audio end)
        """
        self._running = True
        logger.info("WebRTC audio capturer started (drain interval=%ds)", _DRAIN_SEC)
        no_data_ticks = 0
        total_chunks_drained = 0
        _tick = 0

        while self._running:
            await asyncio.sleep(_DRAIN_SEC)
            _tick += 1

            # Heartbeat every 20 ticks (~1 min at 3s drain) so we can confirm the loop is alive
            if _tick % 20 == 0:
                try:
                    track_info = await self.page.evaluate("() => window.__getTrackInfo ? window.__getTrackInfo() : []")
                    logger.info(
                        "[diag] WebRTC heartbeat tick=%d — active tracks=%d, total_chunks=%d",
                        _tick, len(track_info) if track_info else 0, total_chunks_drained,
                    )
                    if track_info:
                        for t in track_info:
                            logger.info(
                                "[diag]   track participant=%s state=%s active=%s",
                                t.get("participantName", "?"), t.get("state", "?"), t.get("active", "?"),
                            )
                except Exception:
                    pass

            try:
                chunks = await self.page.evaluate("() => window.__drainAudioChunks()")
            except Exception as exc:
                logger.debug("WebRTC drain failed: %s", exc)
                continue

            if not chunks:
                no_data_ticks += 1
                if no_data_ticks == 3:
                    logger.warning(
                        "WebRTC: no per-track audio after %ds — starting mixed fallback",
                        no_data_ticks * CHUNK_SEC,
                    )
                    await self.start_mixed_recorder()
                elif no_data_ticks % 4 == 0:
                    logger.info("[diag] WebRTC: still no chunks (tick=%d) — waiting for tracks", no_data_ticks)
                continue

            no_data_ticks = 0
            total_chunks_drained += len(chunks)
            logger.info("[diag] WebRTC drained %d chunk(s) this tick (total=%d)", len(chunks), total_chunks_drained)

            # Each chunk is a COMPLETE webm file — process individually
            for chunk in chunks:
                track_id = chunk.get("trackId", "unknown")
                participant_name = chunk.get("participantName") or None
                if participant_name == "Unknown":
                    participant_name = None
                # Measured RTP source identity for this chunk (true speaker signal).
                _ssrc = chunk.get("ssrc")
                _csrc = chunk.get("csrc")
                _level = chunk.get("level") or 0.0
                _ts = chunk.get("ts")

                # Skip Google Meet internal processing tracks — not real speech
                if participant_name and participant_name.lower() in _INTERNAL_TRACK_NAMES:
                    logger.debug(
                        "WebRTC: skipping internal track %s (label=%s)",
                        track_id[:12], participant_name,
                    )
                    continue

                # Skip the bot's own audio track — mic is muted, only silence.
                # Use 'in' check so "OneClarity AI" matches even if name has extra spaces/case.
                if participant_name and _BOT_DISPLAY_NAME in participant_name.lower():
                    logger.debug(
                        "WebRTC: skipping bot's own track %s (label=%s)",
                        track_id[:12], participant_name,
                    )
                    continue

                try:
                    webm_data = base64.b64decode(chunk["b64"])
                except Exception as exc:
                    logger.debug("WebRTC: base64 decode failed for track %s: %s", track_id, exc)
                    continue

                webm_kb = len(webm_data) / 1024
                # A 15s silent webm/opus chunk is ~3-4KB. Real speech = 30KB+.
                # Skip silentchunks entirely — converting + sending them wastes
                # Sarvam credits and CPU with no useful transcript.
                if webm_kb < 5:
                    logger.debug(
                        "WebRTC: silent chunk skipped — track=%s participant=%s webm=%.1fKB",
                        track_id[:12], participant_name or "Unknown", webm_kb,
                    )
                    continue

                logger.info(
                    "WebRTC: audio chunk track=%s participant=%s webm=%.1fKB → converting",
                    track_id[:12], participant_name or "Unknown", webm_kb,
                )

                wav = await asyncio.get_running_loop().run_in_executor(
                    None, webm_to_wav, webm_data
                )
                if wav:
                    logger.info(
                        "WebRTC: WAV %.1fKB → Sarvam STT (participant=%s ssrc=%s level=%.3f)",
                        len(wav) / 1024, participant_name or "Unknown", _ssrc, _level,
                    )
                    yield {
                        "participant_name": participant_name,
                        "wav": wav,
                        "track_id": track_id,
                        "ssrc": _ssrc,
                        "csrc": _csrc,
                        "level": _level,
                        "ts": _ts,
                    }
                else:
                    logger.warning(
                        "WebRTC: webm→wav conversion returned empty for track=%s participant=%s",
                        track_id[:12], participant_name or "Unknown",
                    )

    def stop(self) -> None:
        self._running = False
