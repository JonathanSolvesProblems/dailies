import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.jetbrains.kotlin.android)
  alias(libs.plugins.compose.compiler)
}

android {
  namespace = "com.dailies.glasses"
  compileSdk = 36

  defaultConfig {
    applicationId = "com.dailies.glasses"
    minSdk = 31
    targetSdk = 36
    versionCode = 1
    versionName = "1.0"

    // Populated from local.properties so the endpoint can be pointed at a laptop during
    // development without editing code.
    buildConfigField(
      "String",
      "DAILIES_ENDPOINT",
      "\"${project.findProperty("dailies.endpoint") ?: "https://dailies-564641829203.us-east1.run.app"}\""
    )

    // Meta Wearables registration. Empty is correct while running in Developer Mode.
    manifestPlaceholders["mwdat_application_id"] = ""
    manifestPlaceholders["mwdat_client_token"] = ""
  }

  buildFeatures {
    compose = true
    buildConfig = true
  }
  compileOptions {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
  }
  packaging { resources { excludes += "/META-INF/{AL2.0,LGPL2.1}" } }
}

kotlin { compilerOptions { jvmTarget = JvmTarget.JVM_17 } }

dependencies {
  implementation(libs.androidx.activity.compose)
  implementation(platform(libs.androidx.compose.bom))
  implementation(libs.androidx.material3)
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)
  implementation(libs.mwdat.core)
  implementation(libs.mwdat.camera)
  implementation(libs.mwdat.mockdevice)
}
