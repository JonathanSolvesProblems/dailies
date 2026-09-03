// The on-set screen. Same instrument as the web app, in the operating position.
//
// Designed to be read at a glance and mostly not looked at. The verdict is the whole screen
// because the operator is watching the scene, and everything else is small.

package com.dailies.glasses

import android.Manifest
import android.app.Application
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

// Same palette as the web app: anodized gunmetal body, one signal jade, oxide red.
private val Body = Color(0xFF31363B)
private val Well = Color(0xFF272B2F)
private val Raised = Color(0xFF3B4147)
private val Engrave = Color(0xFF1E2225)
private val Ink = Color(0xFFE3E7E9)
private val InkDim = Color(0xFFA2ABB1)
private val InkFaint = Color(0xFF767F86)
private val Accent = Color(0xFF2FBF84)
private val Alert = Color(0xFFC4553C)

private const val GAP_MS = 2500L

data class UiState(
    val rolling: Boolean = false,
    val verdict: Verdict? = null,
    val sceneId: String = "scene_a",
    val referenceTake: String = "",
    val availableTakes: List<String> = emptyList(),
    val referenceObjects: Int = 0,
    val checks: Int = 0,
    val flagged: Int = 0,
    val error: String? = null,
    val onHeadset: Boolean = false,
)

class RollViewModel(app: Application) : AndroidViewModel(app) {

    private val client = ContinuityClient(BuildConfig.DAILIES_ENDPOINT)
    val glasses = GlassesSession(app, viewModelScope)
    private val voice = Voice(app)

    private val _ui = MutableStateFlow(UiState())
    val ui = _ui.asStateFlow()
    private var loop: Job? = null

    init {
        glasses.initialize()
        loadReference()
    }

    fun loadReference() = viewModelScope.launch {
        runCatching { withContext(Dispatchers.IO) { client.fetchReference(_ui.value.sceneId) } }
            .onSuccess {
                _ui.value = _ui.value.copy(
                    referenceTake = it.takeId,
                    availableTakes = it.available,
                    referenceObjects = it.objectCount,
                    error = null,
                )
            }
            .onFailure { _ui.value = _ui.value.copy(error = "Reference: ${it.message}") }
    }

    fun pickReference(takeId: String) {
        _ui.value = _ui.value.copy(referenceTake = takeId)
    }

    fun toggle() {
        if (_ui.value.rolling) {
            loop?.cancel(); loop = null
            voice.announce(null)
            _ui.value = _ui.value.copy(rolling = false)
            glasses.disconnect()
            return
        }
        loop = viewModelScope.launch {
            if (!glasses.connect()) {
                _ui.value = _ui.value.copy(rolling = false)
                return@launch
            }
            _ui.value = _ui.value.copy(
                rolling = true, checks = 0, flagged = 0, verdict = null,
                onHeadset = voice.onHeadset(),
            )
            // Sequential, never on a timer. A check takes several seconds; a fixed interval
            // would stack overlapping requests until the queue collapsed.
            while (_ui.value.rolling) {
                runOneCheck()
                if (!_ui.value.rolling) break
                delay(GAP_MS)
            }
        }
    }

    private suspend fun runOneCheck() {
        val bitmap = glasses.grab()
        if (bitmap == null) {
            _ui.value = _ui.value.copy(error = "No frame from the glasses")
            return
        }
        try {
            val verdict = withContext(Dispatchers.IO) {
                val encoded = client.encodeFrame(bitmap)
                client.check(encoded, _ui.value.sceneId, _ui.value.referenceTake)
            }
            voice.announce(verdict.spoken())
            _ui.value = _ui.value.copy(
                verdict = verdict,
                checks = _ui.value.checks + 1,
                flagged = _ui.value.flagged + if (verdict.divergences.isEmpty()) 0 else 1,
                error = null,
            )
        } catch (e: Exception) {
            _ui.value = _ui.value.copy(error = e.message ?: "check failed")
        } finally {
            bitmap.recycle()
        }
    }

    override fun onCleared() {
        loop?.cancel()
        glasses.disconnect()
        voice.shutdown()
        super.onCleared()
    }

    class Factory(private val app: Application) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : ViewModel> create(modelClass: Class<T>): T = RollViewModel(app) as T
    }
}

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val permissions = registerForActivityResult(
            ActivityResultContracts.RequestMultiplePermissions()
        ) { }
        permissions.launch(
            arrayOf(
                Manifest.permission.BLUETOOTH,
                Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.CAMERA,
            )
        )

        setContent {
            val vm: RollViewModel = viewModel(
                factory = RollViewModel.Factory(application)
            )
            RollScreen(vm)
        }
    }
}

@Composable
private fun viewModel(factory: ViewModelProvider.Factory): RollViewModel {
    val owner = androidx.lifecycle.viewmodel.compose.LocalViewModelStoreOwner.current!!
    return remember { ViewModelProvider(owner, factory)[RollViewModel::class.java] }
}

