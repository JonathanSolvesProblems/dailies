// Talks to the Dailies backend: fetch a reference take, post a frame, get a verdict.
//
// Deliberately plain HttpURLConnection and hand-rolled JSON. Two reasons. A judge cloning
// this should be able to read the whole network layer in one sitting without learning a
// client library, and the payload is one string field, so a serialization framework would
// be more code than it removes.

package com.dailies.glasses

import android.graphics.Bitmap
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL

data class Divergence(
    val entity: String,
    val expected: String,
    val observed: String,
    val confidence: Double,
)

data class Verdict(
    val ok: Boolean,
    val divergences: List<Divergence>,
    val frameNote: String,
    val model: String,
    val latencyMs: Int,
) {
    /** What gets spoken into the wearer's ear. Short, because they are watching a scene. */
    fun spoken(): String? {
        if (divergences.isEmpty()) return null
        val first = divergences.first()
        val rest = divergences.size - 1
        val tail = if (rest > 0) ", and $rest other${if (rest > 1) "s" else ""}" else ""
        return "${first.entity} is ${first.observed}, should be ${first.expected}$tail"
    }
}

data class ReferenceTake(val takeId: String, val available: List<String>, val objectCount: Int)

class ContinuityClient(private val baseUrl: String) {

    /** JPEG at this width. Measured: the backend answers in ~4s here and ~15s at full glasses
     *  resolution, and the extra pixels buy no accuracy on "did the mug move". */
    private val frameWidth = 512
    private val jpegQuality = 72

    fun encodeFrame(bitmap: Bitmap): String {
        val scale = frameWidth.toFloat() / bitmap.width
        val scaled =
            if (scale < 1f)
                Bitmap.createScaledBitmap(
                    bitmap, frameWidth, (bitmap.height * scale).toInt(), true
                )
            else bitmap
        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, jpegQuality, out)
        if (scaled !== bitmap) scaled.recycle()
        return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
    }

    fun fetchReference(sceneId: String): ReferenceTake {
        val body = get("$baseUrl/api/live/reference/$sceneId")
        val json = JSONObject(body)
        val takes = json.optJSONArray("available_takes") ?: JSONArray()
        return ReferenceTake(
            takeId = json.optString("reference_take"),
            available = (0 until takes.length()).map { takes.getString(it) },
            objectCount = json.optJSONArray("observations")?.length() ?: 0,
        )
    }

    fun check(frameBase64: String, sceneId: String, referenceTake: String): Verdict {
        val payload = JSONObject()
            .put("frame", frameBase64)
            .put("scene_id", sceneId)
            .put("reference_take", referenceTake)
        val json = JSONObject(post("$baseUrl/api/live/check", payload.toString()))

        val arr = json.optJSONArray("divergences") ?: JSONArray()
        val divergences = (0 until arr.length()).map {
            val d = arr.getJSONObject(it)
            Divergence(
                entity = d.optString("entity"),
                expected = d.optString("expected"),
                observed = d.optString("observed"),
                confidence = d.optDouble("confidence", 0.0),
            )
        }
        return Verdict(
            ok = json.optBoolean("ok", divergences.isEmpty()),
            divergences = divergences,
            frameNote = json.optString("frame_note"),
            model = json.optString("model"),
            latencyMs = json.optInt("latency_ms"),
        )
    }

    private fun get(url: String): String = open(url, "GET", null)

    private fun post(url: String, body: String): String = open(url, "POST", body)

    private fun open(url: String, method: String, body: String?): String {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000
            // Generous, because a check is a vision model call rather than a lookup, and a
            // cold backend can take a while. Shorter than this and the app gives up on
            // answers that were about to arrive.
            readTimeout = 90_000
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
            }
        }
        try {
            body?.let { conn.outputStream.use { os -> os.write(it.toByteArray()) } }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            if (code !in 200..299) {
                val detail = runCatching { JSONObject(text).optString("detail") }.getOrNull()
                throw RuntimeException(detail?.takeIf { it.isNotBlank() } ?: "HTTP $code")
            }
            return text
        } finally {
            conn.disconnect()
        }
    }
}
