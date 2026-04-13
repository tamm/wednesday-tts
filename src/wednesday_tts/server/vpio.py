"""VPIO audio unit for the wednesday-tts daemon.

Wraps CoreAudio's VoiceProcessingIO audio unit:
- Bus 0 (output): TTS PCM fed here plays through speakers + provides the AEC reference
- Bus 1 (input):  AEC-processed mic audio pulled here, published to a Unix socket

The daemon feeds TTS audio via feed_audio() instead of PortAudio out_stream.write().
Wednesday-yarn reads clean mic PCM from /tmp/wednesday-yarn-mic.sock.

Socket protocol: 4-byte uint32 LE sample rate header, then raw float32 PCM (little-endian).
VPIO runs at 16kHz mono (STT-compatible). feed_audio() resamples from any source rate.

Requires macOS (Darwin). No-ops on other platforms.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import os
import platform
import socket
import struct
import threading
from ctypes import c_double, c_int16, c_int32, c_uint32, c_uint64, c_void_p

import numpy as np

logger = logging.getLogger(__name__)

MIC_SOCK_PATH = "/tmp/wednesday-yarn-mic.sock"
_VPIO_SAMPLE_RATE = 16000  # Hz — matches STT pipeline expectation

# ---------------------------------------------------------------------------
# CoreAudio constants
# ---------------------------------------------------------------------------
kAudioUnitType_Output = 0x6175_6F75  # 'auou'
kAudioUnitSubType_VoiceProcessingIO = 0x7670_696F  # 'vpio'
kAudioUnitManufacturer_Apple = 0x6170_706C  # 'appl'
kAudioFormatLinearPCM = 0x6C70_636D  # 'lpcm'
kAudioFormatFlagIsFloat = 0x1
kAudioFormatFlagIsPacked = 0x8
kAudioFormatFlagIsNonInterleaved = 0x20

kAudioUnitScope_Global = 0
kAudioUnitScope_Input = 1
kAudioUnitScope_Output = 2

kInputBus = 1  # element 1 = microphone
kOutputBus = 0  # element 0 = speaker

kAudioOutputUnitProperty_EnableIO = 2003
kAudioUnitProperty_StreamFormat = 8
kAudioUnitProperty_SetRenderCallback = 23
kAudioOutputUnitProperty_SetInputCallback = 2005
kAudioUnitProperty_MaximumFramesPerSlice = 14
kAUVoiceIOProperty_VoiceProcessingEnableAGC = 2101
kAUVoiceIOProperty_OtherAudioDuckingConfiguration = 2108

kAUVoiceIOOtherAudioDuckingLevelMin = 10

# ---------------------------------------------------------------------------
# ctypes structs
# ---------------------------------------------------------------------------


class AudioComponentDescription(ctypes.Structure):
    _fields_ = [
        ("componentType", c_uint32),
        ("componentSubType", c_uint32),
        ("componentManufacturer", c_uint32),
        ("componentFlags", c_uint32),
        ("componentFlagsMask", c_uint32),
    ]


class AudioStreamBasicDescription(ctypes.Structure):
    _fields_ = [
        ("mSampleRate", c_double),
        ("mFormatID", c_uint32),
        ("mFormatFlags", c_uint32),
        ("mBytesPerPacket", c_uint32),
        ("mFramesPerPacket", c_uint32),
        ("mBytesPerFrame", c_uint32),
        ("mChannelsPerFrame", c_uint32),
        ("mBitsPerChannel", c_uint32),
        ("mReserved", c_uint32),
    ]


class SMPTETime(ctypes.Structure):
    _fields_ = [
        ("mSubframes", c_int16),
        ("mSubframeDivisor", c_int16),
        ("mCounter", c_uint32),
        ("mType", c_uint32),
        ("mFlags", c_uint32),
        ("mHours", c_int16),
        ("mMinutes", c_int16),
        ("mSeconds", c_int16),
        ("mFrames", c_int16),
    ]


class AudioTimeStamp(ctypes.Structure):
    _fields_ = [
        ("mSampleTime", c_double),
        ("mHostTime", c_uint64),
        ("mRateScalar", c_double),
        ("mWordClockTime", c_uint64),
        ("mSMPTETime", SMPTETime),
        ("mFlags", c_uint32),
        ("mReserved", c_uint32),
    ]


class AudioBuffer(ctypes.Structure):
    _fields_ = [
        ("mNumberChannels", c_uint32),
        ("mDataByteSize", c_uint32),
        ("mData", c_void_p),
    ]


class AudioBufferList(ctypes.Structure):
    _fields_ = [
        ("mNumberBuffers", c_uint32),
        ("mBuffers", AudioBuffer * 1),
    ]


AURenderCallback = ctypes.CFUNCTYPE(
    c_int32,  # OSStatus return
    c_void_p,  # inRefCon
    ctypes.POINTER(c_uint32),  # ioActionFlags
    ctypes.POINTER(AudioTimeStamp),  # inTimeStamp
    c_uint32,  # inBusNumber
    c_uint32,  # inNumberFrames
    ctypes.POINTER(AudioBufferList),  # ioData (NULL for input callbacks)
)


class AUVoiceIOOtherAudioDuckingConfiguration(ctypes.Structure):
    _fields_ = [
        ("mEnableAdvancedDucking", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8 * 3),
        ("mDuckingLevel", c_uint32),
    ]


class AURenderCallbackStruct(ctypes.Structure):
    _fields_ = [
        ("inputProc", AURenderCallback),
        ("inputProcRefCon", c_void_p),
    ]


# ---------------------------------------------------------------------------
# AudioToolbox loader
# ---------------------------------------------------------------------------


def _load_audio_toolbox():
    if platform.system() != "Darwin":
        raise OSError("VoiceProcessingIO is only available on macOS")

    lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")

    lib.AudioComponentFindNext.restype = c_void_p
    lib.AudioComponentFindNext.argtypes = [c_void_p, ctypes.POINTER(AudioComponentDescription)]

    lib.AudioComponentInstanceNew.restype = c_int32
    lib.AudioComponentInstanceNew.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]

    lib.AudioComponentInstanceDispose.restype = c_int32
    lib.AudioComponentInstanceDispose.argtypes = [c_void_p]

    lib.AudioUnitSetProperty.restype = c_int32
    lib.AudioUnitSetProperty.argtypes = [
        c_void_p,
        c_uint32,
        c_uint32,
        c_uint32,
        c_void_p,
        c_uint32,
    ]

    lib.AudioUnitInitialize.restype = c_int32
    lib.AudioUnitInitialize.argtypes = [c_void_p]

    lib.AudioUnitUninitialize.restype = c_int32
    lib.AudioUnitUninitialize.argtypes = [c_void_p]

    lib.AudioOutputUnitStart.restype = c_int32
    lib.AudioOutputUnitStart.argtypes = [c_void_p]

    lib.AudioOutputUnitStop.restype = c_int32
    lib.AudioOutputUnitStop.argtypes = [c_void_p]

    lib.AudioUnitRender.restype = c_int32
    lib.AudioUnitRender.argtypes = [
        c_void_p,
        ctypes.POINTER(c_uint32),
        ctypes.POINTER(AudioTimeStamp),
        c_uint32,
        c_uint32,
        ctypes.POINTER(AudioBufferList),
    ]

    return lib


# ---------------------------------------------------------------------------
# VPIOUnit
# ---------------------------------------------------------------------------


class VPIOUnit:
    """CoreAudio VoiceProcessingIO wrapper for the TTS daemon.

    Usage:
        unit = VPIOUnit()
        unit.setup()   # initialises the audio unit
        unit.start()   # starts the CoreAudio I/O thread
        unit.feed_audio(pcm_float32, sample_rate=24000)  # called per TTS chunk
        unit.stop()    # teardown
    """

    def __init__(self) -> None:
        self._lib = _load_audio_toolbox()
        self._audio_unit: c_void_p | None = None
        self._running = False

        # Output deque: TTS PCM resampled to _VPIO_SAMPLE_RATE, fed to Bus 0.
        # Accessed from the asyncio/playback thread (feed_audio) and CoreAudio's
        # realtime thread (_output_render_callback). Individual deque ops are
        # atomic under CPython's GIL — no lock needed, and adding one would risk
        # stalling the realtime thread.
        self._pcm_buffer: collections.deque = collections.deque()

        # Mic socket publisher — runs in its own thread
        self._mic_server_thread: threading.Thread | None = None
        self._mic_clients: list[socket.socket] = []
        self._mic_clients_lock = threading.Lock()
        self._mic_stop = threading.Event()

        # Must keep ctypes callback objects alive to prevent GC
        self._output_cb: AURenderCallback | None = None
        self._input_cb: AURenderCallback | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Create and configure the VPIO audio unit. Does not start I/O."""
        lib = self._lib

        desc = AudioComponentDescription(
            componentType=kAudioUnitType_Output,
            componentSubType=kAudioUnitSubType_VoiceProcessingIO,
            componentManufacturer=kAudioUnitManufacturer_Apple,
            componentFlags=0,
            componentFlagsMask=0,
        )
        component = lib.AudioComponentFindNext(None, ctypes.byref(desc))
        if not component:
            raise RuntimeError(
                "AudioComponentFindNext returned NULL — "
                "VoiceProcessingIO not available on this system"
            )

        audio_unit = c_void_p()
        status = lib.AudioComponentInstanceNew(component, ctypes.byref(audio_unit))
        if status != 0:
            raise RuntimeError(f"AudioComponentInstanceNew failed: OSStatus {status}")

        # Enable input on Bus 1 (microphone)
        one = c_uint32(1)
        status = lib.AudioUnitSetProperty(
            audio_unit,
            kAudioOutputUnitProperty_EnableIO,
            kAudioUnitScope_Input,
            kInputBus,
            ctypes.byref(one),
            ctypes.sizeof(c_uint32),
        )
        if status != 0:
            lib.AudioComponentInstanceDispose(audio_unit)
            raise RuntimeError(f"EnableIO (input) failed: OSStatus {status}")

        # Float32 mono at VPIO rate
        fmt = AudioStreamBasicDescription(
            mSampleRate=float(_VPIO_SAMPLE_RATE),
            mFormatID=kAudioFormatLinearPCM,
            mFormatFlags=(
                kAudioFormatFlagIsFloat
                | kAudioFormatFlagIsPacked
                | kAudioFormatFlagIsNonInterleaved
            ),
            mBytesPerPacket=4,
            mFramesPerPacket=1,
            mBytesPerFrame=4,
            mChannelsPerFrame=1,
            mBitsPerChannel=32,
            mReserved=0,
        )

        # Stream format on input bus (scope=Output = what comes out of mic bus)
        status = lib.AudioUnitSetProperty(
            audio_unit,
            kAudioUnitProperty_StreamFormat,
            kAudioUnitScope_Output,
            kInputBus,
            ctypes.byref(fmt),
            ctypes.sizeof(AudioStreamBasicDescription),
        )
        if status != 0:
            lib.AudioComponentInstanceDispose(audio_unit)
            raise RuntimeError(f"SetStreamFormat (input bus) failed: OSStatus {status}")

        # Stream format on output bus (scope=Input = what we feed to speakers).
        # VPIO needs this set or AudioUnitInitialize fails (-10875).
        status = lib.AudioUnitSetProperty(
            audio_unit,
            kAudioUnitProperty_StreamFormat,
            kAudioUnitScope_Input,
            kOutputBus,
            ctypes.byref(fmt),
            ctypes.sizeof(AudioStreamBasicDescription),
        )
        if status != 0:
            lib.AudioComponentInstanceDispose(audio_unit)
            raise RuntimeError(f"SetStreamFormat (output bus) failed: OSStatus {status}")

        # Output render callback: feeds TTS PCM to Bus 0 (speakers + AEC reference)
        self._output_cb = AURenderCallback(self._output_render_callback)
        out_cb_struct = AURenderCallbackStruct(
            inputProc=self._output_cb,
            inputProcRefCon=None,
        )
        status = lib.AudioUnitSetProperty(
            audio_unit,
            kAudioUnitProperty_SetRenderCallback,
            kAudioUnitScope_Input,
            kOutputBus,
            ctypes.byref(out_cb_struct),
            ctypes.sizeof(AURenderCallbackStruct),
        )
        if status != 0:
            lib.AudioComponentInstanceDispose(audio_unit)
            raise RuntimeError(f"SetRenderCallback (output) failed: OSStatus {status}")

        # Input callback: pulls AEC-processed mic from Bus 1
        self._input_cb = AURenderCallback(self._input_render_callback)
        in_cb_struct = AURenderCallbackStruct(
            inputProc=self._input_cb,
            inputProcRefCon=None,
        )
        status = lib.AudioUnitSetProperty(
            audio_unit,
            kAudioOutputUnitProperty_SetInputCallback,
            kAudioUnitScope_Global,
            kInputBus,
            ctypes.byref(in_cb_struct),
            ctypes.sizeof(AURenderCallbackStruct),
        )
        if status != 0:
            lib.AudioComponentInstanceDispose(audio_unit)
            raise RuntimeError(f"SetInputCallback failed: OSStatus {status}")

        # Disable AGC (non-fatal)
        zero = c_uint32(0)
        st = lib.AudioUnitSetProperty(
            audio_unit,
            kAUVoiceIOProperty_VoiceProcessingEnableAGC,
            kAudioUnitScope_Global,
            kInputBus,
            ctypes.byref(zero),
            ctypes.sizeof(c_uint32),
        )
        if st != 0:
            logger.debug("Failed to disable AGC (non-fatal): OSStatus %d", st)

        # Minimum ducking (non-fatal)
        ducking_cfg = AUVoiceIOOtherAudioDuckingConfiguration(
            mEnableAdvancedDucking=1,
            mDuckingLevel=kAUVoiceIOOtherAudioDuckingLevelMin,
        )
        st = lib.AudioUnitSetProperty(
            audio_unit,
            kAUVoiceIOProperty_OtherAudioDuckingConfiguration,
            kAudioUnitScope_Global,
            kOutputBus,
            ctypes.byref(ducking_cfg),
            ctypes.sizeof(AUVoiceIOOtherAudioDuckingConfiguration),
        )
        if st != 0:
            logger.debug("Failed to set VPIO ducking config (non-fatal): OSStatus %d", st)
        else:
            logger.info("VPIO ducking set to minimum with advanced ducking enabled")

        status = lib.AudioUnitInitialize(audio_unit)
        if status != 0:
            lib.AudioComponentInstanceDispose(audio_unit)
            raise RuntimeError(f"AudioUnitInitialize failed: OSStatus {status}")

        self._audio_unit = audio_unit
        logger.info("VPIO audio unit initialised at %dHz mono", _VPIO_SAMPLE_RATE)

    def start(self) -> None:
        """Start the VPIO audio unit and mic socket server."""
        if self._audio_unit is None:
            raise RuntimeError("Call setup() before start()")
        status = self._lib.AudioOutputUnitStart(self._audio_unit)
        if status != 0:
            raise RuntimeError(f"AudioOutputUnitStart failed: OSStatus {status}")
        self._running = True
        self._start_mic_server()
        logger.info("VPIO started")

    def stop(self) -> None:
        """Stop the VPIO audio unit and tear down the mic server."""
        self._running = False
        self._stop_mic_server()
        if self._audio_unit is not None:
            self._lib.AudioOutputUnitStop(self._audio_unit)
            self._lib.AudioUnitUninitialize(self._audio_unit)
            self._lib.AudioComponentInstanceDispose(self._audio_unit)
            self._audio_unit = None
        logger.info("VPIO stopped")

    def feed_audio(self, audio: np.ndarray, sample_rate: int = 24000) -> None:
        """Queue TTS PCM for playback through VPIO Bus 0.

        Audio is resampled to _VPIO_SAMPLE_RATE if needed.
        Safe to call from any thread — deque.extend() is atomic under GIL.

        Args:
            audio: float32 mono PCM
            sample_rate: source sample rate (daemon produces 24kHz)
        """
        if audio is None or len(audio) == 0:
            return

        data = np.asarray(audio, dtype=np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1)  # downmix to mono if needed

        if sample_rate != _VPIO_SAMPLE_RATE:
            target_len = int(len(data) * _VPIO_SAMPLE_RATE / sample_rate)
            if target_len > 0:
                data = np.interp(
                    np.linspace(0, len(data) - 1, target_len),
                    np.arange(len(data)),
                    data,
                ).astype(np.float32)

        # Atomic under GIL — no lock needed on realtime thread
        self._pcm_buffer.extend(data.tolist())

    def clear_buffer(self) -> None:
        """Drain the output PCM buffer (e.g. on stop/skip)."""
        self._pcm_buffer.clear()

    # ------------------------------------------------------------------
    # CoreAudio callbacks (run on the realtime I/O thread)
    # ------------------------------------------------------------------

    def _output_render_callback(
        self,
        ref_con,
        action_flags,
        timestamp,
        bus_number,
        num_frames,
        io_data,
    ) -> int:
        """Feed TTS PCM to Bus 0 (speakers + AEC reference).

        Outputs silence when the buffer is empty.
        RUNS ON CORAAUDIO REALTIME THREAD — no locks, no allocations.
        """
        if not io_data:
            return 0

        abl = io_data.contents
        for i in range(abl.mNumberBuffers):
            buf = abl.mBuffers[i]
            if not buf.mData or not buf.mDataByteSize:
                continue

            frames_needed = buf.mDataByteSize // 4  # float32 = 4 bytes
            out_ptr = (ctypes.c_float * frames_needed).from_address(buf.mData)
            buf_ref = self._pcm_buffer  # local ref to avoid repeated attr lookup

            for j in range(frames_needed):
                try:
                    out_ptr[j] = buf_ref.popleft()
                except IndexError:
                    # Buffer empty — fill remainder with silence
                    for k in range(j, frames_needed):
                        out_ptr[k] = 0.0
                    break

        return 0

    def _input_render_callback(
        self,
        ref_con,
        action_flags,
        timestamp,
        bus_number,
        num_frames,
        _io_data,
    ) -> int:
        """Pull AEC-processed mic from Bus 1 and publish to socket clients.

        RUNS ON CORAAUDIO REALTIME THREAD — no locks, no allocations beyond
        the ctypes buffer. Socket sends happen on the publisher thread via a
        separate queue to avoid blocking CoreAudio.
        """
        if not self._running or self._audio_unit is None:
            return 0

        try:
            buf_size = num_frames * 4  # float32
            raw = (ctypes.c_float * num_frames)()
            ab = AudioBuffer(
                mNumberChannels=1,
                mDataByteSize=buf_size,
                mData=ctypes.cast(raw, c_void_p),
            )
            abl = AudioBufferList(mNumberBuffers=1)
            abl.mBuffers[0] = ab

            status = self._lib.AudioUnitRender(
                self._audio_unit,
                action_flags,
                timestamp,
                bus_number,
                num_frames,
                ctypes.byref(abl),
            )
            if status != 0:
                return status

            audio = np.ctypeslib.as_array(raw, shape=(num_frames,)).copy()
            self._publish_mic(audio)

        except Exception:
            logger.exception("Error in VPIO input callback")

        return 0

    # ------------------------------------------------------------------
    # Mic socket server
    # ------------------------------------------------------------------

    def _start_mic_server(self) -> None:
        """Start the Unix socket server that publishes mic PCM."""
        self._mic_stop.clear()
        try:
            os.unlink(MIC_SOCK_PATH)
        except FileNotFoundError:
            pass

        self._mic_server_thread = threading.Thread(
            target=self._mic_server_loop,
            daemon=True,
            name="vpio-mic-server",
        )
        self._mic_server_thread.start()

    def _stop_mic_server(self) -> None:
        self._mic_stop.set()
        # Close all active client connections to unblock any pending sends
        with self._mic_clients_lock:
            for conn in self._mic_clients:
                try:
                    conn.close()
                except Exception:
                    pass
            self._mic_clients.clear()
        if self._mic_server_thread is not None:
            self._mic_server_thread.join(timeout=2.0)
            self._mic_server_thread = None
        try:
            os.unlink(MIC_SOCK_PATH)
        except FileNotFoundError:
            pass

    def _mic_server_loop(self) -> None:
        """Accept connections on MIC_SOCK_PATH and track client list."""
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(MIC_SOCK_PATH)
        except OSError as exc:
            logger.error("VPIO mic server bind failed: %s", exc)
            return
        srv.listen(4)
        srv.settimeout(1.0)
        logger.info("VPIO mic server listening on %s", MIC_SOCK_PATH)

        while not self._mic_stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            # Send protocol header: 4-byte uint32 LE sample rate
            try:
                conn.sendall(struct.pack("<I", _VPIO_SAMPLE_RATE))
            except OSError:
                conn.close()
                continue

            with self._mic_clients_lock:
                self._mic_clients.append(conn)
            logger.info("VPIO mic client connected")

            # Spawn a lightweight thread to detect disconnects
            threading.Thread(
                target=self._mic_client_watch,
                args=(conn,),
                daemon=True,
                name="vpio-mic-client",
            ).start()

        srv.close()

    def _mic_client_watch(self, conn: socket.socket) -> None:
        """Block on recv to detect disconnect, then remove from client list."""
        try:
            conn.recv(1)  # blocks until closed by client or stop()
        except OSError:
            pass
        with self._mic_clients_lock:
            try:
                self._mic_clients.remove(conn)
            except ValueError:
                pass
        try:
            conn.close()
        except OSError:
            pass
        logger.info("VPIO mic client disconnected")

    def _publish_mic(self, audio: np.ndarray) -> None:
        """Send mic PCM to all connected clients.

        Called from the CoreAudio realtime thread — we do a quick non-blocking
        send attempt. Clients that can't keep up are disconnected.
        """
        if not self._mic_clients:
            return

        payload = audio.astype(np.float32).tobytes()
        dead: list[socket.socket] = []

        with self._mic_clients_lock:
            clients = list(self._mic_clients)

        for conn in clients:
            try:
                conn.sendall(payload)
            except OSError:
                dead.append(conn)

        if dead:
            with self._mic_clients_lock:
                for conn in dead:
                    try:
                        self._mic_clients.remove(conn)
                    except ValueError:
                        pass
                    try:
                        conn.close()
                    except OSError:
                        pass
