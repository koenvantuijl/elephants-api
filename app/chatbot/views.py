import base64
import json
import os
from functools import wraps

from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from openai import OpenAI

from chatbot.models import SimulatedCustomer


def basic_auth_required(view_func):
    """
    HTTP Basic Authentication for a single fixed username/password.

    Configure via environment variables:
      - API_USERNAME
      - API_PASSWORD
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        expected_user = os.environ.get("API_USERNAME")
        expected_pass = os.environ.get("API_PASSWORD")

        if not expected_user or not expected_pass:
            return HttpResponse("Server auth not configured", status=500)

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Basic "):
            resp = HttpResponse("Authentication required", status=401)
            resp["WWW-Authenticate"] = 'Basic realm="Simulated API"'
            return resp

        try:
            encoded = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            resp = HttpResponse("Invalid authentication header", status=401)
            resp["WWW-Authenticate"] = 'Basic realm="Simulated API"'
            return resp

        if username != expected_user or password != expected_pass:
            resp = HttpResponse("Invalid credentials", status=401)
            resp["WWW-Authenticate"] = 'Basic realm="Simulated API"'
            return resp

        return view_func(request, *args, **kwargs)

    return _wrapped


def chat_page(request):
    """
    Serveert een minimale HTML-pagina met een invoerveld.
    Deze pagina post naar het JSON endpoint ask_foods.
    """
    return render(request, "chatbot/chat.html")


@csrf_exempt
def ask_foods(request):
    """
    JSON API:
    POST {"foods": "<vrije tekst>"} -> {"model": "...", "reply": "..."}
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    foods = (payload.get("foods") or "").strip()
    if not foods:
        return JsonResponse({"error": "foods is required"}, status=400)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return JsonResponse({"error": "OPENAI_API_KEY not set"}, status=500)

    client = OpenAI(api_key=api_key)
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    prompt = (
        "You are a concise assistant. The user provided their favourite foods. "
        "Extract exactly three foods if present; otherwise ask a single follow-up question.\n\n"
        f"User input: {foods}"
    )

    resp = client.responses.create(
        model=model,
        input=prompt,
    )

    return JsonResponse(
        {
            "model": model,
            "reply": resp.output_text,
        }
    )


# ================================
# STEP 5 — API endpoint (protected in STEP 6)
# ================================

@require_GET
@basic_auth_required
def veg_customers(request):
    """
    Returns all simulated customers that are vegetarian or vegan
    and their top 3 favourite foods.

    Protected with HTTP Basic Auth (Step 6).
    """

    qs = (
        SimulatedCustomer.objects
        .filter(dietary_label__in=["vegetarian", "vegan"])
        .only("customer_code", "dietary_label", "favorite_foods")
        .order_by("customer_code")
    )

    results = [
        {
            "customer_code": c.customer_code,
            "dietary_label": c.dietary_label,
            "favorite_foods": c.favorite_foods,
        }
        for c in qs
    ]

    return JsonResponse(
        {
            "count": len(results),
            "results": results,
        },
        json_dumps_params={"indent": 2},
    )