@Composable
fun RollScreen(vm: RollViewModel) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    val link by vm.glasses.link.collectAsStateWithLifecycle()
    val linkMessage by vm.glasses.message.collectAsStateWithLifecycle()

    Column(
        Modifier.fillMaxSize().background(Body).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("DAILIES", color = Ink, fontSize = 19.sp, fontWeight = FontWeight.Bold, letterSpacing = 3.sp)
            Spacer(Modifier.weight(1f))
            Text(
                if (ui.rolling) "ROLLING" else linkStatus(link),
                color = if (ui.rolling) Alert else InkFaint,
                fontFamily = FontFamily.Monospace, fontSize = 11.sp,
            )
        }

        // Reference picker. Small, because it is set once and then ignored.
        Column(Modifier.fillMaxWidth().background(Well, RoundedCornerShape(2.dp))
            .border(1.dp, Engrave, RoundedCornerShape(2.dp)).padding(12.dp)) {
            Text("REFERENCE", color = InkDim, fontSize = 10.sp,
                fontFamily = FontFamily.Monospace, letterSpacing = 2.sp)
            Spacer(Modifier.height(8.dp))
            Row(
                Modifier.horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                ui.availableTakes.forEach { take ->
                    val selected = take == ui.referenceTake
                    Text(
                        take.removePrefix("take_").trimStart('0').ifEmpty { "0" },
                        color = if (selected) Accent else InkDim,
                        fontFamily = FontFamily.Monospace, fontSize = 13.sp,
                        modifier = Modifier
                            .background(if (selected) Well else Raised, RoundedCornerShape(2.dp))
                            .border(1.dp, if (selected) Accent else Engrave, RoundedCornerShape(2.dp))
                            .clickable(enabled = !ui.rolling) { vm.pickReference(take) }
                            .padding(horizontal = 14.dp, vertical = 7.dp)
                    )
                }
            }
            Spacer(Modifier.height(6.dp))
            Text("${ui.referenceObjects} objects tracked", color = InkFaint,
                fontFamily = FontFamily.Monospace, fontSize = 10.sp)
        }

        // The verdict. Everything else on this screen is subordinate to it.
        Column(
            Modifier.fillMaxWidth().weight(1f)
                .background(Well, RoundedCornerShape(2.dp))
                .border(1.dp, Engrave, RoundedCornerShape(2.dp))
                .padding(18.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.Center,
        ) {
            val v = ui.verdict
            when {
                ui.error != null -> Big(ui.error!!, Alert, 22)
                !ui.rolling && v == null -> {
                    Big("STANDBY", InkFaint, 38)
                    Spacer(Modifier.height(10.dp))
                    Text(linkMessage.ifEmpty { "Pick a reference and press Roll." },
                        color = InkDim, fontSize = 16.sp)
                }
                v == null -> {
                    Big("CHECKING", InkFaint, 34)
                    Spacer(Modifier.height(10.dp))
                    Text(linkMessage, color = InkDim, fontSize = 15.sp)
                }
                v.divergences.isEmpty() -> {
                    Big("HOLDS", Accent, 46)
                    Spacer(Modifier.height(10.dp))
                    Text(v.frameNote, color = InkDim, fontSize = 15.sp)
                }
                else -> {
                    Big("OFF THE MARK", Alert, 36)
                    Spacer(Modifier.height(12.dp))
                    v.divergences.forEach { d ->
                        Text(d.entity.uppercase(), color = Alert, fontSize = 20.sp,
                            fontWeight = FontWeight.Bold)
                        Text("reference: ${d.expected}", color = InkDim, fontSize = 15.sp)
                        Text("now: ${d.observed}", color = Ink, fontSize = 15.sp)
                        Spacer(Modifier.height(10.dp))
                    }
                }
            }
            if (v != null) {
                Spacer(Modifier.height(14.dp))
                Text("${v.latencyMs}ms · ${v.model} · ${ui.checks} checks · ${ui.flagged} flagged",
                    color = InkFaint, fontFamily = FontFamily.Monospace, fontSize = 10.sp)
            }
        }

        if (ui.rolling && !ui.onHeadset) {
            Text(
                "Audio is not routed to the glasses. Alerts will play from the phone.",
                color = Alert, fontSize = 12.sp, textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth()
            )
        }

        Button(
            onClick = { vm.toggle() },
            modifier = Modifier.fillMaxWidth().height(64.dp),
            shape = RoundedCornerShape(2.dp),
            colors = ButtonDefaults.buttonColors(
                containerColor = if (ui.rolling) Alert else Raised,
                contentColor = if (ui.rolling) Color.White else Ink,
            ),
        ) {
            Text(if (ui.rolling) "CUT" else "ROLL",
                fontSize = 18.sp, fontWeight = FontWeight.Bold, letterSpacing = 4.sp)
        }
    }
}

@Composable
private fun Big(text: String, color: Color, size: Int) {
    Text(text, color = color, fontSize = size.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
}

private fun linkStatus(link: Link) = when (link) {
    Link.Idle -> "standby"
    Link.Connecting -> "connecting"
    Link.Ready -> "camera live"
    Link.Error -> "error"
}
