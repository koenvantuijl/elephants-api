import base64
import json
import os
from functools import wraps

from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.db.models import QuerySet

from openai import OpenAI

from chatbot.models import SimulatedCustomer, SimulatedConversation, SimulatedMessage

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
    Paginated list of vegetarian/vegan customers.

    Query parameters:
      - limit (int, default 100, max 500)
      - offset (int, default 0)

    Response:
      {
        "count_total": <int>,      # total number of customers (all labels)
        "veg_total": <int>,        # total number of veg customers (filtered set)
        "limit": <int>,
        "offset": <int>,
        "count": <int>,            # count in this page
        "results": [...]
      }
    """

    # --- pagination params (defensive) ---
    try:
        limit = int(request.GET.get("limit", "100"))
    except ValueError:
        return JsonResponse({"error": "limit must be an integer"}, status=400)

    try:
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return JsonResponse({"error": "offset must be an integer"}, status=400)

    if offset < 0:
        offset = 0

    # Cap limit to prevent huge payloads / timeouts
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    # Total customers (unfiltered)
    count_total = SimulatedCustomer.objects.count()

    # Veg/vegetarian customers (filtered)
    base_qs = (
        SimulatedCustomer.objects
        .filter(dietary_label__in=["vegetarian", "vegan"])
        .order_by("customer_code")
    )
    veg_total = base_qs.count()

    # Page slice (values() avoids model instantiation)
    page_qs = base_qs.values(
        "customer_code", "dietary_label", "favorite_foods"
    )[offset: offset + limit]
    results = list(page_qs)

    return JsonResponse(
        {
            "count_total": count_total,  # e.g. 100
            "veg_total": veg_total,      # e.g. 23
            "results": results,
        }
    )

@require_GET
@basic_auth_required
def conversations_full(request):
    """
    Paginated list of conversations with full chat messages.

    Query parameters:
      - limit (int, default 25, max 100)
      - offset (int, default 0)

    Response:
      {
        "count_total": <int>,
        "limit": <int>,
        "offset": <int>,
        "count": <int>,
        "results": [
          {
            "conversation_id": <int>,
            "customer_code": <str>,
            "dietary_label": <str>,
            "favorite_foods": <list>,
            "ordered_dishes": <json>,
            "messages": [
              {"turn_index": <int>, "role": <str>, "content": <str>},
              ...
            ]
          },
          ...
        ]
      }
    """

    # --- pagination params (defensive) ---
    try:
        limit = int(request.GET.get("limit", "25"))
    except ValueError:
        return JsonResponse({"error": "limit must be an integer"}, status=400)

    try:
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return JsonResponse({"error": "offset must be an integer"}, status=400)

    if offset < 0:
        offset = 0

    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    base_qs = (
        SimulatedConversation.objects
        .select_related("customer")
        .prefetch_related("messages")
        .order_by("id")
    )

    count_total = base_qs.count()

    page_qs = base_qs[offset: offset + limit]

    results = []
    for conv in page_qs:
        customer = conv.customer

        msgs = (
            conv.messages.all()
            .only("turn_index", "role", "content")
            .order_by("turn_index")
        )

        results.append(
            {
                "conversation_id": conv.id,
                "customer_code": customer.customer_code,
                "dietary_label": customer.dietary_label,
                "favorite_foods": customer.favorite_foods,
                "ordered_dishes": getattr(conv, "ordered_dishes", None),
                "messages": [
                    {
                        "turn_index": m.turn_index,
                        "role": m.role,
                        "content": m.content,
                    }
                    for m in msgs
                ],
            }
        )

    return JsonResponse(
        {
            "count_total": count_total,
            "limit": limit,
            "offset": offset,
            "count": len(results),
            "results": results,
        },
        json_dumps_params={"indent": 2},
    )