// Says the verdict out loud, which on a set means into the glasses.
//
// This is the whole reason the app is worth building rather than just using the phone. A
// script supervisor is watching the scene, not a screen. Anything that requires them to
// look down has already failed, because the moment they look down is the moment the take
// is happening without them.
//
// The Ray-Ban Meta glasses register as an ordinary Bluetooth audio device, so once they are
// the active output, TextToSpeech lands in the wearer's ear with no special routing. That
// is also the honest limit of the hardware: these glasses have no display, so audio IS the
// output channel. It turns out to be the right one anyway.

package com.dailies.glasses

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale

class Voice(context: Context) {

    private var tts: TextToSpeech? = null
    private var ready = false

    /** What was last said, so the same divergence is not repeated on every check. */
    private var lastSpoken: String? = null

    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    init {
        tts = TextToSpeech(context.applicationContext) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale.UK
                // Slightly brisk. This is an interruption during a take, so it needs to be
                // over quickly.
                tts?.setSpeechRate(1.08f)
                tts?.setAudioAttributes(
                    AudioAttributes.Builder()
                        // ASSISTANCE_ACCESSIBILITY rather than MEDIA: it ducks other audio
                        // instead of being ducked BY it, which matters when something else
                        // on the phone is playing.
                        .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                ready = true
            }
        }
        tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {}
            override fun onDone(utteranceId: String?) {}
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) {}
        })
    }

    /** True when audio is going somewhere on the wearer's head rather than the phone speaker. */
    fun onHeadset(): Boolean =
        audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any {
            it.type == android.media.AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                it.type == android.media.AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                it.type == android.media.AudioDeviceInfo.TYPE_WIRED_HEADSET
        }

    /**
     * Speak a divergence, but only if it is not the one just announced.
     *
     * Checks repeat every few seconds and a prop stays wrong until someone fixes it, so
     * without this the glasses would repeat the same sentence into the wearer's ear until
     * they took them off. Saying it once and then staying quiet is what makes it tolerable
     * to actually wear.
     */
    fun announce(text: String?) {
        if (text == null) {
            lastSpoken = null // cleared, so if it recurs later it is worth saying again
            return
        }
        if (!ready || text == lastSpoken) return
        lastSpoken = text
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "dailies-${System.currentTimeMillis()}")
    }

    fun shutdown() {
        runCatching { tts?.stop() }
        runCatching { tts?.shutdown() }
        tts = null
        ready = false
    }
}
