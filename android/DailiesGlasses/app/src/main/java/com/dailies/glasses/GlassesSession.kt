// Holds the camera open on the glasses and takes a still every few seconds.
//
// The frame source is `stream.capturePhoto()` rather than the video stream, and that is the
// single most important decision in this file. The video stream delivers compressed HEVC,
// which would mean carrying a MediaCodec decoder, an ImageReader, colour conversion and a
// pile of lifecycle around it, all to produce a JPEG. capturePhoto returns a Bitmap. The
// check runs every few seconds, not every frame, so the stream's frame rate buys nothing
// and costs a decoder.
//
// The stream still has to be started, because photo capture hangs off it and the camera has
// to be live. It is simply never decoded.

package com.dailies.glasses

import android.app.Application
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import com.meta.wearable.dat.camera.Camera
import com.meta.wearable.dat.camera.Stream
import com.meta.wearable.dat.camera.addCamera
import com.meta.wearable.dat.camera.types.PhotoData
import com.meta.wearable.dat.camera.types.StreamConfiguration
import com.meta.wearable.dat.camera.types.StreamState
import com.meta.wearable.dat.camera.types.VideoQuality
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.core.selectors.AutoDeviceSelector
import com.meta.wearable.dat.core.selectors.DeviceSelector
import com.meta.wearable.dat.core.session.DeviceSession
import com.meta.wearable.dat.core.session.DeviceSessionState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.nio.ByteBuffer

private const val TAG = "DailiesGlasses"
private const val SETUP_TIMEOUT_MS = 30_000L

enum class Link { Idle, Connecting, Ready, Error }

class GlassesSession(private val app: Application, private val scope: CoroutineScope) {

    private val selector: DeviceSelector by lazy { AutoDeviceSelector() }
    private var session: DeviceSession? = null
    private var camera: Camera? = null
    private var stream: Stream? = null
    private var stateJob: Job? = null

    private val _link = MutableStateFlow(Link.Idle)
    val link: StateFlow<Link> = _link.asStateFlow()

    private val _message = MutableStateFlow("")
    val message: StateFlow<String> = _message.asStateFlow()

    fun initialize() {
        runCatching { Wearables.initialize(app) }
            .onFailure { _message.value = "SDK init failed: ${it.message}" }
    }

    /** Session, camera, stream. Returns true once frames can be captured. */
    suspend fun connect(): Boolean {
        if (_link.value == Link.Ready) return true
        _link.value = Link.Connecting
        _message.value = "waiting for glasses"

        val active = withTimeoutOrNull(SETUP_TIMEOUT_MS) {
            selector.activeDeviceFlow().first { it != null }
        }
        if (active == null) {
            fail("No active glasses. Check they are on, worn, and paired in the Meta AI app.")
            return false
        }

        var created: DeviceSession? = null
        var err: String? = null
        Wearables.createSession(selector)
            .onSuccess { created = it }
            .onFailure { e, _ -> err = e.description }
        val active_session = created ?: run { fail("Session: ${err ?: "unknown error"}"); return false }

        session = active_session
        stateJob = scope.launch {
            active_session.state.collect { if (it == DeviceSessionState.STOPPED) reset() }
        }
        active_session.start()
        _message.value = "starting session"
        if (withTimeoutOrNull(SETUP_TIMEOUT_MS) {
                active_session.state.first { it == DeviceSessionState.STARTED }
            } == null) {
            fail("Session did not start."); return false
        }

        // MEDIUM, not the highest quality available. Frames are downscaled to 512px before
        // they leave the phone, so capturing at maximum resolution would only spend battery
        // and radio to throw the pixels away.
        var added: Camera? = null
        active_session
            .addCamera(StreamConfiguration(videoQuality = VideoQuality.MEDIUM, frameRate = 24))
            .onSuccess { added = it }
            .onFailure { e, _ -> err = e.description }
        val activeCamera = added ?: run { fail("Camera: ${err ?: "unknown error"}"); return false }

        camera = activeCamera
        val activeStream = activeCamera.stream
        stream = activeStream
        _message.value = "opening camera"

        activeStream.start().onFailure { e, _ -> err = e.description }
        if (withTimeoutOrNull(SETUP_TIMEOUT_MS) {
                activeStream.state.first { it == StreamState.STREAMING }
            } == null) {
            fail("Camera did not open: ${err ?: "timed out"}"); return false
        }

        _link.value = Link.Ready
        _message.value = "camera live"
        return true
    }

    /** One still from the glasses, or null if the capture failed. */
    suspend fun grab(): Bitmap? {
        val active = stream ?: return null
        var bitmap: Bitmap? = null
        active.capturePhoto()
            .onSuccess { bitmap = decode(it) }
            .onFailure { e, _ -> Log.w(TAG, "capture failed: ${e.description}") }
        return bitmap
    }

    private fun decode(photo: PhotoData): Bitmap? =
        when (photo) {
            is PhotoData.Bitmap -> photo.bitmap
            is PhotoData.HEIC -> decodeHeic(photo.data)
        }

    private fun decodeHeic(data: ByteBuffer): Bitmap? {
        val buffer = data.duplicate().apply { rewind() }
        val bytes = ByteArray(buffer.remaining())
        buffer.get(bytes)
        return runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size) }.getOrNull()
    }

    fun disconnect() {
        runCatching { camera?.close() }
        runCatching { session?.stop() }
        reset()
    }

    private fun reset() {
        stateJob?.cancel(); stateJob = null
        camera = null; stream = null; session = null
        if (_link.value != Link.Error) {
            _link.value = Link.Idle
            _message.value = ""
        }
    }

    private fun fail(reason: String) {
        Log.e(TAG, reason)
        _link.value = Link.Error
        _message.value = reason
        runCatching { camera?.close() }
        runCatching { session?.stop() }
    }
}
