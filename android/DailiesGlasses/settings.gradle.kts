import java.util.Properties
import kotlin.io.path.div
import kotlin.io.path.exists
import kotlin.io.path.inputStream

pluginManagement {
  repositories {
    google {
      content {
        includeGroupByRegex("com\\.android.*")
        includeGroupByRegex("com\\.google.*")
        includeGroupByRegex("androidx.*")
      }
    }
    mavenCentral()
    gradlePluginPortal()
  }
}

val localProperties =
    Properties().apply {
      val path = rootDir.toPath() / "local.properties"
      if (path.exists()) load(path.inputStream())
    }

dependencyResolutionManagement {
  repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
  repositories {
    google()
    mavenCentral()
    // The Meta Wearables SDK is published to GitHub Packages, not Maven Central, so this
    // needs a token with read:packages in local.properties (git-ignored).
    maven {
      url = uri("https://maven.pkg.github.com/facebook/meta-wearables-dat-android")
      credentials {
        username = ""
        password = System.getenv("GITHUB_TOKEN") ?: localProperties.getProperty("github_token")
      }
    }
  }
}

rootProject.name = "DailiesGlasses"

include(":app")
