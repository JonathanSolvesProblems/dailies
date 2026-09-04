"""One place that decides how to reach Gemini.

There are two doors to the same models and this project needs both.

**Vertex AI** (`DAILIES_USE_VERTEX=true`) authenticates as the service account the code is
already running as and bills through the project's normal Cloud Billing account. No API key
exists anywhere, which is both simpler and safer.

**The AI Studio API key** is the fallback, for a laptop that has no Application Default
Credentials set up.

The reason this file exists, written down because the symptom looked nothing like the cause:
the deployed app was returning 429 on every call with `Your prepayment credits are depleted`.
That is not a rate limit and not a quota to wait out. The AI Studio key lived in a project
with no billing, and the billing account it would have needed is prepay-only with no payment
method and no balance, so no amount of waiting or retrying would ever have fixed it.

Vertex AI reaches the identical models through billing the project already had working, which
is what Cloud Run has been using for months. It also lifts the free tier's per-minute token
ceiling, which image frames blow through almost immediately: a single live check sends a
frame, and a handful in a row is enough to hit it.

Region note: `global` is deliberate. us-east1 has no gemini-3.6-flash at all and us-central1
404s on both 3.5 and 3.6; only `global` serves all three models this project uses. That was
measured, not assumed, and it is the kind of thing that silently reintroduces a fallback chain
if someone "tidies" the location to match the Cloud Run region.
"""

from __future__ import annotations

import os


def use_vertex() -> bool:
    return os.environ.get("DAILIES_USE_VERTEX", "").strip().lower() in ("1", "true", "yes")


def vertex_location() -> str:
    return os.environ.get("DAILIES_VERTEX_LOCATION", "global")


def vertex_project() -> str | None:
    return (
        os.environ.get("DAILIES_VERTEX_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or None
    )


def make_client():
    """A configured genai client, Vertex first, API key second."""
    from google import genai

    if use_vertex():
        project = vertex_project()
        if not project:
            raise RuntimeError(
                "DAILIES_USE_VERTEX is set but no project. Set DAILIES_VERTEX_PROJECT "
                "(or GOOGLE_CLOUD_PROJECT) to the Google Cloud project that has billing."
            )
        return genai.Client(vertexai=True, project=project, location=vertex_location())

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No credentials. Either set DAILIES_USE_VERTEX=true with Application Default "
            "Credentials (gcloud auth application-default login), or set GOOGLE_API_KEY "
            "from https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def describe() -> str:
    """Which door is in use, for logs and the capabilities endpoint."""
    if use_vertex():
        return f"vertex:{vertex_project()}:{vertex_location()}"
    return "aistudio-api-key"
